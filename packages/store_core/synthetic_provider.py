"""Local DEMO provider: independent durable effects, never a real channel adapter."""

import hashlib
import json
import sqlite3
import threading
from pathlib import Path

from .errors import ConflictError


class DurableSyntheticProvider:
    execution_mode = "DEMO"
    adapter_version = "synthetic-v1"
    MODES = frozenset({"success", "refusal", "timeout_before", "timeout_after",
                       "delayed_lookup", "lookup_unavailable", "stale_response"})

    def __init__(self, path, mode="success", authoritative_absence=False):
        self.path = Path(path)
        self.mode = mode
        self.authoritative_absence = bool(authoritative_absence)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("""CREATE TABLE IF NOT EXISTS synthetic_effects (
            tenant_id TEXT NOT NULL, operation_key TEXT NOT NULL,
            intent_digest TEXT NOT NULL, kind TEXT NOT NULL,
            provider_reference TEXT NOT NULL, hidden_lookups INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (tenant_id, operation_key))""")
        self._db.commit()

    @property
    def mode(self):
        return self._mode

    @mode.setter
    def mode(self, value):
        if value not in self.MODES:
            raise ValueError("Unsupported synthetic mode")
        self._mode = value

    @staticmethod
    def _result(kind, tenant_id, operation_key, intent_digest, reference=None, authoritative=False):
        body = [kind, tenant_id, operation_key, intent_digest, reference, authoritative]
        digest = hashlib.sha256(json.dumps(body, separators=(",", ":")).encode()).hexdigest()
        return {"kind": kind, "response_digest": digest,
                "provider_reference": reference, "authoritative_absence": authoritative}

    def _existing(self, tenant_id, operation_key, intent_digest):
        if not all(isinstance(v, str) and v for v in (tenant_id, operation_key, intent_digest)):
            raise ValueError("Nonempty tenant, operation key and intent digest required")
        row = self._db.execute(
            "SELECT * FROM synthetic_effects WHERE tenant_id=? AND operation_key=?",
            (tenant_id, operation_key)).fetchone()
        if row and row["intent_digest"] != intent_digest:
            raise ConflictError("Synthetic operation key already binds a different intent")
        return row

    def execute(self, tenant_id: str, operation_key: str, intent_digest: str) -> dict:
        with self._lock:
            # An immediate transaction also serializes independent provider instances.
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._existing(tenant_id, operation_key, intent_digest)
                if row is None:
                    if self.mode == "timeout_before":
                        raise TimeoutError("Synthetic timeout before effect")
                    reference = hashlib.sha256(json.dumps(
                        [tenant_id, operation_key], separators=(",", ":")).encode()).hexdigest()
                    kind = "FOUND_FAILURE" if self.mode == "refusal" else "FOUND_SUCCESS"
                    self._db.execute("INSERT INTO synthetic_effects VALUES (?,?,?,?,?,?)",
                                     (tenant_id, operation_key, intent_digest, kind, reference,
                                      1 if self.mode == "delayed_lookup" else 0))
                    row = self._existing(tenant_id, operation_key, intent_digest)
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise
            if self.mode in {"timeout_after", "delayed_lookup", "lookup_unavailable"}:
                raise TimeoutError("Synthetic response lost after durable effect")
            if self.mode == "stale_response":
                return self._result("INCONCLUSIVE", tenant_id, operation_key, intent_digest)
            return self._result(row["kind"], tenant_id, operation_key, intent_digest,
                                row["provider_reference"])

    def lookup(self, tenant_id: str, operation_key: str, intent_digest: str) -> dict:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._existing(tenant_id, operation_key, intent_digest)
                if self.mode in {"lookup_unavailable", "stale_response"}:
                    result = self._result("INCONCLUSIVE", tenant_id, operation_key, intent_digest)
                elif row is None:
                    result = self._result("ABSENT", tenant_id, operation_key, intent_digest,
                                          authoritative=self.authoritative_absence)
                elif row["hidden_lookups"]:
                    self._db.execute("UPDATE synthetic_effects SET hidden_lookups=hidden_lookups-1 "
                                     "WHERE tenant_id=? AND operation_key=?", (tenant_id, operation_key))
                    result = self._result("INCONCLUSIVE", tenant_id, operation_key, intent_digest)
                else:
                    result = self._result(row["kind"], tenant_id, operation_key, intent_digest,
                                          row["provider_reference"])
                self._db.commit()
                return result
            except BaseException:
                self._db.rollback()
                raise

    def effect_count(self, tenant_id):
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM synthetic_effects WHERE tenant_id=? "
                                    "AND kind='FOUND_SUCCESS'", (tenant_id,)).fetchone()[0]

    def close(self):
        with self._lock:
            self._db.close()
