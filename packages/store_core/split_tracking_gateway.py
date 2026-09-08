"""Bind the bounded two-item split tracking review to DEMO execution."""
from __future__ import annotations

from typing import Any

from .domain import ApprovalKind
from .offline_split_tracking import FixtureSplitReview, verify_split_review


def _payload(review: FixtureSplitReview) -> dict[str, Any]:
    return {
        "contract": "coupang-split-tracking-fixture-v1",
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


def _verify(service: Any, context: Any, review: FixtureSplitReview,
            connection_ref: str) -> None:
    verify_split_review(
        review, approval_digest=review.approval_digest,
        tenant_ref=context.tenant_id, connection_ref=connection_ref,
        now=service._clock())


def request_split_tracking_approval(
    service: Any, context: Any, review: FixtureSplitReview, *,
    connection_ref: str, idempotency_key: str, policy_version: int,
    target_version: int,
):
    _verify(service, context, review, connection_ref)
    return service.request_approval(
        context, ApprovalKind.PURCHASE, f"order:{review.order_id}", _payload(review),
        idempotency_key, policy_version, target_version,
        evidence=({"risk": "OFFLINE_FIXTURE_ONLY", "connection_ref": connection_ref,
                   "real_shipment_confirmed": False},))


def submit_approved_split_tracking(
    service: Any, context: Any, review: FixtureSplitReview, *,
    connection_ref: str, approval_id: str, idempotency_key: str,
    policy_version: int,
):
    _verify(service, context, review, connection_ref)
    return service.submit_demo_tool(
        context, actor_type="workflow", actor_id="split-tracking-fixture-v1",
        tool="dispatch_shipment", target_type="order", target_id=review.order_id,
        input_value=_payload(review), idempotency_key=idempotency_key,
        requested_policy_version=policy_version, approval_id=approval_id)
