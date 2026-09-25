"""Isolated local fixture-page journal. Never opens a production ledger schema."""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from .channel_contracts import ReadBackConflict
from .channel_order_contracts import (
    ContractQuarantine, OfflineOrderPage, _cursor, _identifier, _integer,
)
from .channel_finance_contracts import OfflineSettlementPage


class OfflineContractJournal:
    def __init__(self, path: str | Path):
        path = Path(path)
        existed = path.exists()
        self.db = sqlite3.connect(str(path), timeout=3)
        try:
            if existed:
                marker = self.db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='offline_contract_marker'"
                ).fetchone()
                if not marker or self.db.execute("SELECT version FROM offline_contract_marker").fetchall() != [(1,)]:
                    raise ContractQuarantine("not_an_offline_contract_journal")
            else:
                self.db.executescript("""
                    CREATE TABLE offline_contract_marker(version INTEGER NOT NULL);
                    INSERT INTO offline_contract_marker VALUES(1);
                    CREATE TABLE checkpoints(
                        tenant TEXT, connection TEXT, provider TEXT, version INTEGER NOT NULL,
                        cursor TEXT, PRIMARY KEY(tenant, connection, provider));
                    CREATE TABLE pages(
                        tenant TEXT, connection TEXT, provider TEXT, page_key TEXT,
                        digest TEXT NOT NULL, payload TEXT NOT NULL,
                        PRIMARY KEY(tenant, connection, provider, page_key));
                """)
        except BaseException:
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

    @staticmethod
    def _scope(tenant: str, connection: str, provider: str) -> tuple[str, str, str]:
        _identifier(tenant)
        _identifier(connection)
        if provider not in {"naver", "coupang", "naver_settlement", "coupang_settlement"}:
            raise ContractQuarantine("unsupported_provider")
        return tenant, connection, provider

    def checkpoint(self, tenant: str, connection: str, provider: str) -> tuple[int, str | None]:
        scope = self._scope(tenant, connection, provider)
        row = self.db.execute(
            "SELECT version,cursor FROM checkpoints WHERE tenant=? AND connection=? AND provider=?", scope
        ).fetchone()
        return row if row else (0, None)

    def record(self, tenant: str, connection: str, page_key: str, page: OfflineOrderPage | OfflineSettlementPage,
               *, expected_version: int, continuation: str | None = None) -> tuple[int, bool]:
        if not isinstance(page, (OfflineOrderPage, OfflineSettlementPage)):
            raise ContractQuarantine("normalized_page_required")
        provider_scope = page.provider + "_settlement" if isinstance(page, OfflineSettlementPage) else page.provider
        scope = self._scope(tenant, connection, provider_scope)
        _identifier(page_key)
        _integer(expected_version)
        if continuation is not None:
            _cursor(continuation)
        if page.provider == "coupang" and continuation != page.next_cursor:
            raise ContractQuarantine("continuation_mismatch")
        # The digest includes the continuation so page replay cannot silently alter it.
        payload = page.canonical_payload()
        digest = hashlib.sha256((payload + "\n" + (continuation or "")).encode("utf-8")).hexdigest()
        try:
            self.db.execute("BEGIN IMMEDIATE")
            current, _ = self.checkpoint(*scope)
            existing = self.db.execute(
                "SELECT digest FROM pages WHERE tenant=? AND connection=? AND provider=? AND page_key=?",
                (*scope, page_key),
            ).fetchone()
            if existing:
                if existing[0] != digest:
                    raise ContractQuarantine("page_identity_content_conflict")
                self.db.commit()
                return current, True
            if current != expected_version:
                raise ContractQuarantine("checkpoint_version_conflict")
            self.db.execute("INSERT INTO pages VALUES(?,?,?,?,?,?)", (*scope, page_key, digest, payload))
            self.db.execute("""INSERT INTO checkpoints VALUES(?,?,?,?,?)
                ON CONFLICT(tenant,connection,provider)
                DO UPDATE SET version=excluded.version,cursor=excluded.cursor""",
                (*scope, current + 1, continuation))
            self.db.commit()
            return current + 1, False
        except BaseException:
            self.db.rollback()
            raise

    def page_count(self, tenant: str, connection: str, provider: str) -> int:
        scope = self._scope(tenant, connection, provider)
        return self.db.execute(
            "SELECT count(*) FROM pages WHERE tenant=? AND connection=? AND provider=?", scope
        ).fetchone()[0]

    def verify_readback(self, tenant: str, connection: str, provider: str,
                        page_key: str, page: OfflineOrderPage | OfflineSettlementPage) -> bool:
        """Verify that *page* matches the previously recorded digest.

        Returns ``True`` when the page is an exact duplicate (idempotent
        readback).  Raises ``ReadBackConflict`` when the stored digest
        differs from the supplied page's digest — the external state has
        changed since the original read, or a partial/corrupt payload was
        retransmitted.
        """
        if not isinstance(page, (OfflineOrderPage, OfflineSettlementPage)):
            raise ContractQuarantine("normalized_page_required")
        provider_scope = page.provider + "_settlement" if isinstance(page, OfflineSettlementPage) else page.provider
        scope = self._scope(tenant, connection, provider_scope)
        _identifier(page_key)
        payload = page.canonical_payload()
        # Match the exact digest format used in record():
        # sha256(payload + "\\n" + continuation_or_empty)
        continuation = page.next_cursor if isinstance(page, OfflineOrderPage) else None
        digest = hashlib.sha256((payload + "\n" + (continuation or "")).encode("utf-8")).hexdigest()
        existing = self.db.execute(
            "SELECT digest FROM pages WHERE tenant=? AND connection=? AND provider=? AND page_key=?",
            (*scope, page_key),
        ).fetchone()
        if existing is None:
            raise ReadBackConflict("no_recorded_page_for_key")
        if existing[0] != digest:
            raise ReadBackConflict("page_identity_content_conflict")
        return True

    def detect_partial_response(self, raw_body: Any) -> bool:
        """Return ``True`` when *raw_body* looks like a complete page.

        Heuristic checks that catch obviously truncated or partial
        responses before they reach the parser.  A response is partial
        when:

        - it is not a dict (not a JSON object)
        - its ``data``/``elements`` key exists but is a string (truncated
          JSON)
        - it is missing both ``data`` and ``elements`` keys

        This is a lightweight pre-filter; the parser still enforces
        strict invariants.
        """
        if not isinstance(raw_body, dict):
            raise ContractQuarantine("object_required")
        # Check for truncated string values BEFORE checking missing keys.
        if "data" in raw_body and isinstance(raw_body["data"], str):
            raise ContractQuarantine("partial_response_truncated_data")
        if "elements" in raw_body and isinstance(raw_body["elements"], str):
            raise ContractQuarantine("partial_response_truncated_elements")
        has_data = "data" in raw_body and isinstance(raw_body.get("data"), (list, dict))
        has_elements = "elements" in raw_body and isinstance(raw_body.get("elements"), list)
        if not has_data and not has_elements:
            raise ContractQuarantine("partial_response_missing_data")
        return True
