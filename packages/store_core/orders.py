"""Local DEMO order projection and deterministic supplier routing.

Routing consumes caller-supplied fixture quotes only.  It creates draft PO
proposals and purchase approvals; it never submits a supplier order or moves
money.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .domain import (
    ApprovalKind, Capability, ChannelOrder, ChannelOrderState, OrderLine,
    PurchaseLine, PurchaseOrderState, RoutingDecision, RoutingState,
    SupplierPurchaseOrder, OutboxEvent, OutboxState,
)
from .errors import ConflictError

_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}\Z")


def _opaque(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _OPAQUE.fullmatch(value):
        raise ConflictError(f"invalid {label}")
    return value


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _load_payload(service: Any, context: Any, payload_ref: str) -> dict[str, Any]:
    payload = service.get_normalized_payload(context, payload_ref)
    if payload.schema_version != 1 or _digest(json.loads(payload.payload_json)) != payload.canonical_digest:
        raise ConflictError("normalized payload digest/schema verification failed")
    value = json.loads(payload.payload_json)
    if not isinstance(value, dict):
        raise ConflictError("normalized order payload must be an object")
    return value


def ingest_order(service: Any, context: Any, channel_id: str, payload_ref: str,
                 idempotency_key: str | None = None) -> tuple[ChannelOrder, bool]:
    service.require(context, Capability.TENANT_ADMIN)
    channel_id = _opaque(channel_id, "channel_id")
    payload = _load_payload(service, context, payload_ref)
    external = _opaque(payload.get("external_order_id"), "external_order_id")
    event_id = _opaque(payload.get("event_id"), "event_id")
    currency = payload.get("currency")
    total = payload.get("total_minor")
    lines = payload.get("lines")
    if not isinstance(currency, str) or not isinstance(total, int) or total < 0 or not isinstance(lines, list) or not lines:
        raise ConflictError("invalid normalized order projection")
    key = idempotency_key or f"order:{channel_id}:{external}"
    if not isinstance(key, str) or not key.strip() or len(key) > 255:
        raise ConflictError("invalid idempotency_key")
    now = service._clock()
    with service.repo.transaction():
        order = ChannelOrder(str(uuid4()), context.tenant_id, channel_id, external, payload_ref, currency, total,
                             ChannelOrderState.ACCEPTED, now, key)
        order, replayed = service.repo.save_channel_order(order)
        if replayed:
            return order, True
        for line in lines:
            service.repo.save_order_line(OrderLine(str(uuid4()), context.tenant_id, order.id, _opaque(line["sku"], "sku"),
                                                   line["quantity"], line["unit_minor"]))
        service._audit(context.tenant_id, context.user_id, "order.accepted", order.id, "succeeded",
                       {"external_order_key": external, "payload_ref": payload_ref, "event_id": event_id})
        service.repo.append_outbox(OutboxEvent(
            str(uuid4()), context.tenant_id, "order.accepted", order.id,
            {"order_id": order.id, "payload_ref": payload_ref}, f"order.accepted:{order.id}",
            OutboxState.PENDING, now))
        return order, False


def _quotes_for(options: Mapping[str, Any], sku: str) -> list[Mapping[str, Any]]:
    raw = options.get(sku, ())
    if not isinstance(raw, (list, tuple)):
        raise ConflictError("supplier quote list required")
    return [quote for quote in raw if isinstance(quote, Mapping)]


def propose_routing(service: Any, context: Any, order_id: str, supplier_options: Mapping[str, Any],
                    expected_order_version: int = 1) -> tuple[SupplierPurchaseOrder, ...]:
    service.require(context, Capability.TENANT_ADMIN)
    order = service.repo.get_channel_order(context.tenant_id, order_id)
    if order.version != expected_order_version:
        raise ConflictError("order version conflict")
    if order.status not in {ChannelOrderState.ACCEPTED, ChannelOrderState.ROUTING, ChannelOrderState.PO_PENDING}:
        raise ConflictError("order cannot be routed")
    existing = service.repo.purchase_orders_for(context.tenant_id, order_id)
    if existing:
        return existing
    lines = service.repo.order_lines_for(context.tenant_id, order_id)
    selected: list[tuple[OrderLine, str, int]] = []
    for line in lines:
        candidates = []
        for quote in _quotes_for(supplier_options, line.sku):
            supplier = _opaque(quote.get("supplier_id"), "supplier_id")
            cost, available = quote.get("unit_cost_minor"), quote.get("available_quantity")
            if type(cost) is int and cost >= 0 and type(available) is int and available >= line.quantity:
                candidates.append((cost, supplier))
        if not candidates:
            with service.repo.transaction():
                current = service.repo.get_channel_order(context.tenant_id, order_id)
                if current.version != expected_order_version: raise ConflictError("order version conflict")
                current.status, current.version = ChannelOrderState.EXCEPTION, current.version + 1
                service.repo.update_channel_order(current, expected_order_version)
                service._audit(context.tenant_id, context.user_id, "order.routing_failed", order_id, "blocked", {"sku": line.sku})
            return ()
        cost, supplier = sorted(candidates, key=lambda value: (value[0], value[1]))[0]
        selected.append((line, supplier, cost))
    now = service._clock()
    with service.repo.transaction():
        current = service.repo.get_channel_order(context.tenant_id, order_id)
        if current.version != expected_order_version: raise ConflictError("order version conflict")
        current.status, current.version = ChannelOrderState.PO_PENDING, current.version + 1
        service.repo.update_channel_order(current, expected_order_version)
        grouped: dict[str, list[tuple[OrderLine, int]]] = {}
        for line, supplier, cost in selected:
            grouped.setdefault(supplier, []).append((line, cost))
            service.repo.save_routing_decision(RoutingDecision(str(uuid4()), context.tenant_id, line.id, supplier,
                                                               line.quantity, cost, "lowest feasible DEMO quote"))
            line.routed_status, line.version = "routed", line.version + 1
            service.repo.update_order_line(line, line.version - 1)
        result = []
        for supplier, routed in sorted(grouped.items()):
            idem = f"po:{order_id}:{supplier}"
            command, _ = service.request_approval(context, ApprovalKind.PURCHASE,
                f"po:{order_id}:{supplier}",
                {"order_id": order_id, "supplier_id": supplier,
                 "lines": [{"order_line_id": line.id, "quantity": line.quantity, "unit_cost_minor": cost} for line, cost in routed]},
                f"{idem}:approval", 1, current.version)
            po = SupplierPurchaseOrder(str(uuid4()), context.tenant_id, order_id, supplier,
                                       PurchaseOrderState.APPROVAL_PENDING, idem, command.id, now)
            po, _ = service.repo.save_purchase_order(po)
            for line, cost in routed:
                service.repo.save_purchase_line(PurchaseLine(str(uuid4()), context.tenant_id, po.id, line.id, line.quantity, cost))
            result.append(po)
        service._audit(context.tenant_id, context.user_id, "order.routing_proposed", order_id, "succeeded",
                       {"purchase_order_count": len(result)})
        return tuple(result)
