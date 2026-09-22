"""Bind an immutable tracking-fixture batch to the local DEMO gateway.

The batch is a review artefact only.  This module emits one fenced synthetic
command after a PURCHASE approval; it cannot submit tracking to a channel.
"""
from __future__ import annotations

from typing import Any

from .domain import ApprovalKind
from .offline_tracking_batch import FixtureTrackingBatch, verify_tracking_batch


def _identities(batch: FixtureTrackingBatch) -> tuple[str, ...]:
    if batch.provider == "coupang":
        return tuple(review.shipment_id for review in batch.reviews)
    return tuple(review.product_order_id for review in batch.reviews)


def _payload(batch: FixtureTrackingBatch) -> dict[str, Any]:
    """Project immutable component identities and digests, never a request body."""
    return {
        "contract": "tracking-batch-fixture-v1",
        "provider": batch.provider,
        "fixture_batch_digest": batch.approval_digest,
        "fixture_review_digests": tuple(review.approval_digest for review in batch.reviews),
        "tracking_identities": _identities(batch),
    }


def _target_id(batch: FixtureTrackingBatch) -> str:
    """Represent the bounded group as an opaque synthetic shipment target."""
    return f"batch.{batch.provider}.{batch.approval_digest}"


def _verify(service: Any, context: Any, batch: FixtureTrackingBatch,
            connection_ref: str) -> None:
    verify_tracking_batch(
        batch, approval_digest=batch.approval_digest,
        tenant_ref=context.tenant_id, connection_ref=connection_ref,
        now=service._clock())


def request_tracking_batch_approval(
    service: Any, context: Any, batch: FixtureTrackingBatch, *, connection_ref: str,
    idempotency_key: str, policy_version: int, target_version: int,
):
    """Create one PURCHASE approval for a bounded, reviewed fixture batch."""
    _verify(service, context, batch, connection_ref)
    return service.request_approval(
        context, ApprovalKind.PURCHASE, f"shipment:{_target_id(batch)}", _payload(batch),
        idempotency_key, policy_version, target_version,
        evidence=({"risk": "OFFLINE_FIXTURE_ONLY", "connection_ref": connection_ref,
                   "real_shipment_confirmed": False, "batch_size": len(batch.reviews)},))


def submit_approved_tracking_batch(
    service: Any, context: Any, batch: FixtureTrackingBatch, *, connection_ref: str,
    approval_id: str, idempotency_key: str, policy_version: int,
):
    """Submit only the exact, approval-bound batch projection to DEMO."""
    _verify(service, context, batch, connection_ref)
    return service.submit_demo_tool(
        context, actor_type="workflow", actor_id="tracking-batch-fixture-v1",
        tool="dispatch_shipment", target_type="shipment",
        target_id=_target_id(batch), input_value=_payload(batch),
        idempotency_key=idempotency_key, requested_policy_version=policy_version,
        approval_id=approval_id)
