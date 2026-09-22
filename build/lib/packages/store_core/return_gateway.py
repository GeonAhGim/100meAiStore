"""Bind a reviewed return fixture to the local DEMO approval gateway.

This bridge records a refund-intent review and, after an authorized decision,
can drive one synthetic DEMO execution.  It never calls a marketplace,
initiates a bank refund, or treats a synthetic effect as a real refund.
"""
from __future__ import annotations

from typing import Any

from .domain import ApprovalKind
from .offline_claim_contracts import FixtureReturnReview, verify_return_fixture_review


def _payload(review: FixtureReturnReview) -> dict[str, Any]:
    return {
        "contract": "coupang-return-fixture-v1",
        "fixture_review_digest": review.approval_digest,
        "vendor_ref": review.vendor_ref,
        "receipt_id": review.receipt_id,
        "shipment_id": review.shipment_id,
        "vendor_item_id": review.vendor_item_id,
        "cancel_quantity": review.cancel_quantity,
        "fixture_amount_krw": review.fixture_amount_krw,
        "fixture_amount_cap_krw": review.fixture_amount_cap_krw,
        "withdrawal_review_digest": review.withdrawal_review_digest,
    }


def _verify(service: Any, context: Any, review: FixtureReturnReview,
            connection_ref: str) -> None:
    verify_return_fixture_review(
        review, approval_digest=review.approval_digest,
        tenant_ref=context.tenant_id, connection_ref=connection_ref,
        now=service._clock())


def request_return_approval(service: Any, context: Any, review: FixtureReturnReview, *,
                            connection_ref: str, idempotency_key: str,
                            policy_version: int, target_version: int):
    """Create a REFUND approval containing only the immutable fixture projection."""
    _verify(service, context, review, connection_ref)
    return service.request_approval(
        context, ApprovalKind.REFUND, f"order:{review.order_id}", _payload(review),
        idempotency_key, policy_version, target_version,
        evidence=({"risk": "OFFLINE_FIXTURE_ONLY", "connection_ref": connection_ref,
                   "bank_refund_verified": False},))


def submit_approved_return(service: Any, context: Any, review: FixtureReturnReview, *,
                           connection_ref: str, approval_id: str,
                           idempotency_key: str, policy_version: int):
    """Submit the exact reviewed command to the DEMO-only typed gateway."""
    _verify(service, context, review, connection_ref)
    return service.submit_demo_tool(
        context, actor_type="workflow", actor_id="return-fixture-v1",
        tool="claim_action", target_type="order", target_id=review.order_id,
        input_value=_payload(review), idempotency_key=idempotency_key,
        requested_policy_version=policy_version, approval_id=approval_id)
