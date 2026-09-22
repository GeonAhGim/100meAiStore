"""Bounded one-item Coupang listing fixtures. No I/O or external authority."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from .channel_order_contracts import ContractQuarantine, _integer, _object, _rows
from .errors import ConflictError
from .inventory import calculate_demo_price
from .offline_tracking_contracts import _aware, _digest, _hash_ref, _ref
from .supplier_file_contracts import SupplierImportReport


def _text(value: Any, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or not value.isprintable():
        raise ContractQuarantine("invalid_listing_text")
    return value


def _keys(body: dict, keys: set[str]) -> None:
    if set(body) != keys:
        raise ContractQuarantine("unsupported_or_missing_listing_field")


def _exact(body: dict, expected: dict) -> None:
    for key, value in expected.items():
        if type(body.get(key)) is not type(value) or body[key] != value:
            raise ContractQuarantine("unsupported_listing_variant")


@dataclass(frozen=True)
class FixtureListingReview:
    tenant_ref: str = field(repr=False)
    connection_ref: str = field(repr=False)
    vendor_id: str = field(repr=False)
    supplier_ref: str = field(repr=False)
    supplier_sku: str = field(repr=False)
    payload_digest: str = field(repr=False)
    supplier_digest: str = field(repr=False)
    metadata_digest: str = field(repr=False)
    rights_digest: str = field(repr=False)
    policy_digest: str = field(repr=False)
    price_krw: int
    quantity: int
    created_at: str
    expires_at: str
    mode: str = field(default="OFFLINE_CONTRACT", init=False)
    external_write_authorized: bool = field(default=False, init=False)

    @property
    def approval_digest(self) -> str:
        return _digest(asdict(self))


def build_coupang_listing_review(
    body: Any, report: SupplierImportReport, metadata: Any, *, tenant_ref: str,
    connection_ref: str, vendor_id: str, supplier_sku: str, rights_digest: str,
    now: datetime, observed_at: datetime, expires_at: datetime,
    reserved_quantity: int, safety_buffer: int, quantity_cap: int,
    variable_cost_minor: int, fee_rate: Decimal | str, max_age_seconds: int = 300,
) -> FixtureListingReview:
    """Metadata is a local fixture contract, not the vendor metadata response schema."""
    payload_digest, metadata_digest = _digest(body), _digest(metadata)
    tenant_ref, connection_ref, vendor_id, supplier_sku = map(
        _ref, (tenant_ref, connection_ref, vendor_id, supplier_sku))
    rights_digest = _hash_ref(rights_digest)
    body, metadata = _object(body), _object(metadata)
    _keys(body, set("displayCategoryCode sellerProductName vendorId saleStartedAt saleEndedAt "
                    "deliveryMethod deliveryCompanyCode deliveryChargeType deliveryCharge "
                    "freeShipOverAmount deliveryChargeOnReturn remoteAreaDeliverable unionDeliveryType "
                    "returnCenterCode returnChargeName companyContactNumber returnZipCode returnAddress "
                    "returnAddressDetail returnCharge outboundShippingPlaceCode vendorUserId requested items".split()))
    _exact(body, {"vendorId": vendor_id, "deliveryMethod": "SEQUENCIAL",
                  "deliveryCompanyCode": "KDEXP", "deliveryChargeType": "FREE",
                  "deliveryCharge": 0, "freeShipOverAmount": 0, "remoteAreaDeliverable": "N",
                  "unionDeliveryType": "NOT_UNION_DELIVERY", "requested": False})
    _text(body["sellerProductName"], 100)
    for key in ("returnCenterCode", "returnChargeName", "companyContactNumber", "returnZipCode",
                "returnAddress", "returnAddressDetail", "vendorUserId"):
        _text(body[key])
    _integer(body["outboundShippingPlaceCode"], positive=True)
    outbound_fee = _integer(body["deliveryChargeOnReturn"], positive=True)
    return_fee = _integer(body["returnCharge"], positive=True)
    if not outbound_fee <= return_fee or return_fee * 2 > outbound_fee * 3:
        raise ContractQuarantine("invalid_fixture_return_charge")
    dates = []
    for key in ("saleStartedAt", "saleEndedAt"):
        raw = _text(body[key], 19)
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            raise ContractQuarantine("invalid_listing_sale_window") from None
        if parsed.strftime("%Y-%m-%dT%H:%M:%S") != raw:
            raise ContractQuarantine("invalid_listing_sale_window")
        dates.append(parsed)
    if not dates[0] < dates[1] or dates[1].year > 2099:
        raise ContractQuarantine("invalid_listing_sale_window")

    _keys(metadata, {"fixtureSchema", "categoryCode", "requiredAttributes", "noticeCategory", "requiredNotices", "certificationRequired"})
    _exact(metadata, {"fixtureSchema": "listing-category-v1", "certificationRequired": False})
    if _integer(body["displayCategoryCode"], positive=True) != _integer(metadata["categoryCode"], positive=True):
        raise ContractQuarantine("listing_category_mismatch")
    required_attributes = tuple(_text(name, 25) for name in _rows(metadata["requiredAttributes"], 50))
    required_notices = tuple(_text(name, 200) for name in _rows(metadata["requiredNotices"], 50))
    if (not required_attributes or not required_notices
            or len(set(required_attributes)) != len(required_attributes)
            or len(set(required_notices)) != len(required_notices)):
        raise ContractQuarantine("invalid_fixture_category_requirements")
    items = _rows(body["items"], 1)
    if len(items) != 1:
        raise ContractQuarantine("single_listing_item_required")
    item = _object(items[0])
    _keys(item, set("itemName originalPrice salePrice maximumBuyCount maximumBuyForPerson "
                    "maximumBuyForPersonPeriod outboundShippingTimeDay unitCount adultOnly taxType "
                    "parallelImported overseasPurchased pccNeeded externalVendorSku images attributes "
                    "notices contents certifications offerCondition".split()))
    _exact(item, {"maximumBuyForPerson": 0, "maximumBuyForPersonPeriod": 1,
                  "unitCount": 1, "adultOnly": "EVERYONE", "taxType": "TAX",
                  "parallelImported": "NOT_PARALLEL_IMPORTED", "overseasPurchased": "NOT_OVERSEAS_PURCHASED",
                  "pccNeeded": False, "externalVendorSku": supplier_sku, "offerCondition": "NEW"})
    _text(item["itemName"], 150)
    _integer(item["outboundShippingTimeDay"], positive=True)
    price = _integer(item["salePrice"], positive=True)
    quantity = _integer(item["maximumBuyCount"], positive=True)
    if _integer(item["originalPrice"], positive=True) != price or quantity > 99999 or price % 10:
        raise ContractQuarantine("unsupported_listing_price_or_quantity")
    if item["certifications"] != [{"certificationType": "NOT_REQUIRED", "certificationCode": ""}]:
        raise ContractQuarantine("unsupported_listing_certification")
    attributes = _rows(item["attributes"], 50)
    names = []
    for attribute in attributes:
        attribute = _object(attribute)
        _keys(attribute, {"attributeTypeName", "attributeValueName"})
        names.append(_text(attribute["attributeTypeName"], 25))
        _text(attribute["attributeValueName"], 30)
    if len(set(names)) != len(names) or set(names) != set(required_attributes):
        raise ContractQuarantine("listing_attribute_coverage_mismatch")
    names = []
    for notice in _rows(item["notices"], 50):
        notice = _object(notice)
        _keys(notice, {"noticeCategoryName", "noticeCategoryDetailName", "content"})
        if _text(notice["noticeCategoryName"]) != _text(metadata["noticeCategory"]):
            raise ContractQuarantine("listing_notice_category_mismatch")
        names.append(_text(notice["noticeCategoryDetailName"]))
        _text(notice["content"], 2000)
    if len(set(names)) != len(names) or set(names) != set(required_notices):
        raise ContractQuarantine("listing_notice_coverage_mismatch")
    images = _rows(item["images"], 1)
    if len(images) != 1:
        raise ContractQuarantine("fixture_representation_image_required")
    picture = _object(images[0])
    _keys(picture, {"imageOrder", "imageType", "vendorPath"})
    _exact(picture, {"imageOrder": 0, "imageType": "REPRESENTATION"})
    # Reserved invalid host makes this a synthetic image reference, never fetched.
    if _text(picture["vendorPath"]) != "https://fixture.invalid/product.png":
        raise ContractQuarantine("synthetic_image_reference_required")
    contents = _rows(item["contents"], 1)
    if len(contents) != 1:
        raise ContractQuarantine("fixture_text_content_required")
    content = _object(contents[0])
    _keys(content, {"contentsType", "contentDetails"})
    _exact(content, {"contentsType": "TEXT"})
    details = _rows(content["contentDetails"], 1)
    if len(details) != 1:
        raise ContractQuarantine("fixture_text_content_required")
    detail = _object(details[0])
    _keys(detail, {"detailType", "content"})
    _exact(detail, {"detailType": "TEXT"})
    if any(char in _text(detail["content"], 2000) for char in "<>"):
        raise ContractQuarantine("fixture_plain_text_required")

    if not isinstance(report, SupplierImportReport) or not report.ready:
        raise ContractQuarantine("ready_supplier_report_required")
    rows = [row for row in report.rows if row.supplier_sku == supplier_sku and row.currency == "KRW"]
    if len(rows) != 1:
        raise ContractQuarantine("supplier_row_unavailable")
    row = rows[0]
    now, observed_at, expires_at = map(_aware, (now, observed_at, expires_at))
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 3600:
        raise ContractQuarantine("invalid_freshness_policy")
    try:
        supplier_time = _aware(datetime.fromisoformat(row.observed_at.replace("Z", "+00:00")))
    except (AttributeError, ValueError):
        raise ContractQuarantine("invalid_supplier_timestamp") from None
    for observed in (observed_at, supplier_time):
        if not timedelta(0) <= now - observed < timedelta(seconds=max_age_seconds):
            raise ContractQuarantine("stale_or_future_observation")
        if not now < expires_at <= observed + timedelta(seconds=max_age_seconds):
            raise ContractQuarantine("invalid_review_expiry")
    reserved_quantity, safety_buffer, quantity_cap = map(_integer, (reserved_quantity, safety_buffer, quantity_cap))
    if quantity > min(quantity_cap, max(0, row.quantity - reserved_quantity - safety_buffer)):
        raise ContractQuarantine("quantity_exceeds_fixture_availability")
    try:
        projection = calculate_demo_price(price, row.unit_cost_minor, variable_cost_minor, fee_rate)
    except ConflictError:
        raise ContractQuarantine("invalid_fixture_cost_policy") from None
    if projection.status != "READY":
        raise ContractQuarantine("projected_margin_below_threshold")
    policy_digest = _digest({"reserved": reserved_quantity, "buffer": safety_buffer, "cap": quantity_cap,
                             "variable_cost": variable_cost_minor, "fee_rate": str(projection.fee_rate),
                             "age": max_age_seconds, "observed_at": observed_at.isoformat(),
                             "supplier_row_digest": _hash_ref(row.row_digest)})
    return FixtureListingReview(tenant_ref, connection_ref, vendor_id, report.supplier_ref, supplier_sku,
        payload_digest, _hash_ref(report.source_digest), metadata_digest, rights_digest, policy_digest,
        price, quantity, now.isoformat(), expires_at.isoformat())


def verify_listing_fixture_review(plan: FixtureListingReview, *, approval_digest: str,
                                  tenant_ref: str, connection_ref: str, now: datetime) -> str:
    if not isinstance(plan, FixtureListingReview):
        raise ContractQuarantine("listing_fixture_required")
    if (tenant_ref, connection_ref) != (plan.tenant_ref, plan.connection_ref):
        raise ContractQuarantine("approval_scope_mismatch")
    if _hash_ref(approval_digest) != plan.approval_digest:
        raise ContractQuarantine("approval_digest_mismatch")
    if not datetime.fromisoformat(plan.created_at) <= _aware(now) < datetime.fromisoformat(plan.expires_at):
        raise ContractQuarantine("approval_time_invalid")
    return "FIXTURE_REVIEW_ONLY"


@dataclass(frozen=True)
class FixtureListingResult:
    decision: str
    seller_product_id: int | None = field(default=None, repr=False)
    resend_authorized: bool = field(default=False, init=False)
    real_listing_confirmed: bool = field(default=False, init=False)


def interpret_listing_creation_fixture(response: Any) -> FixtureListingResult:
    """A creation ID requires future exact-ID readback; never declares sale readiness."""
    try:
        _digest(response)
        body = _object(response)
        if body.get("code") == "200":
            if (body.get("message") not in (None, "")
                    or body.get("details") not in (None, "")
                    or body.get("errorItems") not in (None, [])
                    or body.get("ettorItems") not in (None, [])):
                return FixtureListingResult("RECONCILE_REQUIRED")
            body = _object(body.get("data"))
        if (body.get("code") != "SUCCESS" or body.get("message") not in (None, "", "[]")
                or body.get("details") not in (None, "")
                or body.get("errorItems") not in (None, [])
                or body.get("ettorItems") not in (None, [])):
            return FixtureListingResult("RECONCILE_REQUIRED")
        product_id = _integer(body.get("data"), positive=True)
    except ContractQuarantine:
        return FixtureListingResult("RECONCILE_REQUIRED")
    return FixtureListingResult("CREATED_ID_REQUIRES_READBACK", product_id)


@dataclass(frozen=True)
class FixtureListingReadback:
    decision: str
    seller_product_id: int | None = field(default=None, repr=False)
    vendor_item_id: int | None = field(default=None, repr=False)
    source_digest: str | None = field(default=None, repr=False)
    resend_authorized: bool = field(default=False, init=False)
    real_listing_confirmed: bool = field(default=False, init=False)


def reconcile_listing_fixture(plan: FixtureListingReview, creation_response: Any, *,
                              original_payload: Any, readback: Any,
                              observed_at: datetime, now: datetime) -> FixtureListingReadback:
    if not isinstance(plan, FixtureListingReview):
        raise ContractQuarantine("listing_fixture_required")
    try:
        if _digest(original_payload) != plan.payload_digest:
            raise ContractQuarantine("listing_payload_digest_mismatch")
        receipt = interpret_listing_creation_fixture(creation_response)
        if receipt.seller_product_id is None:
            return FixtureListingReadback("RECONCILE_REQUIRED")
        observed_at, now = map(_aware, (observed_at, now))
        if not datetime.fromisoformat(plan.created_at) <= observed_at <= now < datetime.fromisoformat(plan.expires_at):
            return FixtureListingReadback("RECONCILE_REQUIRED")
        source_digest = _digest(readback)
        body = _object(readback)
        if (body.get("code") != "SUCCESS" or body.get("message") not in (None, "", "OK")
                or body.get("details") not in (None, "") or body.get("errorItems") not in (None, [])):
            return FixtureListingReadback("RECONCILE_REQUIRED")
        data = _object(body.get("data"))
        if _integer(data.get("sellerProductId"), positive=True) != receipt.seller_product_id:
            return FixtureListingReadback("RECONCILE_REQUIRED")
        status = data.get("statusName")
        if status not in ("임시저장", "승인완료"):
            return FixtureListingReadback("RECONCILE_REQUIRED")
        submitted = _object(original_payload)
        for key, value in submitted.items():
            if key != "items" and _digest(data.get(key)) != _digest(value):
                return FixtureListingReadback("RECONCILE_REQUIRED")
        items = _rows(data.get("items"), 1)
        if len(items) != 1:
            return FixtureListingReadback("RECONCILE_REQUIRED")
        item = _object(items[0])
        for key, value in submitted["items"][0].items():
            if _digest(item.get(key)) != _digest(value):
                return FixtureListingReadback("RECONCILE_REQUIRED")
        if (item.get("autoPricingInfoView") not in (None, {})
                or item.get("isAutoGenerated") not in (None, "false")):
            return FixtureListingReadback("RECONCILE_REQUIRED")
        _integer(item.get("sellerProductItemId"), positive=True)
        vendor_item_id = item.get("vendorItemId")
        if status == "임시저장":
            if vendor_item_id is not None:
                return FixtureListingReadback("RECONCILE_REQUIRED")
            decision = "MATCHED_DRAFT_FIXTURE"
        else:
            vendor_item_id = _integer(vendor_item_id, positive=True)
            decision = "MATCHED_APPROVED_FIXTURE"
    except ContractQuarantine:
        return FixtureListingReadback("RECONCILE_REQUIRED")
    return FixtureListingReadback(decision, receipt.seller_product_id, vendor_item_id, source_digest)
