"""PII-minimized offline settlement observations; never payment authority."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from .channel_order_contracts import (
    ContractQuarantine, _cursor, _hash, _identifier, _integer, _object, _rows, canonical_json,
)


NAVER_SETTLE_TYPES = frozenset({"NORMAL_SETTLE_ORIGINAL", "NORMAL_SETTLE_AFTER_CANCEL",
    "NORMAL_SETTLE_BEFORE_CANCEL", "QUICK_SETTLE_ORIGINAL", "QUICK_SETTLE_CANCEL",
    "QUANTITY_CANCEL_DEDUCTION", "QUANTITY_CANCEL_RESTORE"})
NAVER_TARGETS = frozenset({"PROD_ORDER", "DELIVERY", "EXTRAFEE", "WITHDRAW", "REFUND",
    "PL_REFUND", "DEDUCTION_RESTORE", "PROD_PAY", "PURCHASE_REVIEW", "PREMIUM_PURCHASE_REVIEW",
    "REGULAR_PURCHASE_REVIEW", "ONE_MONTH_PURCHASE_REVIEW", "ONE_MONTH_PREMIUM_PURCHASE_REVIEW",
    "REVIEW", "ETC_COUPON", "QUICK_SETTLE", "QUANTITY_CANCEL", "DIFFERENCE_SETTLE",
    "DEPOSIT_SETTLE", "RENTAL_ORDER", "MANUAL_ORDER", "RENTAL_SCHEDULED_ORDER",
    "PREFERENTIAL_COMMISSION", "POINT_ACCUMULATION", "POST_ORDER_ADJUSTMENT_AMOUNT", "CSF", "CONCESSION"})


def _signed(value: Any, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if type(value) is not int or not -(2**63) <= value <= 2**63 - 1:
        raise ContractQuarantine("integral_krw_amount_required")
    return value


def _date(value: Any, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ContractQuarantine("calendar_date_required") from None
    if parsed.isoformat() != value:
        raise ContractQuarantine("calendar_date_required")
    return value


def _optional_id(value: Any) -> str | None:
    return None if value is None else _identifier(value)


@dataclass(frozen=True)
class SettlementObservation:
    provider: str
    source_record_ref: str
    order_id: str | None
    product_ref: str | None
    kind: str
    dates: tuple[tuple[str, str | None], ...]
    amounts_krw: tuple[tuple[str, int | None], ...]
    expected_minor: int
    source_digest: str
    received_minor: None = field(default=None, init=False)
    cash_receipt_verified: bool = field(default=False, init=False)


@dataclass(frozen=True)
class OfflineSettlementPage:
    provider: str
    observations: tuple[SettlementObservation, ...]
    next_cursor: str | None = field(repr=False)
    warnings: tuple[str, ...]
    source_digest: str
    pagination: tuple[tuple[str, int], ...] = ()

    def canonical_payload(self) -> str:
        return canonical_json(asdict(self))


def parse_naver_settlement_page(body: Any) -> OfflineSettlementPage:
    digest = _hash(body)
    body = _object(body)
    rows = _rows(body.get("elements"), 1000)
    pagination = _object(body.get("pagination"))
    metadata = tuple((key, _integer(pagination.get(key))) for key in ("page", "size", "totalPages", "totalElements"))
    if not 1 <= pagination["size"] <= 1000 or len(rows) > pagination["size"] or len(rows) > pagination["totalElements"]:
        raise ContractQuarantine("invalid_settlement_pagination")
    observations, seen = [], set()
    for row in rows:
        row = _object(row)
        target, kind = row.get("productOrderType"), row.get("settleType")
        if not isinstance(target, str) or target not in NAVER_TARGETS or not isinstance(kind, str) or kind not in NAVER_SETTLE_TYPES:
            raise ContractQuarantine("unknown_settlement_kind")
        row_digest = _hash(row)
        if row_digest in seen:
            raise ContractQuarantine("duplicate_settlement_record")
        seen.add(row_digest)
        dates = tuple((key, _date(row.get(key), optional=True)) for key in
                      ("settleBasisDate", "settleExpectDate", "settleCompleteDate", "payDate"))
        amounts = tuple((key, _signed(row.get(key), optional=key not in
                         {"paySettleAmount", "benefitSettleAmount", "settleExpectAmount"})) for key in
                        ("paySettleAmount", "benefitSettleAmount", "settleExpectAmount",
                         "totalPayCommissionAmount", "freeInstallmentCommissionAmount", "sellingInterlockCommissionAmount"))
        observations.append(SettlementObservation("naver", row_digest,
            _optional_id(row.get("orderId")), _optional_id(row.get("productOrderId")), target + ":" + kind,
            dates, amounts, dict(amounts)["settleExpectAmount"], row_digest))
    # Preserve page metadata; response page indexing is not assumed to be request indexing.
    return OfflineSettlementPage("naver", tuple(observations), None,
                                  ("pagination_requires_request_context",), digest, metadata)


def parse_coupang_revenue_page(body: Any) -> OfflineSettlementPage:
    digest = _hash(body)
    body = _object(body)
    if type(body.get("code")) is not int or body["code"] != 200:
        raise ContractQuarantine("provider_response_error")
    if type(body.get("hasNext")) is not bool:
        raise ContractQuarantine("has_next_required")
    token = body.get("nextToken")
    if body["hasNext"]:
        token = _cursor(token)
    elif token not in (None, ""):
        raise ContractQuarantine("contradictory_revenue_cursor")
    else:
        token = None
    observations, warnings, seen = [], [], set()
    for row in _rows(body.get("data"), 50):
        row = _object(row)
        row_digest = _hash(row)
        if row_digest in seen:
            raise ContractQuarantine("duplicate_settlement_record")
        seen.add(row_digest)
        order_id = _identifier(row.get("orderId"), numeric=True)
        kind = row.get("saleType")
        if kind not in ("SALE", "REFUND"):
            raise ContractQuarantine("unknown_settlement_kind")
        dates = tuple((key, _date(row.get(key), optional=key == "finalSettlementDate")) for key in
                      ("saleDate", "recognitionDate", "settlementDate", "finalSettlementDate"))
        delivery = _object(row.get("deliveryFee"))
        delivery_amounts = tuple((key, _signed(delivery.get(key))) for key in ("amount", "fee", "feeVat", "settlementAmount"))
        observations.append(SettlementObservation("coupang", _hash([row_digest, "delivery"]), order_id, None,
            kind + ":DELIVERY", dates, delivery_amounts, dict(delivery_amounts)["settlementAmount"], row_digest))
        items = _rows(row.get("items"), 100)
        if not items:
            warnings.append("items_unavailable_not_zero_revenue")
        for index, item in enumerate(items):
            item = _object(item)
            vendor_item = _integer(item.get("vendorItemId"))
            if vendor_item == 0:
                warnings.append("unbound_adjustment_item")
            amounts = tuple((key, _signed(item.get(key), optional=key not in
                            {"saleAmount", "serviceFee", "serviceFeeVat", "settlementAmount"})) for key in
                           ("salePrice", "saleAmount", "serviceFee", "serviceFeeVat", "settlementAmount",
                            "coupangDiscountCoupon", "sellerDiscountCoupon", "downloadableCoupon",
                            "couranteeFee", "couranteeFeeVat", "storeFeeDiscount", "storeFeeDiscountVat"))
            observations.append(SettlementObservation("coupang", _hash([row_digest, "item", index]), order_id,
                str(vendor_item) if vendor_item else None, kind + ":ITEM", dates, amounts,
                dict(amounts)["settlementAmount"], row_digest))
    return OfflineSettlementPage("coupang", tuple(observations), token,
                                  tuple(sorted(set(warnings))), digest)


@dataclass(frozen=True)
class FixtureLedgerComparison:
    source_record_ref: str
    expected_minor: int
    ledger_minor: int | None
    status: str
    cash_receipt_verified: bool = field(default=False, init=False)


def compare_fixture_ledger(page: OfflineSettlementPage, ledger_rows: list[dict]) -> tuple[FixtureLedgerComparison, ...]:
    """Exact explicit source-ref matching only. A fixture match is not cash proof."""
    if not isinstance(page, OfflineSettlementPage):
        raise ContractQuarantine("settlement_page_required")
    expected = {item.source_record_ref: item for item in page.observations}
    if len(expected) != len(page.observations):
        raise ContractQuarantine("duplicate_settlement_record")
    received = {}
    refs = set()
    for row in _rows(ledger_rows, 1000):
        row = _object(row)
        if set(row) != {"source_record_ref", "ledger_ref", "amount_minor", "currency", "date"}:
            raise ContractQuarantine("ledger_schema_mismatch")
        ref, ledger_ref = _identifier(row["source_record_ref"]), _identifier(row["ledger_ref"])
        if ref not in expected or ref in received or ledger_ref in refs:
            raise ContractQuarantine("unmatched_or_duplicate_ledger_row")
        _date(row["date"])
        amount = _signed(row["amount_minor"])
        if row["currency"] != "KRW":
            raise ContractQuarantine("ledger_currency_mismatch")
        refs.add(ledger_ref)
        received[ref] = amount
    return tuple(FixtureLedgerComparison(ref, row.expected_minor, received.get(ref),
        "MISSING_LEDGER" if ref not in received else "MATCHED_FIXTURE" if received[ref] == row.expected_minor else "AMOUNT_MISMATCH")
        for ref, row in expected.items())
