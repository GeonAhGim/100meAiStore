"""Scoped composition of non-executable return-review fixtures."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Mapping

from .channel_order_contracts import ContractQuarantine
from .offline_claim_contracts import FixtureReturnResult, FixtureReturnReview, verify_return_fixture_review
from .offline_tracking_contracts import _digest, _hash_ref


@dataclass(frozen=True)
class FixtureReturnBatch:
    tenant_ref: str = field(repr=False)
    connection_ref: str = field(repr=False)
    vendor_ref: str = field(repr=False)
    reviews: tuple[FixtureReturnReview, ...] = field(repr=False)
    external_write_authorized: bool = field(default=False, init=False)

    @property
    def approval_digest(self) -> str:
        return _digest(asdict(self))

    @property
    def fixture_amount_krw(self) -> int:
        return sum(review.fixture_amount_krw for review in self.reviews)


@dataclass(frozen=True)
class FixtureReturnBatchResult:
    decision: str
    resend_authorized: bool = field(default=False, init=False)
    bank_refund_verified: bool = field(default=False, init=False)
    real_return_confirmed: bool = field(default=False, init=False)


def build_return_batch(reviews: tuple[FixtureReturnReview, ...], *, tenant_ref: str,
                       connection_ref: str, now: datetime) -> FixtureReturnBatch:
    if not isinstance(reviews, tuple) or not 1 <= len(reviews) <= 30:
        raise ContractQuarantine("bounded_return_batch_required")
    if any(type(review) is not FixtureReturnReview for review in reviews):
        raise ContractQuarantine("return_review_required")
    vendor_ref = reviews[0].vendor_ref
    receipts, identities = set(), set()
    for review in reviews:
        verify_return_fixture_review(review, approval_digest=review.approval_digest,
                                     tenant_ref=tenant_ref, connection_ref=connection_ref, now=now)
        if review.vendor_ref != vendor_ref:
            raise ContractQuarantine("mixed_return_vendor")
        identity = (review.order_id, review.vendor_item_id)
        if review.receipt_id in receipts or identity in identities:
            raise ContractQuarantine("duplicate_return_batch_identity")
        receipts.add(review.receipt_id); identities.add(identity)
    return FixtureReturnBatch(tenant_ref, connection_ref, vendor_ref, reviews)


def verify_return_batch(batch: FixtureReturnBatch, *, approval_digest: str,
                        tenant_ref: str, connection_ref: str, now: datetime) -> str:
    if type(batch) is not FixtureReturnBatch:
        raise ContractQuarantine("return_batch_required")
    rebuilt = build_return_batch(batch.reviews, tenant_ref=tenant_ref,
                                 connection_ref=connection_ref, now=now)
    if _hash_ref(approval_digest) != batch.approval_digest or rebuilt != batch:
        raise ContractQuarantine("batch_approval_digest_mismatch")
    return "FIXTURE_REVIEW_ONLY"


def reconcile_return_batch(batch: FixtureReturnBatch,
                           results: Mapping[str, FixtureReturnResult]) -> FixtureReturnBatchResult:
    """Correlate caller-observed local fixture outcomes; never infer a refund."""
    if type(batch) is not FixtureReturnBatch or not isinstance(results, Mapping):
        return FixtureReturnBatchResult("RECONCILE_REQUIRED")
    expected = {review.receipt_id for review in batch.reviews}
    if set(results) != expected or any(type(value) is not FixtureReturnResult for value in results.values()):
        return FixtureReturnBatchResult("RECONCILE_REQUIRED")
    if any(value.decision != "MATCHED_COMPLETED_FIXTURE" for value in results.values()):
        return FixtureReturnBatchResult("RECONCILE_REQUIRED")
    return FixtureReturnBatchResult("MATCHED_COMPLETED_BATCH")
