"""Bind reviewed tracking fixtures to the local DEMO approval gateway.

The bridge is deliberately limited to synthetic execution.  It does not send
an invoice to a channel or claim that a real shipment was confirmed.
"""
from __future__ import annotations

from typing import Any

from .domain import ApprovalKind
from .offline_tracking_contracts import (
    FixtureNaverDispatchReview, FixtureTrackingReview, verify_fixture_review,
)


def _payload(review: FixtureTrackingReview | FixtureNaverDispatchReview) -> dict[str, Any]:
    if isinstance(review, FixtureTrackingReview):
        return {
            "contract": "coupang-tracking-fixture-v1",
            "fixture_review_digest": review.approval_digest,
            "shipment_id": review.shipment_id,
            "vendor_item_id": review.vendor_item_id,
            "invoice_number": review.invoice_number,
            "carrier": review.carrier,
            "preparation_review_digest": review.preparation_review_digest,
            "claim_source_digest": review.claim_source_digest,
        }
    return {
        "contract": "naver-dispatch-fixture-v1",
        "fixture_review_digest": review.approval_digest,
        "product_order_id": review.product_order_id,
        "invoice_number": review.invoice_number,
        "carrier": review.carrier,
        "dispatch_date": review.dispatch_date,
    }


def _target_id(review: FixtureTrackingReview | FixtureNaverDispatchReview) -> str:
    return review.shipment_id if isinstance(review, FixtureTrackingReview) else review.product_order_id


def _verify(service: Any, context: Any,
            review: FixtureTrackingReview | FixtureNaverDispatchReview,
            connection_ref: str) -> None:
    verify_fixture_review(
        review, approval_digest=review.approval_digest,
        tenant_ref=context.tenant_id, connection_ref=connection_ref,
        now=service._clock())


def request_tracking_approval(
    service: Any, context: Any,
    review: FixtureTrackingReview | FixtureNaverDispatchReview, *,
    connection_ref: str, idempotency_key: str, policy_version: int,
    target_version: int,
):
    """Create a PURCHASE approval for one immutable fixture tracking review."""
    _verify(service, context, review, connection_ref)
    return service.request_approval(
        context, ApprovalKind.PURCHASE, f"shipment:{_target_id(review)}",
        _payload(review), idempotency_key, policy_version, target_version,
        evidence=({"risk": "OFFLINE_FIXTURE_ONLY", "connection_ref": connection_ref,
                   "real_shipment_confirmed": False},))


def submit_approved_tracking(
    service: Any, context: Any,
    review: FixtureTrackingReview | FixtureNaverDispatchReview, *,
    connection_ref: str, approval_id: str, idempotency_key: str,
    policy_version: int,
):
    """Submit the exact reviewed tracking payload to the DEMO gateway."""
    _verify(service, context, review, connection_ref)
    return service.submit_demo_tool(
        context, actor_type="workflow", actor_id="tracking-fixture-v1",
        tool="dispatch_shipment", target_type="shipment",
        target_id=_target_id(review), input_value=_payload(review),
        idempotency_key=idempotency_key, requested_policy_version=policy_version,
        approval_id=approval_id)
