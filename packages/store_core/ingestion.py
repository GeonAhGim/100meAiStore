"""Strict, local-only DEMO order ingestion.

This module intentionally contains no HTTP client, credentials, marketplace write,
supplier, payment, or model integration.  Adapters are read fixtures and all
durable effects are committed by :class:`StoreControlPlane` in one transaction.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol

from .domain import (
    AdapterCapability, AdapterPollCheckpoint, Capability, DemoAdapterDescription, DemoPage,
    NormalizedDemoOrder, NormalizedInboundPayload, TenantContext,
)
from .errors import ConflictError

Page = DemoPage

MAX_PAGE_ITEMS = 100
MAX_LINES = 100
MAX_STRING_LENGTH = 255
MAX_PAYLOAD_BYTES = 64 * 1024
ALLOWED_CURRENCIES = frozenset({"KRW", "USD", "JPY", "EUR", "GBP", "CNY"})
_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class RetryableReadError(Exception):
    """A bounded, caller-retryable fixture read failure."""


class DemoReadAdapter(Protocol):
    def describe(self) -> DemoAdapterDescription: ...
    def list_changes(self, cursor: str | None, overlap_from: datetime | None = None) -> DemoPage: ...


@dataclass(frozen=True)
class DemoPollResult:
    checkpoint: AdapterPollCheckpoint
    payload_refs: tuple[str, ...]
    inbox_ids: tuple[str, ...]
    replayed_count: int
    has_more: bool


class FixtureDemoReadAdapter:
    """Small deterministic paging fixture used by contract tests and demos."""

    def __init__(self, pages: list[DemoPage] | tuple[DemoPage, ...], *, provider: str = "demo",
                 adapter_version: str = "demo-orders-v1", fail_once: bool = False) -> None:
        self.pages = tuple(pages)
        self.provider = provider
        self.adapter_version = adapter_version
        self.fail_once = fail_once
        self.calls: list[tuple[str | None, datetime | None]] = []

    def describe(self) -> DemoAdapterDescription:
        return DemoAdapterDescription(self.provider, self.adapter_version)

    def list_changes(self, cursor: str | None, overlap_from: datetime | None = None) -> DemoPage:
        self.calls.append((cursor, overlap_from))
        if self.fail_once:
            self.fail_once = False
            raise RetryableReadError("temporary DEMO fixture read failure")
        index = 0 if cursor is None else _cursor_index(cursor)
        if index >= len(self.pages):
            now = datetime.now(timezone.utc)
            return DemoPage((), cursor, False, now)
        return self.pages[index]


def _cursor_index(cursor: str) -> int:
    if not isinstance(cursor, str) or not re.fullmatch(r"p[1-9][0-9]{0,8}", cursor):
        raise ConflictError("invalid DEMO page cursor")
    return int(cursor[1:])


def _opaque(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _OPAQUE.fullmatch(value):
        raise ConflictError(f"invalid {label}")
    return value


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalize_demo_order(item: Mapping[str, Any]) -> tuple[NormalizedDemoOrder, str, str]:
    """Validate and canonicalize one complete untrusted fixture row."""
    if not isinstance(item, Mapping):
        raise ConflictError("DEMO order must be an object")
    allowed = {"external_order_id", "event_id", "revision", "currency", "total_minor", "lines", "source_digest"}
    if any(type(key) is not str for key in item) or set(item) - allowed:
        raise ConflictError("unknown DEMO order field")
    required = allowed - {"source_digest"}
    if set(item) & required != required:
        raise ConflictError("missing DEMO order field")
    external_id = _opaque(item["external_order_id"], "external_order_id")
    event_id = _opaque(item["event_id"], "event_id")
    revision = item["revision"]
    if type(revision) is not int or revision < 1:
        raise ConflictError("revision must be a positive integer")
    currency = item["currency"]
    if type(currency) is not str or currency not in ALLOWED_CURRENCIES:
        raise ConflictError("currency is not allowlisted")
    total = item["total_minor"]
    if type(total) is not int or total < 0:
        raise ConflictError("total_minor must be a nonnegative integer")
    raw_lines = item["lines"]
    if not isinstance(raw_lines, (list, tuple)) or not raw_lines or len(raw_lines) > MAX_LINES:
        raise ConflictError("lines must be a nonempty bounded list")
    lines: list[dict[str, Any]] = []
    calculated = 0
    for raw in raw_lines:
        if not isinstance(raw, Mapping) or set(raw) != {"sku", "quantity", "unit_minor"}:
            raise ConflictError("invalid DEMO order line schema")
        sku = _opaque(raw["sku"], "sku")
        quantity, unit = raw["quantity"], raw["unit_minor"]
        if type(quantity) is not int or quantity < 1:
            raise ConflictError("line quantity must be a positive integer")
        if type(unit) is not int or unit < 0:
            raise ConflictError("line unit_minor must be a nonnegative integer")
        calculated += quantity * unit
        lines.append({"sku": sku, "quantity": quantity, "unit_minor": unit})
    if calculated != total:
        raise ConflictError("order amount does not match line amounts")
    source_digest = item.get("source_digest")
    if source_digest is not None and (not isinstance(source_digest, str) or not _DIGEST.fullmatch(source_digest)):
        raise ConflictError("source_digest must be a SHA-256 hex digest")
    payload = {
        "external_order_id": external_id, "event_id": event_id, "revision": revision,
        "currency": currency, "total_minor": total, "lines": lines,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ConflictError("normalized DEMO payload exceeds size limit")
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    ref = f"demo-{event_id}-r{revision}-{digest[:16]}"
    return NormalizedDemoOrder(external_id, event_id, revision, currency, total, tuple(lines), source_digest), encoded, ref


def validate_demo_page(page: DemoPage) -> None:
    if not isinstance(page, DemoPage):
        raise ConflictError("adapter returned an invalid page")
    if not isinstance(page.items, (list, tuple)) or type(page.has_more) is not bool or len(page.items) > MAX_PAGE_ITEMS:
        raise ConflictError("DEMO page exceeds item limit")
    if page.next_cursor is not None:
        _cursor_index(page.next_cursor)
    if page.has_more and page.next_cursor is None:
        raise ConflictError("page with more results requires next_cursor")
    if not isinstance(page.observed_at, datetime) or page.observed_at.tzinfo is None:
        raise ConflictError("page observed_at must be timezone aware")


def poll_demo_connection(service: Any, context: TenantContext, provider: str, connection_id: str,
                         expected_checkpoint_version: int, adapter: DemoReadAdapter,
                         overlap_from: datetime | None = None) -> DemoPollResult:
    """Fetch one page and atomically persist payloads, inbox receipts and cursor."""
    service.require(context, Capability.TENANT_ADMIN)
    if type(expected_checkpoint_version) is not int or expected_checkpoint_version < 0:
        raise ConflictError("expected checkpoint version must be nonnegative")
    description = adapter.describe()
    if not isinstance(description, DemoAdapterDescription) or description.provider != provider or description.mode != "DEMO" or description.capability != AdapterCapability.ORDERS_READ:
        raise ConflictError("DEMO ORDERS_READ adapter required")
    if type(description.normalized_schema_version) is not int or description.normalized_schema_version != 1:
        raise ConflictError("unsupported normalized DEMO schema version")
    if overlap_from is not None and (not isinstance(overlap_from, datetime) or overlap_from.tzinfo is None):
        raise ConflictError("overlap_from must be timezone aware")
    manifest = service.repo.get_adapter_manifest(context.tenant_id, provider, connection_id)
    if (manifest.adapter_version != description.adapter_version
            or AdapterCapability.ORDERS_READ not in manifest.capabilities
            or AdapterCapability.INBOUND_EVENTS not in manifest.capabilities
            or 1 not in manifest.inbound_schema_versions):
        raise ConflictError("unsupported DEMO adapter manifest")
    checkpoint = service.repo.get_poll_checkpoint(context.tenant_id, provider, connection_id)
    if checkpoint and checkpoint.adapter_version != description.adapter_version:
        raise ConflictError("adapter version does not match poll checkpoint")
    read_cursor = checkpoint.cursor if checkpoint else None
    read_overlap = overlap_from if overlap_from is not None else (checkpoint.overlap_from if checkpoint else None)
    page = adapter.list_changes(read_cursor, read_overlap)
    validate_demo_page(page)
    normalized: list[tuple[NormalizedDemoOrder, str, str]] = [normalize_demo_order(item) for item in page.items]
    now = service._clock()
    if now.tzinfo is None:
        raise ConflictError("service clock must be timezone aware")
    with service.repo.transaction():
        service.require(context, Capability.TENANT_ADMIN)
        manifest = service.repo.get_adapter_manifest(context.tenant_id, provider, connection_id)
        if manifest.adapter_version != description.adapter_version or AdapterCapability.ORDERS_READ not in manifest.capabilities or AdapterCapability.INBOUND_EVENTS not in manifest.capabilities or 1 not in manifest.inbound_schema_versions:
            raise ConflictError("unsupported DEMO adapter manifest")
        current = service.repo.get_poll_checkpoint(context.tenant_id, provider, connection_id)
        actual = current.version if current else 0
        if actual != expected_checkpoint_version:
            raise ConflictError("adapter poll checkpoint version conflict")
        refs: list[str] = []
        inbox_ids: list[str] = []
        replayed = 0
        for order, payload_json, immutable_ref in normalized:
            service.repo.save_normalized_payload(NormalizedInboundPayload(
                context.tenant_id, immutable_ref, _digest(json.loads(payload_json)), 1,
                payload_json, order.source_digest, now,
            ))
            message, was_replayed = service.receive_inbound(
                context, provider, connection_id, order.event_id, 1,
                _digest(json.loads(payload_json)), immutable_ref,
            )
            refs.append(immutable_ref)
            inbox_ids.append(message.id)
            replayed += int(was_replayed)
        cursor = page.next_cursor if page.next_cursor is not None else read_cursor
        next_checkpoint = AdapterPollCheckpoint(
            context.tenant_id, provider, connection_id, description.adapter_version, cursor,
            read_overlap, expected_checkpoint_version + 1, now, now,
        )
        service.repo.insert_or_advance_poll_checkpoint(next_checkpoint, expected_checkpoint_version)
    return DemoPollResult(next_checkpoint, tuple(refs), tuple(inbox_ids), replayed, page.has_more)
