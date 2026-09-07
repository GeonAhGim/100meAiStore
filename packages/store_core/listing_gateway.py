"""Bind a reviewed Coupang listing fixture to the generic DEMO approval gateway."""
from __future__ import annotations

from typing import Any

from .domain import ApprovalKind
from .offline_listing_contracts import FixtureListingReview, verify_listing_fixture_review


def _payload(review: FixtureListingReview) -> dict[str, Any]:
    return {
        "contract": "coupang-listing-fixture-v1",
        "fixture_review_digest": review.approval_digest,
        "supplier_sku": review.supplier_sku,
        "price_minor": review.price_krw,
        "quantity": review.quantity,
    }


def _verify(service: Any, context: Any, review: FixtureListingReview,
            connection_ref: str) -> None:
    verify_listing_fixture_review(
        review, approval_digest=review.approval_digest,
        tenant_ref=context.tenant_id, connection_ref=connection_ref,
        now=service._clock())


def request_listing_approval(service: Any, context: Any, review: FixtureListingReview, *,
                             offer_id: str, connection_ref: str,
                             idempotency_key: str, policy_version: int,
                             target_version: int):
    _verify(service, context, review, connection_ref)
    return service.request_approval(
        context, ApprovalKind.PRODUCT, f"offer:{offer_id}", _payload(review),
        idempotency_key, policy_version, target_version,
        evidence=({"risk": "OFFLINE_FIXTURE_ONLY", "connection_ref": connection_ref},))


def submit_approved_listing(service: Any, context: Any, review: FixtureListingReview, *,
                            offer_id: str, connection_ref: str, approval_id: str,
                            idempotency_key: str, policy_version: int):
    _verify(service, context, review, connection_ref)
    return service.submit_demo_tool(
        context, actor_type="workflow", actor_id="listing-fixture-v1",
        tool="publish_offer", target_type="offer", target_id=offer_id,
        input_value=_payload(review), idempotency_key=idempotency_key,
        requested_policy_version=policy_version, approval_id=approval_id)
