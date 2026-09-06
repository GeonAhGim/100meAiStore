"""Synthetic, non-executable single-item tracking review contracts. No I/O."""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .channel_order_contracts import (ContractQuarantine, OfflineOrderPage, canonical_json,
                                     parse_naver_details)


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _ref(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,149}", value):
        raise ContractQuarantine("invalid_fixture_reference")
    return value


def _hash_ref(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ContractQuarantine("invalid_evidence_digest")
    return value


def _aware(value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ContractQuarantine("aware_timestamp_required")
    return value


@dataclass(frozen=True)
class FixtureTrackingReview:
    tenant_ref: str = field(repr=False)
    connection_ref: str = field(repr=False)
    order_id: str = field(repr=False)
    shipment_id: str = field(repr=False)
    vendor_item_id: str = field(repr=False)
    invoice_number: str = field(repr=False)
    source_digest: str = field(repr=False)
    preparation_review_digest: str = field(repr=False)
    observed_at: str
    review_expires_at: str
    carrier: str = "KDEXP"
    split_shipping: bool = field(default=False, init=False)
    pre_split_shipped: bool = field(default=False, init=False)
    estimated_shipping_date: str = field(default="", init=False)
    mode: str = field(default="OFFLINE_CONTRACT", init=False)
    external_write_authorized: bool = field(default=False, init=False)

    @property
    def approval_digest(self) -> str:
        return _digest(asdict(self))


@dataclass(frozen=True)
class FixtureNaverDispatchReview:
    tenant_ref: str = field(repr=False)
    connection_ref: str = field(repr=False)
    product_order_id: str = field(repr=False)
    invoice_number: str = field(repr=False)
    source_digest: str = field(repr=False)
    observed_at: str
    review_expires_at: str
    dispatch_date: str
    delivery_method: str = field(default="DELIVERY", init=False)
    carrier: str = field(default="CJGLS", init=False)
    mode: str = field(default="OFFLINE_CONTRACT", init=False)
    external_write_authorized: bool = field(default=False, init=False)

    @property
    def approval_digest(self) -> str:
        return _digest(asdict(self))


def build_naver_dispatch_review(body: Any, *, tenant_ref: str, connection_ref: str,
                                product_order_id: str, invoice_number: str,
                                observed_at: datetime, now: datetime,
                                review_expires_at: datetime, dispatch_date: datetime,
                                max_age_seconds: int = 300) -> FixtureNaverDispatchReview:
    refs = tuple(map(_ref, (tenant_ref, connection_ref, product_order_id)))
    page = parse_naver_details(body, requested_ids=(product_order_id,))
    now, observed_at, review_expires_at, dispatch_date = map(
        _aware, (now, observed_at, review_expires_at, dispatch_date))
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 3600:
        raise ContractQuarantine("invalid_freshness_policy")
    if not timedelta(0) <= now - observed_at < timedelta(seconds=max_age_seconds):
        raise ContractQuarantine("stale_or_future_observation")
    if not now < review_expires_at <= observed_at + timedelta(seconds=max_age_seconds):
        raise ContractQuarantine("invalid_review_expiry")
    if not isinstance(invoice_number, str) or not re.fullmatch(r"[0-9]{1,40}", invoice_number):
        raise ContractQuarantine("invalid_fixture_invoice")
    line = page.snapshots[0]
    product = body["data"][0]["productOrder"]
    if line.status != "PAYED" or line.claim_review_required or line.remaining_quantity <= 0:
        raise ContractQuarantine("shipment_state_review_required")
    if product.get("placeOrderStatus") != "OK":
        raise ContractQuarantine("order_confirmation_required")
    try:
        confirmed_at = datetime.fromisoformat(product["placeOrderDate"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, TypeError, ValueError):
        raise ContractQuarantine("invalid_order_confirmation_time") from None
    _aware(confirmed_at)
    if not confirmed_at <= dispatch_date <= observed_at:
        raise ContractQuarantine("dispatch_time_invalid")
    return FixtureNaverDispatchReview(*refs, invoice_number, page.source_digest,
                                      observed_at.isoformat(), review_expires_at.isoformat(),
                                      dispatch_date.isoformat())


def build_coupang_tracking_review(
    page: OfflineOrderPage, *, tenant_ref: str, connection_ref: str,
    order_id: str, shipment_id: str, vendor_item_id: str, invoice_number: str,
    observed_at: datetime, now: datetime, review_expires_at: datetime,
    preparation_review_digest: str, post_preparation_reviewed: bool,
    max_age_seconds: int = 300,
) -> FixtureTrackingReview:
    """300 seconds is local fixture policy, not a vendor freshness guarantee."""
    if not isinstance(page, OfflineOrderPage) or page.provider != "coupang":
        raise ContractQuarantine("coupang_page_required")
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 3600:
        raise ContractQuarantine("invalid_freshness_policy")
    now, observed_at, review_expires_at = map(_aware, (now, observed_at, review_expires_at))
    if not timedelta(0) <= now - observed_at < timedelta(seconds=max_age_seconds):
        raise ContractQuarantine("stale_or_future_observation")
    if not now < review_expires_at <= observed_at + timedelta(seconds=max_age_seconds):
        raise ContractQuarantine("invalid_review_expiry")
    if post_preparation_reviewed is not True:
        raise ContractQuarantine("post_preparation_review_required")
    refs = tuple(map(_ref, (tenant_ref, connection_ref, order_id, shipment_id, vendor_item_id)))
    if not isinstance(invoice_number, str) or not re.fullmatch(r"[0-9]{1,40}", invoice_number):
        raise ContractQuarantine("invalid_fixture_invoice")
    # Full shipment coverage is intentionally restricted to one normalized line.
    lines = [line for line in page.snapshots if line.shipment_id == shipment_id]
    if len(lines) != 1:
        raise ContractQuarantine("single_item_shipment_required")
    line = lines[0]
    if (line.provider, line.order_id, line.product_id) != ("coupang", order_id, vendor_item_id):
        raise ContractQuarantine("external_identity_mismatch")
    if line.status != "INSTRUCT" or line.claim_review_required or line.remaining_quantity <= 0:
        raise ContractQuarantine("shipment_state_review_required")
    return FixtureTrackingReview(*refs, invoice_number, _hash_ref(page.source_digest),
                                 _hash_ref(preparation_review_digest), observed_at.isoformat(),
                                 review_expires_at.isoformat())


def verify_fixture_review(plan: FixtureTrackingReview | FixtureNaverDispatchReview, *, approval_digest: str,
                          tenant_ref: str, connection_ref: str, now: datetime) -> str:
    if not isinstance(plan, (FixtureTrackingReview, FixtureNaverDispatchReview)):
        raise ContractQuarantine("fixture_review_required")
    _aware(now)
    if (tenant_ref, connection_ref) != (plan.tenant_ref, plan.connection_ref):
        raise ContractQuarantine("approval_scope_mismatch")
    if _hash_ref(approval_digest) != plan.approval_digest:
        raise ContractQuarantine("approval_digest_mismatch")
    if not datetime.fromisoformat(plan.observed_at) <= now < datetime.fromisoformat(plan.review_expires_at):
        raise ContractQuarantine("approval_time_invalid")
    return "FIXTURE_REVIEW_ONLY"


@dataclass(frozen=True)
class FixtureTrackingResult:
    decision: str
    response_digest: str | None = field(default=None, repr=False)
    resend_authorized: bool = field(default=False, init=False)
    real_shipment_confirmed: bool = field(default=False, init=False)


def interpret_coupang_tracking_fixture(plan: FixtureTrackingReview, body: Any) -> FixtureTrackingResult:
    """An OK fixture is not live confirmation; unknown results never authorize resend."""
    if not isinstance(plan, FixtureTrackingReview):
        raise ContractQuarantine("fixture_review_required")
    if body is None:
        return FixtureTrackingResult("RECONCILE_REQUIRED")
    digest = _digest(body)
    def result(decision: str) -> FixtureTrackingResult:
        return FixtureTrackingResult(decision, digest)
    if not isinstance(body, dict) or type(body.get("code")) not in {str, int} or body["code"] not in (200, "200"):
        return result("RECONCILE_REQUIRED")
    data = body.get("data")
    if not isinstance(data, dict) or type(data.get("responseCode")) is not int or data["responseCode"] != 0:
        return result("RECONCILE_REQUIRED")
    rows = data.get("responseList")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return result("RECONCILE_REQUIRED")
    row = rows[0]
    if type(row.get("shipmentBoxId")) is not int or str(row["shipmentBoxId"]) != plan.shipment_id:
        return result("RECONCILE_REQUIRED")
    if row.get("succeed") is True and row.get("resultCode") == "OK" and row.get("retryRequired") is False:
        return result("MATCHED_SUCCESS_FIXTURE")
    return result("RECONCILE_REQUIRED")


def interpret_naver_dispatch_fixture(plan: FixtureNaverDispatchReview, body: Any) -> FixtureTrackingResult:
    if not isinstance(plan, FixtureNaverDispatchReview):
        raise ContractQuarantine("naver_fixture_review_required")
    digest = None if body is None else _digest(body)
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        data = body["data"]
        if (data.get("successProductOrderIds") == [plan.product_order_id]
                and data.get("failProductOrderInfos") == []):
            return FixtureTrackingResult("MATCHED_SUCCESS_FIXTURE", digest)
    return FixtureTrackingResult("RECONCILE_REQUIRED", digest)
