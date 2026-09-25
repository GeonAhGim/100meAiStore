"""Bind the bounded two-item split tracking review to DEMO execution.

A split shipment is two dispatches: the INITIAL review sends one item's
invoice and defers the other, and the FOLLOWUP review later sends the
deferred item under its remapped shipment. The follow-up used to be checked
only against the prior review object the caller still held, so after a
restart nothing proved the first half had gone out, and two follow-ups could
both dispatch. The follow-up is now bound to durable DEMO evidence: the
INITIAL dispatch whose review it continues must be an accepted tool command
whose execution attempt verified success, and no other follow-up for that
split may already have been dispatched.
"""
from __future__ import annotations

import json
from typing import Any

from .domain import ApprovalKind, AttemptState
from .errors import ConflictError
from .offline_split_tracking import FixtureSplitReview, verify_split_review

CONTRACT = "coupang-split-tracking-fixture-v1"


def _payload(review: FixtureSplitReview) -> dict[str, Any]:
    return {
        "contract": CONTRACT,
        "fixture_review_digest": review.approval_digest,
        # Use order_ref rather than the legacy linked-PO payload field.  This
        # is a shipment command and must not invoke the PO state transition.
        "order_ref": review.order_id,
        "phase": review.phase,
        "items": [
            {"vendor_item_id": item.vendor_item_id, "shipment_id": item.shipment_id,
             "quantity": item.quantity, "invoice_number": item.invoice_number,
             "estimated_shipping_date": item.estimated_shipping_date,
             "split_shipping": item.split_shipping, "pre_split_shipped": item.pre_split_shipped,
             "carrier": item.carrier}
            for item in review.items
        ],
        "source_digest": review.source_digest,
        "claim_digest": review.claim_digest,
        "preparation_digest": review.preparation_digest,
        "prior_review_digest": review.prior_review_digest,
    }


def _target_id(review: FixtureSplitReview) -> str:
    """Use the reviewed shipment identity, including after split remapping."""
    if not review.items:
        raise ValueError("split review requires at least one item")
    shipment_ids = {item.shipment_id for item in review.items}
    if len(shipment_ids) != 1:
        raise ValueError("split review must bind one shipment target")
    return next(iter(shipment_ids))


def _verify(service: Any, context: Any, review: FixtureSplitReview,
            connection_ref: str) -> None:
    verify_split_review(
        review, approval_digest=review.approval_digest,
        tenant_ref=context.tenant_id, connection_ref=connection_ref,
        now=service._clock())


def _dispatches(service: Any, context: Any) -> list[tuple[Any, dict[str, Any]]]:
    rows = []
    for command in service.repo.tool_commands_for(context.tenant_id):
        if command.tool != "dispatch_shipment" or command.state != "accepted":
            continue
        payload = json.loads(command.input_json)
        if payload.get("contract") == CONTRACT:
            rows.append((command, payload))
    return rows


def _verified(service: Any, context: Any, command: Any) -> bool:
    from .service import _strict_intent_digest
    key = _strict_intent_digest({"tenant": context.tenant_id, "command": command.approval_command_id, "operation_version": 1})
    attempt = service.repo.attempt_for_key(context.tenant_id, key)
    return attempt is not None and attempt.state == AttemptState.VERIFIED_SUCCESS


def _require_followup_basis(service: Any, context: Any, review: FixtureSplitReview) -> None:
    """A FOLLOWUP continues exactly one verified INITIAL dispatch and is the only follow-up for it."""
    if review.phase != "FOLLOWUP":
        return
    dispatches = _dispatches(service, context)
    initial = [command for command, payload in dispatches
               if payload.get("phase") == "INITIAL" and payload.get("fixture_review_digest") == review.prior_review_digest]
    if not initial:
        raise ConflictError("initial split shipment was never dispatched; follow-up refused")
    if not any(_verified(service, context, command) for command in initial):
        raise ConflictError("initial split shipment is not verified delivered to DEMO; follow-up refused")
    if any(payload.get("phase") == "FOLLOWUP" and payload.get("prior_review_digest") == review.prior_review_digest
           and payload.get("fixture_review_digest") != review.approval_digest for _, payload in dispatches):
        raise ConflictError("another follow-up was already dispatched for this split")


def request_split_tracking_approval(
    service: Any, context: Any, review: FixtureSplitReview, *,
    connection_ref: str, idempotency_key: str, policy_version: int,
    target_version: int,
):
    _verify(service, context, review, connection_ref)
    _require_followup_basis(service, context, review)
    return service.request_approval(
        context, ApprovalKind.PURCHASE, f"shipment:{_target_id(review)}", _payload(review),
        idempotency_key, policy_version, target_version,
        evidence=({"risk": "OFFLINE_FIXTURE_ONLY", "connection_ref": connection_ref,
                   "real_shipment_confirmed": False},))


def submit_approved_split_tracking(
    service: Any, context: Any, review: FixtureSplitReview, *,
    connection_ref: str, approval_id: str, idempotency_key: str,
    policy_version: int,
):
    _verify(service, context, review, connection_ref)
    _require_followup_basis(service, context, review)  # re-checked: the durable state may have changed since approval
    return service.submit_demo_tool(
        context, actor_type="workflow", actor_id="split-tracking-fixture-v1",
        tool="dispatch_shipment", target_type="shipment", target_id=_target_id(review),
        input_value=_payload(review), idempotency_key=idempotency_key,
        requested_policy_version=policy_version, approval_id=approval_id)
