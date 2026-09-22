"""Scoped batch composition of non-executable tracking review fixtures."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from .channel_order_contracts import ContractQuarantine, _integer, _object, _rows
from .offline_tracking_contracts import (
    FixtureNaverDispatchReview, FixtureTrackingReview, FixtureTrackingResult,
    _digest, _hash_ref, verify_fixture_review,
)


@dataclass(frozen=True)
class FixtureTrackingBatch:
    provider: str
    tenant_ref: str = field(repr=False)
    connection_ref: str = field(repr=False)
    reviews: tuple[FixtureTrackingReview | FixtureNaverDispatchReview, ...] = field(repr=False)
    external_write_authorized: bool = field(default=False, init=False)

    @property
    def approval_digest(self) -> str:
        return _digest(asdict(self))


def build_tracking_batch(reviews: tuple[FixtureTrackingReview | FixtureNaverDispatchReview, ...], *,
                         tenant_ref: str, connection_ref: str, now: datetime) -> FixtureTrackingBatch:
    if not isinstance(reviews, tuple) or not 1 <= len(reviews) <= 30:
        raise ContractQuarantine("bounded_tracking_batch_required")
    provider_type = type(reviews[0])
    if provider_type not in (FixtureTrackingReview, FixtureNaverDispatchReview):
        raise ContractQuarantine("tracking_review_required")
    seen = set()
    for review in reviews:
        if type(review) is not provider_type:
            raise ContractQuarantine("mixed_tracking_provider")
        verify_fixture_review(review, approval_digest=review.approval_digest,
                              tenant_ref=tenant_ref, connection_ref=connection_ref, now=now)
        identity = review.shipment_id if provider_type is FixtureTrackingReview else review.product_order_id
        if identity in seen:
            raise ContractQuarantine("duplicate_tracking_batch_identity")
        seen.add(identity)
    return FixtureTrackingBatch("coupang" if provider_type is FixtureTrackingReview else "naver",
                                tenant_ref, connection_ref, reviews)


def verify_tracking_batch(batch: FixtureTrackingBatch, *, approval_digest: str,
                          tenant_ref: str, connection_ref: str, now: datetime) -> str:
    if not isinstance(batch, FixtureTrackingBatch):
        raise ContractQuarantine("tracking_batch_required")
    rebuilt = build_tracking_batch(batch.reviews, tenant_ref=tenant_ref, connection_ref=connection_ref, now=now)
    if _hash_ref(approval_digest) != batch.approval_digest or rebuilt != batch:
        raise ContractQuarantine("batch_approval_digest_mismatch")
    return "FIXTURE_REVIEW_ONLY"


def interpret_tracking_batch(batch: FixtureTrackingBatch, body: Any) -> FixtureTrackingResult:
    if not isinstance(batch, FixtureTrackingBatch):
        raise ContractQuarantine("tracking_batch_required")
    digest = None
    try:
        digest = _digest(body)
        body = _object(body)
        data = _object(body.get("data"))
        if batch.provider == "naver":
            expected = {review.product_order_id for review in batch.reviews}
            ids = _rows(data.get("successProductOrderIds"), 30)
            if (any(not isinstance(value, str) for value in ids) or len(ids) != len(expected)
                    or len(set(ids)) != len(ids) or set(ids) != expected
                    or data.get("failProductOrderInfos") != []):
                return FixtureTrackingResult("RECONCILE_REQUIRED", digest)
        elif batch.provider == "coupang":
            if (type(body.get("code")) not in (str, int) or body["code"] not in (200, "200")
                    or type(data.get("responseCode")) is not int or data["responseCode"] != 0):
                return FixtureTrackingResult("RECONCILE_REQUIRED", digest)
            expected = {review.shipment_id for review in batch.reviews}
            rows = _rows(data.get("responseList"), 30)
            seen = set()
            for row in rows:
                row = _object(row)
                identity = str(_integer(row.get("shipmentBoxId"), positive=True))
                if (identity in seen or identity not in expected or row.get("succeed") is not True
                        or row.get("retryRequired") is not False or row.get("resultCode") != "OK"):
                    return FixtureTrackingResult("RECONCILE_REQUIRED", digest)
                seen.add(identity)
            if seen != expected:
                return FixtureTrackingResult("RECONCILE_REQUIRED", digest)
        else:
            return FixtureTrackingResult("RECONCILE_REQUIRED", digest)
    except (ContractQuarantine, AttributeError):
        return FixtureTrackingResult("RECONCILE_REQUIRED", digest)
    return FixtureTrackingResult("MATCHED_BATCH_FIXTURE", digest)
