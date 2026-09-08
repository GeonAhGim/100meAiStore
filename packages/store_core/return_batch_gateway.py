"""Bind a bounded return-fixture batch to one local DEMO command."""
from __future__ import annotations

from typing import Any

from .domain import ApprovalKind
from .offline_return_batch import FixtureReturnBatch, verify_return_batch


def _target_id(batch: FixtureReturnBatch) -> str:
    return f"batch.return.{batch.approval_digest}"


def _payload(batch: FixtureReturnBatch) -> dict[str, Any]:
    return {"contract": "return-batch-fixture-v1", "fixture_batch_digest": batch.approval_digest,
            "vendor_ref": batch.vendor_ref, "fixture_amount_krw": batch.fixture_amount_krw,
            "items": tuple({"fixture_review_digest": review.approval_digest, "receipt_id": review.receipt_id,
                             "order_id": review.order_id, "vendor_item_id": review.vendor_item_id,
                             "cancel_quantity": review.cancel_quantity,
                             "fixture_amount_krw": review.fixture_amount_krw}
                           for review in batch.reviews)}


def _verify(service: Any, context: Any, batch: FixtureReturnBatch, connection_ref: str) -> None:
    verify_return_batch(batch, approval_digest=batch.approval_digest, tenant_ref=context.tenant_id,
                        connection_ref=connection_ref, now=service._clock())


def request_return_batch_approval(service: Any, context: Any, batch: FixtureReturnBatch, *,
                                  connection_ref: str, idempotency_key: str,
                                  policy_version: int, target_version: int):
    _verify(service, context, batch, connection_ref)
    return service.request_approval(context, ApprovalKind.REFUND, f"order:{_target_id(batch)}", _payload(batch),
        idempotency_key, policy_version, target_version,
        evidence=({"risk": "OFFLINE_FIXTURE_ONLY", "connection_ref": connection_ref,
                   "bank_refund_verified": False, "batch_size": len(batch.reviews)},))


def submit_approved_return_batch(service: Any, context: Any, batch: FixtureReturnBatch, *,
                                 connection_ref: str, approval_id: str, idempotency_key: str,
                                 policy_version: int):
    _verify(service, context, batch, connection_ref)
    return service.submit_demo_tool(context, actor_type="workflow", actor_id="return-batch-fixture-v1",
        tool="claim_action", target_type="order", target_id=_target_id(batch), input_value=_payload(batch),
        idempotency_key=idempotency_key, requested_policy_version=policy_version, approval_id=approval_id)
