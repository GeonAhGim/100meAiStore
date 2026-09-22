"""Approval revalidation, renewal and local-only DEMO purchase-order reconciliation."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from .domain import (ApprovalKind, ApprovalState, Capability, ChannelOrderState, CommandState,
                     OutboxEvent, OutboxState, PurchaseOrderState)
from .errors import ConflictError

_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}\Z")


def _digest(value: Any) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _routed_payload(service: Any, context: Any, po: Any, order: Any) -> dict[str, Any]:
    """The approval payload the PO's lines, routing decisions and order lines support today."""
    lines = service.repo.purchase_lines_for(context.tenant_id, po.id)
    decisions = {row.order_line_id: row for row in service.repo.routing_for(context.tenant_id, order.id)}
    expected = []
    for line in lines:
        decision = decisions.get(line.order_line_id)
        if decision is None or decision.supplier_id != po.supplier_id or decision.quantity != line.quantity or decision.unit_cost_minor != line.unit_cost_minor:
            raise ConflictError("purchase routing changed; approval must be renewed")
        order_line = next((item for item in service.repo.order_lines_for(context.tenant_id, order.id) if item.id == line.order_line_id), None)
        if order_line is None or order_line.routed_status != "routed":
            raise ConflictError("order line routing is no longer valid")
        expected.append({"order_line_id": line.order_line_id, "quantity": line.quantity, "unit_cost_minor": line.unit_cost_minor})
    return {"order_id": order.id, "supplier_id": po.supplier_id, "lines": expected}


def _verify_po(service: Any, context: Any, po_id: str):
    po = service.repo.get_purchase_order(context.tenant_id, po_id)
    if po.status != PurchaseOrderState.APPROVAL_PENDING or po.approval_command_id is None:
        raise ConflictError("purchase order is not awaiting approval")
    order = service.repo.get_channel_order(context.tenant_id, po.channel_order_id)
    if order.status != ChannelOrderState.PO_PENDING:
        raise ConflictError("order changed; purchase approval must be renewed")
    expected_payload = _routed_payload(service, context, po, order)
    command = service.repo.get_command(context.tenant_id, po.approval_command_id)
    approval = service.repo.get_approval_for_command(context.tenant_id, command.id)
    if _digest(command.payload) != _digest(expected_payload) or command.state != CommandState.AWAITING_APPROVAL or approval.state != ApprovalState.PENDING:
        raise ConflictError("purchase approval intent no longer matches PO")
    intent = service.repo.get_approval_intent(context.tenant_id, command.id)
    if intent is None or intent.target_version != order.version:
        raise ConflictError("purchase approval target version is stale")
    return po, order, command, approval


def approve_demo_po(service: Any, context: Any, po_id: str, approve: bool, reason: str):
    service.require(context, Capability.APPROVE_PURCHASE)
    if type(approve) is not bool or not isinstance(reason, str) or not reason.strip():
        raise ConflictError("approval decision and reason are required")
    with service.repo.transaction():
        service.require(context, Capability.APPROVE_PURCHASE)
        po, order, command, approval = _verify_po(service, context, po_id)
        _, expired = service._decide(context, command.id, approve, reason)
        if not expired:
            po.status = PurchaseOrderState.APPROVED if approve else PurchaseOrderState.CANCELLED
            po.version += 1
            service.repo.update_purchase_order(po, po.version - 1)
            now = service._clock()
            service._audit(context.tenant_id, context.user_id, "purchase_order.approved" if approve else "purchase_order.rejected", po.id, "succeeded", {"command_id": command.id})
            service.repo.append_outbox(OutboxEvent(str(uuid4()), context.tenant_id,
                "purchase_order.approved" if approve else "purchase_order.rejected", po.id,
                {"purchase_order_id": po.id, "command_id": command.id, "state": po.status.value},
                f"po:{po.id}:{po.status.value}", OutboxState.PENDING, now))
    if expired:
        raise ConflictError("approval expired")
    return po


def _renewable(service: Any, po: Any, approval: Any) -> bool:
    """A PO whose approval can no longer lead to a submission while its order still wants it."""
    if po.status == PurchaseOrderState.CANCELLED:
        return approval.state in {ApprovalState.REJECTED, ApprovalState.EXPIRED}
    if po.status == PurchaseOrderState.APPROVAL_PENDING:
        return approval.state == ApprovalState.EXPIRED or service._clock() >= approval.expires_at
    if po.status == PurchaseOrderState.APPROVED:
        return service._clock() >= approval.expires_at
    return False


def regenerate_demo_po(service: Any, context: Any, po_id: str, reason: str, expected_po_version: int):
    """Issue a fresh purchase approval for a PO whose previous one was rejected or expired.

    The PO keeps its identity, supplier and lines (one PO per order and
    supplier), so regeneration renews the approval, never the routing: the
    lines are re-verified against the routing decisions and the order's
    current version becomes the new approval's target. A PO cancelled because
    its order was cancelled, or one already submitted, is not renewable.
    """
    service.require(context, Capability.TENANT_ADMIN)
    if not isinstance(reason, str) or not reason.strip() or type(expected_po_version) is not int:
        raise ConflictError("regeneration reason and PO version are required")
    with service.repo.transaction():
        po = service.repo.get_purchase_order(context.tenant_id, po_id)
        if po.version != expected_po_version:
            raise ConflictError("purchase order version conflict")
        order = service.repo.get_channel_order(context.tenant_id, po.channel_order_id)
        if order.status != ChannelOrderState.PO_PENDING:
            raise ConflictError("order is not awaiting purchase; regeneration refused")
        if po.approval_command_id is None:
            raise ConflictError("purchase order has no approval to renew")
        previous = service.repo.get_approval_for_command(context.tenant_id, po.approval_command_id)
        if not _renewable(service, po, previous):
            raise ConflictError("purchase order approval is still usable or the PO is past approval")
        payload = _routed_payload(service, context, po, order)
        command, _ = service.request_approval(context, ApprovalKind.PURCHASE, f"po:{order.id}:{po.supplier_id}", payload,
                                              f"{po.idempotency_key}:approval:v{po.version + 1}", 1, order.version)
        prior_command = po.approval_command_id
        po.status, po.approval_command_id, po.version = PurchaseOrderState.APPROVAL_PENDING, command.id, po.version + 1
        service.repo.update_purchase_order(po, expected_po_version)
        now = service._clock()
        service._audit(context.tenant_id, context.user_id, "purchase_order.regenerated", po.id, "succeeded",
                       {"reason": reason, "previous_command_id": prior_command, "command_id": command.id})
        service.repo.append_outbox(OutboxEvent(str(uuid4()), context.tenant_id, "purchase_order.regenerated", po.id,
            {"purchase_order_id": po.id, "command_id": command.id, "previous_command_id": prior_command},
            f"po:{po.id}:regenerated:v{po.version}", OutboxState.PENDING, now))
        return po


def submit_demo_po(service: Any, context: Any, po_id: str):
    service.require(context, Capability.TENANT_ADMIN)
    with service.repo.transaction():
        po = service.repo.get_purchase_order(context.tenant_id, po_id)
        if po.status != PurchaseOrderState.APPROVED or po.approval_command_id is None:
            raise ConflictError("only an approved PO can be submitted")
        order = service.repo.get_channel_order(context.tenant_id, po.channel_order_id)
        if service.repo.demo_stop_active(context.tenant_id, order.channel_id):
            raise ConflictError("DEMO scope stopped")
        command = service.repo.get_command(context.tenant_id, po.approval_command_id)
        approval = service.repo.get_approval_for_command(context.tenant_id, command.id)
        intent = service.repo.get_approval_intent(context.tenant_id, command.id)
        now = service._clock()
        if order.status != ChannelOrderState.PO_PENDING or approval.state != ApprovalState.APPROVED or intent is None or now >= approval.expires_at:
            raise ConflictError("approved PO is no longer executable")
        service.prepare_execution(context, command.id, intent.policy_version, order.version)
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
