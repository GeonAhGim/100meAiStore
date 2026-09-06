"""Non-executable single-receipt return review. No refund/payment authority."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .channel_claim_contracts import OfflineClaimLine, OfflineClaimPage
from .channel_order_contracts import ContractQuarantine, _integer
from .offline_tracking_contracts import _aware, _digest, _hash_ref, _ref


@dataclass(frozen=True)
class FixtureReturnReview:
    tenant_ref: str = field(repr=False)
    connection_ref: str = field(repr=False)
    vendor_ref: str = field(repr=False)
    receipt_id: str = field(repr=False)
    order_id: str = field(repr=False)
    shipment_id: str = field(repr=False)
    vendor_item_id: str = field(repr=False)
    cancel_quantity: int
    purchase_quantity: int
    fixture_amount_krw: int
    fixture_amount_cap_krw: int
    source_digest: str = field(repr=False)
    withdrawal_review_digest: str = field(repr=False)
    observed_at: str
    withdrawal_observed_at: str
    created_at: str
    expires_at: str
    mode: str = field(default="OFFLINE_CONTRACT", init=False)
    external_write_authorized: bool = field(default=False, init=False)

    @property
    def approval_digest(self) -> str:
        return _digest(asdict(self))


def _one_receipt(page: OfflineClaimPage, receipt_id: str) -> OfflineClaimLine:
    if not isinstance(page, OfflineClaimPage) or page.next_cursor is not None:
        raise ContractQuarantine("complete_claim_fixture_required")
    lines = [line for line in page.lines if line.receipt_id == receipt_id]
    if len(lines) != 1:
        raise ContractQuarantine("single_exact_receipt_line_required")
    return lines[0]


def build_coupang_return_review(
    page: OfflineClaimPage, *, tenant_ref: str, connection_ref: str, vendor_ref: str,
    receipt_id: str, order_id: str, shipment_id: str, vendor_item_id: str,
    cancel_quantity: int, fixture_amount_krw: int, fixture_amount_cap_krw: int,
    withdrawal_review_digest: str, withdrawal_reviewed: bool,
    observed_at: datetime, withdrawal_observed_at: datetime, now: datetime,
    expires_at: datetime, max_age_seconds: int = 300,
) -> FixtureReturnReview:
    refs = tuple(map(_ref, (tenant_ref, connection_ref, vendor_ref, receipt_id,
                           order_id, shipment_id, vendor_item_id)))
    line = _one_receipt(page, receipt_id)
    if (line.order_id, line.shipment_id, line.vendor_item_id) != (order_id, shipment_id, vendor_item_id):
        raise ContractQuarantine("return_identity_mismatch")
    if (line.receipt_type != "RETURN" or line.receipt_status != "VENDOR_WAREHOUSE_CONFIRM"
            or line.pre_refund_observed is not False):
        raise ContractQuarantine("return_state_requires_reconciliation")
    if _integer(cancel_quantity, positive=True) != line.cancel_quantity:
        raise ContractQuarantine("return_quantity_mismatch")
    fixture_amount_krw = _integer(fixture_amount_krw, positive=True)
    fixture_amount_cap_krw = _integer(fixture_amount_cap_krw, positive=True)
    if fixture_amount_krw > fixture_amount_cap_krw:
        raise ContractQuarantine("fixture_return_amount_cap_exceeded")
    if withdrawal_reviewed is not True:
        raise ContractQuarantine("withdrawal_review_required")
    withdrawal_review_digest = _hash_ref(withdrawal_review_digest)
    now, observed_at, withdrawal_observed_at, expires_at = map(
        _aware, (now, observed_at, withdrawal_observed_at, expires_at))
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 3600:
        raise ContractQuarantine("invalid_freshness_policy")
    for observed in (observed_at, withdrawal_observed_at):
        if not timedelta(0) <= now - observed < timedelta(seconds=max_age_seconds):
            raise ContractQuarantine("stale_or_future_claim_observation")
        if not now < expires_at <= observed + timedelta(seconds=max_age_seconds):
            raise ContractQuarantine("invalid_review_expiry")
    return FixtureReturnReview(*refs, cancel_quantity, line.purchase_quantity,
        fixture_amount_krw, fixture_amount_cap_krw, _hash_ref(page.source_digest),
        withdrawal_review_digest, observed_at.isoformat(), withdrawal_observed_at.isoformat(),
        now.isoformat(), expires_at.isoformat())


def verify_return_fixture_review(plan: FixtureReturnReview, *, approval_digest: str,
                                 tenant_ref: str, connection_ref: str, now: datetime) -> str:
    if not isinstance(plan, FixtureReturnReview):
        raise ContractQuarantine("return_fixture_required")
    if (tenant_ref, connection_ref) != (plan.tenant_ref, plan.connection_ref):
        raise ContractQuarantine("approval_scope_mismatch")
    if _hash_ref(approval_digest) != plan.approval_digest:
        raise ContractQuarantine("approval_digest_mismatch")
    if not datetime.fromisoformat(plan.created_at) <= _aware(now) < datetime.fromisoformat(plan.expires_at):
        raise ContractQuarantine("approval_time_invalid")
    return "FIXTURE_REVIEW_ONLY"


@dataclass(frozen=True)
class FixtureReturnResult:
    decision: str
    resend_authorized: bool = field(default=False, init=False)
    bank_refund_verified: bool = field(default=False, init=False)
    real_return_confirmed: bool = field(default=False, init=False)


def reconcile_return_fixture(plan: FixtureReturnReview, response: Any, *,
                             readback: OfflineClaimPage | None = None,
                             readback_observed_at: datetime | None = None,
                             now: datetime) -> FixtureReturnResult:
    if not isinstance(plan, FixtureReturnReview):
        raise ContractQuarantine("return_fixture_required")
    try:
        _digest(response)
        if (not isinstance(response, dict)
                or set(response) != {"code", "message"}
                or not ((type(response.get("code")) is int and response["code"] == 200)
                        or response.get("code") == "200")
                or response.get("message") != "OK"):
            return FixtureReturnResult("RECONCILE_REQUIRED")
        if readback is None:
            return FixtureReturnResult("READBACK_REQUIRED")
        line = _one_receipt(readback, plan.receipt_id)
        observed, now = map(_aware, (readback_observed_at, now))
        if not datetime.fromisoformat(plan.created_at) <= observed <= now < datetime.fromisoformat(plan.expires_at):
            return FixtureReturnResult("RECONCILE_REQUIRED")
        if (line.order_id, line.shipment_id, line.vendor_item_id, line.cancel_quantity, line.purchase_quantity) != (
                plan.order_id, plan.shipment_id, plan.vendor_item_id, plan.cancel_quantity, plan.purchase_quantity):
            return FixtureReturnResult("RECONCILE_REQUIRED")
        if line.receipt_type != "RETURN" or line.receipt_status != "RETURNS_COMPLETED":
            return FixtureReturnResult("RECONCILE_REQUIRED")
    except ContractQuarantine:
        return FixtureReturnResult("RECONCILE_REQUIRED")
    return FixtureReturnResult("MATCHED_COMPLETED_FIXTURE")
