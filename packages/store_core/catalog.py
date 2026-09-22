"""Strict local DEMO catalog source, canonical, projection, lineage and offer-approval flow."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .domain import ApprovalKind, ApprovalState, Capability, CommandState, DemoCatalogImport, DemoCatalogSnapshot, DemoCanonicalProduct, DemoProductLineage, DemoChannelOffer, OutboxEvent, OutboxState
from .errors import ConflictError

_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}\Z")
_CURRENCIES = {"KRW", "USD", "JPY", "EUR", "GBP", "CNY"}
_FIELDS = {"external_key", "sku", "title", "category", "price_minor", "currency", "attributes"}
_PII_NAMES = {"email", "phone", "address", "name", "customer", "buyer", "recipient"}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _OPAQUE.fullmatch(value):
        raise ConflictError(f"invalid {label}")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 500 or "\x00" in value:
        raise ConflictError(f"invalid {label}")
    return value.strip()


def _attributes(value: Any) -> dict[str, Any]:
    def safe(item: Any) -> bool:
        if isinstance(item, Mapping):
            return all(isinstance(k, str) and k.lower() not in _PII_NAMES and safe(v) for k, v in item.items())
        if isinstance(item, (list, tuple)):
            return all(safe(v) for v in item)
        return item is None or type(item) in (str, int, float, bool)
    if not isinstance(value, Mapping) or not safe(value):
        raise ConflictError("invalid or PII catalog attributes")
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        decoded = json.loads(encoded)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ConflictError("catalog attributes must be finite JSON") from exc
    if len(encoded.encode()) > 16 * 1024:
        raise ConflictError("catalog attributes too large")
    return decoded


def _normalize(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(rows, (list, tuple)) or not rows or len(rows) > 10_000:
        raise ConflictError("catalog rows are required")
    result: list[dict[str, Any]] = []
    seen_external: set[str] = set()
    seen_sku: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != _FIELDS:
            raise ConflictError("invalid catalog row schema")
        external, sku = _id(row["external_key"], "external_key"), _id(row["sku"], "sku")
        if external in seen_external or sku in seen_sku:
            raise ConflictError("duplicate catalog key")
        seen_external.add(external); seen_sku.add(sku)
        if type(row["price_minor"]) is not int or row["price_minor"] < 0 or row["currency"] not in _CURRENCIES:
            raise ConflictError("invalid catalog price or currency")
        result.append({"external_key": external, "sku": sku, "title": _text(row["title"], "title"),
                       "category": _text(row["category"], "category"), "price_minor": row["price_minor"],
                       "currency": row["currency"], "attributes": _attributes(row["attributes"])})
    return result


def ingest_demo_catalog(service: Any, context: Any, supplier_id: str, rows: Sequence[Mapping[str, Any]], idempotency_key: str) -> tuple[DemoCatalogImport, bool]:
    service.require(context, Capability.TENANT_ADMIN)
    supplier_id, idempotency_key = _id(supplier_id, "supplier_id"), _text(idempotency_key, "idempotency_key")
    if len(idempotency_key) > 255:
        raise ConflictError("invalid idempotency_key")
    normalized = _normalize(rows)
    source_digest = _digest(normalized)
    now = service._clock()
    with service.repo.transaction():
        batch, replay = service.repo.save_catalog_import(DemoCatalogImport(str(uuid4()), context.tenant_id, supplier_id, source_digest, idempotency_key, now))
        if replay:
            return batch, True
        for row in normalized:
            snapshot = DemoCatalogSnapshot(str(uuid4()), context.tenant_id, batch.id, supplier_id, row["external_key"], _digest(row), json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")), now)
            service.repo.save_catalog_snapshot(snapshot)
            canonical = DemoCanonicalProduct(str(uuid4()), context.tenant_id, row["sku"], row["title"], row["category"], row["price_minor"], row["currency"], json.dumps(row["attributes"], ensure_ascii=False, sort_keys=True, separators=(",", ":")), snapshot.id, 1, now)
            canonical = service.repo.save_canonical_product(canonical)
            service.repo.save_product_lineage(DemoProductLineage(str(uuid4()), context.tenant_id, snapshot.id, canonical.id, 1, now))
        service._audit(context.tenant_id, context.user_id, "catalog.imported", batch.id, "succeeded", {"source_digest": source_digest, "row_count": len(normalized)})
        service.repo.append_outbox(OutboxEvent(str(uuid4()), context.tenant_id, "catalog.imported", batch.id,
                                               {"import_id": batch.id, "source_digest": source_digest},
                                               f"catalog:{batch.id}:imported", OutboxState.PENDING, now))
        return batch, False


def _gates(service: Any, context: Any, sku: str, max_age_seconds: int, *,
           price_minor: int | None = None) -> dict[str, Any]:
    """Margin and inventory gates on the latest DEMO evidence for ``sku``.

    Projection keeps its original leniency (a SKU without evidence may be
    projected). An approval passes ``price_minor`` and is strict: both kinds of
    evidence must exist, the margin must have been projected for exactly the
    offer's price, and at least one unit must be observed in stock.
    """
    strict = price_minor is not None
    projections = service.repo.price_projections_for(context.tenant_id, sku)
    inventory = service.repo.inventory_snapshots_for(context.tenant_id, sku)
    if strict and (not projections or not inventory):
        raise ConflictError("approval requires DEMO margin and inventory evidence")
    if projections and projections[-1].status != "READY":
        raise ConflictError("projected margin below DEMO threshold")
    cutoff = service._clock() - timedelta(seconds=max_age_seconds)
    if projections and projections[-1].calculated_at < cutoff:
        raise ConflictError("DEMO price projection is stale; reapproval required")
    if inventory and inventory[-1].observed_at < cutoff:
        raise ConflictError("DEMO inventory observation is stale; reapproval required")
    if strict and projections[-1].selling_price_minor != price_minor:
        raise ConflictError("margin was projected for a different price; reapproval required")
    if strict and inventory[-1].quantity < 1:
        raise ConflictError("no DEMO inventory observed for the offer")
    return {"price_projection_id": projections[-1].id if projections else None,
            "projected_margin": projections[-1].projected_margin if projections else None,
            "inventory_snapshot_id": inventory[-1].id if inventory else None,
            "inventory_quantity": inventory[-1].quantity if inventory else None}


def project_demo_offer(service: Any, context: Any, canonical_product_id: str, channel_id: str, price_minor: int | None = None, max_age_seconds: int = 86400) -> tuple[DemoChannelOffer, bool]:
    service.require(context, Capability.TENANT_ADMIN)
    channel_id = _id(channel_id, "channel_id")
    if type(max_age_seconds) is not int or max_age_seconds < 1:
        raise ConflictError("invalid freshness window")
    with service.repo.transaction():
        product = service.repo.get_canonical_product(context.tenant_id, _id(canonical_product_id, "canonical_product_id"))
        if price_minor is None: price_minor = product.price_minor
        if type(price_minor) is not int or price_minor < 0: raise ConflictError("invalid offer price")
        _gates(service, context, product.sku, max_age_seconds)
        offer = DemoChannelOffer(str(uuid4()), context.tenant_id, channel_id, product.id, product.source_snapshot_id, product.sku, price_minor, product.currency, 1, service._clock())
        offer, replay = service.repo.save_channel_offer(offer)
        if not replay:
            service._audit(context.tenant_id, context.user_id, "catalog.offer_projected", offer.id, "succeeded", {"channel_id": channel_id, "canonical_product_id": product.id})
            service.repo.append_outbox(OutboxEvent(str(uuid4()), context.tenant_id, "catalog.offer_projected", offer.id,
                                                   {"offer_id": offer.id, "channel_id": channel_id},
                                                   f"catalog:offer:{offer.id}:projected", OutboxState.PENDING, service._clock()))
        return offer, replay


def _offer_intent(service: Any, context: Any, offer_id: str) -> tuple[Any, dict[str, Any]]:
    offer = service.repo.get_channel_offer(context.tenant_id, _id(offer_id, "offer_id"))
    product = service.repo.get_canonical_product(context.tenant_id, offer.canonical_product_id)
    if offer.source_snapshot_id != product.source_snapshot_id or offer.currency != product.currency:
        raise ConflictError("offer no longer matches its canonical product; reapproval required")
    return offer, {"offer_id": offer.id, "channel_id": offer.channel_id, "canonical_product_id": product.id,
                   "source_snapshot_id": offer.source_snapshot_id, "sku": product.sku, "title": product.title,
                   "price_minor": offer.price_minor, "currency": offer.currency, "mode": "DEMO",
                   "external_write_authorized": False}


def request_demo_offer_approval(service: Any, context: Any, offer_id: str, idempotency_key: str,
                                max_age_seconds: int = 86400):
    """Ask for a PRODUCT approval of a projected DEMO offer: catalog scan -> offer -> approval.

    The gates run again at request time, so an approval is never requested for
    an offer whose margin or stock went bad after projection. Nothing is
    published: the approval is the precondition an operator decides on, and
    ``verify_approved_demo_offer`` re-checks it before anything may use it.
    """
    service.require(context, Capability.TENANT_ADMIN)
    if type(max_age_seconds) is not int or max_age_seconds < 1:
        raise ConflictError("invalid freshness window")
    idempotency_key = _text(idempotency_key, "idempotency_key")
    with service.repo.transaction():
        offer, payload = _offer_intent(service, context, offer_id)
        evidence = _gates(service, context, payload["sku"], max_age_seconds, price_minor=offer.price_minor)
        return service.request_approval(context, ApprovalKind.PRODUCT, f"offer:{offer.id}", payload, idempotency_key,
                                        1, offer.version, evidence=({"risk": "DEMO_ONLY", **evidence},))


def verify_approved_demo_offer(service: Any, context: Any, offer_id: str, command_id: str,
                               max_age_seconds: int = 86400) -> dict[str, Any]:
    """Confirm an approved offer is still exactly what was approved and still passes its gates.

    Fails closed on a rejected, pending or expired approval, an approval for
    another offer, a changed offer, or evidence that has since gone stale or
    below threshold. Success is a readback, not publication authority.
    """
    service.require(context, Capability.TENANT_ADMIN)
    offer, payload = _offer_intent(service, context, offer_id)
    command = service.repo.get_command(context.tenant_id, _id(command_id, "command_id"))
    approval = service.repo.get_approval_for_command(context.tenant_id, command.id)
    if command.kind != ApprovalKind.PRODUCT or command.target_ref != f"offer:{offer.id}":
        raise ConflictError("approval is not for this offer")
    if command.state != CommandState.APPROVED or approval.state != ApprovalState.APPROVED:
        raise ConflictError("offer is not approved")
    if service._clock() >= approval.expires_at:
        raise ConflictError("offer approval expired; reapproval required")
    if _digest(command.payload) != _digest(payload):
        raise ConflictError("offer changed since approval; reapproval required")
    intent = service.repo.get_approval_intent(context.tenant_id, command.id)
    if intent is None or intent.target_version != offer.version:
        raise ConflictError("offer approval target version is stale")
    evidence = _gates(service, context, payload["sku"], max_age_seconds, price_minor=offer.price_minor)
    return {"offer_id": offer.id, "command_id": command.id, "approved_by": approval.decided_by,
            "mode": "DEMO", "external_write_authorized": False, **evidence}
