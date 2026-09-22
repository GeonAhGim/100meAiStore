"""Synthetic single-item price/quantity reviews. No transport or live authority."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from .channel_order_contracts import ContractQuarantine, _integer
from .errors import ConflictError
from .inventory import calculate_demo_price
from .offline_tracking_contracts import _aware, _digest, _hash_ref, _ref
from .supplier_file_contracts import SupplierImportReport


@dataclass(frozen=True)
class FixtureCatalogReview:
    tenant_ref: str = field(repr=False)
    connection_ref: str = field(repr=False)
    vendor_item_id: int = field(repr=False)
    supplier_ref: str = field(repr=False)
    supplier_sku: str = field(repr=False)
    kind: str
    before_value: int
    proposed_value: int
    source_digest: str = field(repr=False)
    supplier_digest: str = field(repr=False)
    mapping_digest: str = field(repr=False)
    policy_digest: str = field(repr=False)
    created_at: str
    expires_at: str
    mode: str = field(default="OFFLINE_CONTRACT", init=False)
    force_sale_price_update: bool = field(default=False, init=False)
    external_write_authorized: bool = field(default=False, init=False)

    @property
    def approval_digest(self) -> str:
        return _digest(asdict(self))


def _inventory(body: Any, expected_id: int) -> tuple[int, int, bool]:
    _integer(expected_id, positive=True)
    if not isinstance(body, dict) or body.get("code") != "SUCCESS" or not isinstance(body.get("data"), dict):
        raise ContractQuarantine("invalid_catalog_response")
    data = body["data"]
    if _integer(data.get("sellerItemId"), positive=True) != expected_id:
        raise ContractQuarantine("catalog_identity_mismatch")
    quantity = _integer(data.get("amountInStock"))
    price = _integer(data.get("salePrice"), positive=True)
    if type(data.get("onSale")) is not bool:
        raise ContractQuarantine("invalid_catalog_sale_state")
    return quantity, price, data["onSale"]


def build_coupang_catalog_review(
    body: Any, report: SupplierImportReport, *, tenant_ref: str, connection_ref: str,
    vendor_item_id: int, supplier_sku: str, mapping_digest: str, kind: str,
    proposed_value: int, channel_observed_at: datetime, now: datetime, expires_at: datetime,
    reserved_quantity: int, safety_buffer: int, quantity_cap: int,
    variable_cost_minor: int, fee_rate: Decimal | str, max_age_seconds: int = 300,
) -> FixtureCatalogReview:
    tenant_ref, connection_ref, supplier_sku = map(_ref, (tenant_ref, connection_ref, supplier_sku))
    mapping_digest = _hash_ref(mapping_digest)
    if not isinstance(kind, str) or kind not in {"PRICE", "QUANTITY"}:
        raise ContractQuarantine("unsupported_catalog_change")
    proposed_value = _integer(proposed_value)
    if not isinstance(report, SupplierImportReport) or not report.ready:
        raise ContractQuarantine("ready_supplier_report_required")
    rows = [row for row in report.rows if row.supplier_sku == supplier_sku]
    if len(rows) != 1 or rows[0].currency != "KRW":
        raise ContractQuarantine("supplier_row_unavailable")
    row = rows[0]
    now, channel_observed_at, expires_at = map(_aware, (now, channel_observed_at, expires_at))
    try:
        supplier_observed_at = _aware(datetime.fromisoformat(row.observed_at.replace("Z", "+00:00")))
    except (AttributeError, ValueError):
        raise ContractQuarantine("invalid_supplier_timestamp") from None
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 3600:
        raise ContractQuarantine("invalid_freshness_policy")
    for observed in (channel_observed_at, supplier_observed_at):
        if not timedelta(0) <= now - observed < timedelta(seconds=max_age_seconds):
            raise ContractQuarantine("stale_or_future_observation")
        if not now < expires_at <= observed + timedelta(seconds=max_age_seconds):
            raise ContractQuarantine("invalid_review_expiry")
    stock, price, on_sale = _inventory(body, vendor_item_id)
    if not on_sale:
        raise ContractQuarantine("stopped_listing_requires_review")
    reserved_quantity, safety_buffer, quantity_cap = map(_integer, (reserved_quantity, safety_buffer, quantity_cap))
    available = max(0, row.quantity - reserved_quantity - safety_buffer)
    proposed_price = proposed_value if kind == "PRICE" else price
    if kind == "PRICE" and (proposed_value <= 0 or proposed_value % 10 or
                             proposed_value * 2 < price or proposed_value > price * 2):
        raise ContractQuarantine("price_step_or_default_range_violation")
    if kind == "QUANTITY" and proposed_value > min(available, quantity_cap):
        raise ContractQuarantine("quantity_exceeds_fixture_availability")
    try:
        projection = calculate_demo_price(proposed_price, row.unit_cost_minor, variable_cost_minor, fee_rate)
    except ConflictError:
        raise ContractQuarantine("invalid_fixture_cost_policy") from None
    if projection.status != "READY":
        raise ContractQuarantine("projected_margin_below_threshold")
    policy_digest = _digest({"reserved_quantity": reserved_quantity, "safety_buffer": safety_buffer,
                             "quantity_cap": quantity_cap, "max_age_seconds": max_age_seconds,
                             "variable_cost_minor": variable_cost_minor, "fee_rate": str(projection.fee_rate),
                             "supplier_row_digest": row.row_digest})
    return FixtureCatalogReview(tenant_ref, connection_ref, vendor_item_id, report.supplier_ref,
        supplier_sku, kind, price if kind == "PRICE" else stock, proposed_value,
        _digest(body), _hash_ref(report.source_digest), mapping_digest, policy_digest,
        now.isoformat(), expires_at.isoformat())


def verify_catalog_fixture_review(plan: FixtureCatalogReview, *, approval_digest: str,
                                  tenant_ref: str, connection_ref: str, now: datetime) -> str:
    if not isinstance(plan, FixtureCatalogReview):
        raise ContractQuarantine("catalog_fixture_required")
    if (tenant_ref, connection_ref) != (plan.tenant_ref, plan.connection_ref):
        raise ContractQuarantine("approval_scope_mismatch")
    if _hash_ref(approval_digest) != plan.approval_digest:
        raise ContractQuarantine("approval_digest_mismatch")
    if not datetime.fromisoformat(plan.created_at) <= _aware(now) < datetime.fromisoformat(plan.expires_at):
        raise ContractQuarantine("approval_time_invalid")
    return "FIXTURE_REVIEW_ONLY"


@dataclass(frozen=True)
class FixtureCatalogResult:
    decision: str
    resend_authorized: bool = field(default=False, init=False)
    real_change_confirmed: bool = field(default=False, init=False)


def reconcile_catalog_fixture(plan: FixtureCatalogReview, response: Any, *, readback: Any = None) -> FixtureCatalogResult:
    if not isinstance(plan, FixtureCatalogReview):
        raise ContractQuarantine("catalog_fixture_required")
    _digest(response)
    if not isinstance(response, dict) or response.get("code") != "SUCCESS":
        return FixtureCatalogResult("RECONCILE_REQUIRED")
    if readback is None:
        return FixtureCatalogResult("READBACK_REQUIRED")
    try:
        _digest(readback)
        quantity, price, on_sale = _inventory(readback, plan.vendor_item_id)
    except ContractQuarantine:
        return FixtureCatalogResult("RECONCILE_REQUIRED")
    actual = price if plan.kind == "PRICE" else quantity
    return FixtureCatalogResult("MATCHED_READBACK_FIXTURE" if on_sale and actual == plan.proposed_value
                                else "RECONCILE_REQUIRED")
