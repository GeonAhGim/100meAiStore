"""Approval revalidation and local-only DEMO purchase-order reconciliation."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from .domain import (ApprovalState, Capability, ChannelOrderState, CommandState,
                     OutboxEvent, OutboxState, PurchaseOrderState)
from .errors import ConflictError

_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}\Z")


def _digest(value: Any) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _verify_po(service: Any, context: Any, po_id: str):
    po = service.repo.get_purchase_order(context.tenant_id, po_id)
    if po.status != PurchaseOrderState.APPROVAL_PENDING or po.approval_command_id is None:
        raise ConflictError("purchase order is not awaiting approval")
    order = service.repo.get_channel_order(context.tenant_id, po.channel_order_id)
    if order.status != ChannelOrderState.PO_PENDING:
        raise ConflictError("order changed; purchase approval must be renewed")
    lines = service.repo.purchase_lines_for(context.tenant_id, po.id)
    decisions = {row.order_line_id: row for row in service.repo.routing_for(context.tenant_id, order.id)}
    command = service.repo.get_command(context.tenant_id, po.approval_command_id)
    approval = service.repo.get_approval_for_command(context.tenant_id, command.id)
    expected = []
    for line in lines:
        decision = decisions.get(line.order_line_id)
        if decision is None or decision.supplier_id != po.supplier_id or decision.quantity != line.quantity or decision.unit_cost_minor != line.unit_cost_minor:
            raise ConflictError("purchase routing changed; approval must be renewed")
        order_line = next((item for item in service.repo.order_lines_for(context.tenant_id, order.id) if item.id == line.order_line_id), None)
        if order_line is None or order_line.routed_status != "routed":
            raise ConflictError("order line routing is no longer valid")
        expected.append({"order_line_id": line.order_line_id, "quantity": line.quantity, "unit_cost_minor": line.unit_cost_minor})
    expected_payload = {"order_id": order.id, "supplier_id": po.supplier_id, "lines": expected}
    if _digest(command.payload) != _digest(expected_payload) or command.state != CommandState.AWAITING_APPROVAL or approval.state != ApprovalState.PENDING:
        raise ConflictError("purchase approval intent no longer matches PO")
    intent = service.repo.get_approval_intent(context.tenant_id, command.id)
    if intent is None or intent.target_version != order.version:
        raise ConflictError("purchase approval target version is stale")
    return po, order, command, approval


def approve_demo_po(service: Any, context: Any, po_id: str, approve: bool, reason: str):
    service.require(context, Capability.TENANT_ADMIN)
    if type(approve) is not bool or not isinstance(reason, str) or not reason.strip():
        raise ConflictError("approval decision and reason are required")
    with service.repo.transaction():
        po, order, command, approval = _verify_po(service, context, po_id)
        decided = service.decide(context, command.id, approve, reason)
        po.status = PurchaseOrderState.APPROVED if approve else PurchaseOrderState.CANCELLED
        po.version += 1
        service.repo.update_purchase_order(po, po.version - 1)
        now = service._clock()
        service._audit(context.tenant_id, context.user_id, "purchase_order.approved" if approve else "purchase_order.rejected", po.id, "succeeded", {"command_id": command.id})
        service.repo.append_outbox(OutboxEvent(str(uuid4()), context.tenant_id,
            "purchase_order.approved" if approve else "purchase_order.rejected", po.id,
            {"purchase_order_id": po.id, "command_id": command.id, "state": po.status.value},
            f"po:{po.id}:{po.status.value}", OutboxState.PENDING, now))
        return po


def submit_demo_po(service: Any, context: Any, po_id: str):
    service.require(context, Capability.TENANT_ADMIN)
    with service.repo.transaction():
        po = service.repo.get_purchase_order(context.tenant_id, po_id)
        if po.status != PurchaseOrderState.APPROVED or po.approval_command_id is None:
            raise ConflictError("only an approved PO can be submitted")
        order = service.repo.get_channel_order(context.tenant_id, po.channel_order_id)
        command = service.repo.get_command(context.tenant_id, po.approval_command_id)
        approval = service.repo.get_approval_for_command(context.tenant_id, command.id)
        intent = service.repo.get_approval_intent(context.tenant_id, command.id)
        now = service._clock()
        if order.status != ChannelOrderState.PO_PENDING or approval.state != ApprovalState.APPROVED or intent is None or now >= approval.expires_at:
            raise ConflictError("approved PO is no longer executable")
        service._check_intent(command, approval, intent)
        po.status, po.version = PurchaseOrderState.SUBMITTED, po.version + 1
        service.repo.update_purchase_order(po, po.version - 1)
        service._audit(context.tenant_id, context.user_id, "purchase_order.submitted_demo", po.id, "succeeded", {"mode": "DEMO"})
        service.repo.append_outbox(OutboxEvent(str(uuid4()), context.tenant_id, "purchase_order.submitted", po.id,
            {"purchase_order_id": po.id, "mode": "DEMO"}, f"po:{po.id}:submitted", OutboxState.PENDING, now))
        return po


def reconcile_demo_po(service: Any, context: Any, po_id: str, response: Mapping[str, Any]):
    service.require(context, Capability.TENANT_ADMIN)
    if not isinstance(response, Mapping) or any(type(key) is not str for key in response) or set(response) - {"status", "provider_reference", "observed_at"} or "status" not in response:
        raise ConflictError("invalid DEMO PO response")
    status = response["status"]
    if status not in {"ACKNOWLEDGED", "REJECTED", "UNKNOWN"}:
        raise ConflictError("unsupported DEMO PO response status")
    ref = response.get("provider_reference")
    if ref is not None and (not isinstance(ref, str) or not _OPAQUE.fullmatch(ref)):
        raise ConflictError("invalid provider reference")
    observed = response.get("observed_at")
    if observed is None: observed = service._clock()
    if not isinstance(observed, datetime) or observed.tzinfo is None:
        raise ConflictError("response observed_at must be timezone aware")
    body = {"status": status, "provider_reference": ref, "observed_at": observed.isoformat()}
    digest = _digest(body)
    with service.repo.transaction():
        po = service.repo.get_purchase_order(context.tenant_id, po_id)
        if po.status not in {PurchaseOrderState.SUBMITTED, PurchaseOrderState.ACKNOWLEDGED, PurchaseOrderState.EXCEPTION}:
            raise ConflictError("PO response is not applicable")
        if po.last_response_digest == digest:
            return po, True
        if po.last_response_digest is not None:
            raise ConflictError("PO response conflicts with recorded evidence")
        if status == "UNKNOWN":
            return po, False
        po.status = PurchaseOrderState.ACKNOWLEDGED if status == "ACKNOWLEDGED" else PurchaseOrderState.EXCEPTION
        po.provider_reference, po.last_response_digest, po.last_observed_at = ref, digest, observed
        po.version += 1
        service.repo.update_purchase_order(po, po.version - 1)
        service._audit(context.tenant_id, context.user_id, "purchase_order.response_reconciled", po.id, "succeeded", {"status": status, "response_digest": digest})
        service.repo.append_outbox(OutboxEvent(str(uuid4()), context.tenant_id, "purchase_order.response_reconciled", po.id,
            {"purchase_order_id": po.id, "status": status, "response_digest": digest}, f"po:{po.id}:response:{digest}", OutboxState.PENDING, observed))
        return po, False
