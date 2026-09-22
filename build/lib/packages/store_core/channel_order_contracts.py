"""Offline snapshots of documented channel fields, never executable orders."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from .channel_contracts import COUPANG_STATES


NAVER_STATES = frozenset({"PAYMENT_WAITING", "PAYED", "DELIVERING", "DELIVERED",
                          "PURCHASE_DECIDED", "EXCHANGED", "CANCELED", "RETURNED",
                          "CANCELED_BY_NOPAYMENT"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,149}\Z")


class ContractQuarantine(ValueError):
    """Fixed safe reason only. Caller must not advance a rejected page cursor."""


def canonical_json(value: Any) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ContractQuarantine("invalid_json") from None
    if len(encoded.encode("utf-8")) > 2 * 1024 * 1024:
        raise ContractQuarantine("page_size_exceeded")
    return encoded


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _object(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ContractQuarantine("object_required")
    return value


def _integer(value: Any, *, positive: bool = False) -> int:
    if type(value) is not int or not (1 if positive else 0) <= value <= 2**63 - 1:
        raise ContractQuarantine("invalid_integer")
    return value


def _identifier(value: Any, *, numeric: bool = False) -> str:
    if numeric:
        return str(_integer(value, positive=True))
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ContractQuarantine("invalid_external_id")
    return value


def _stamp(value: Any) -> str:
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        raise ContractQuarantine("invalid_timestamp") from None
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ContractQuarantine("timezone_required")
    return value


def _rows(value: Any, maximum: int) -> list:
    if not isinstance(value, list) or len(value) > maximum:
        raise ContractQuarantine("invalid_row_list")
    return value


def _state(value: Any, states: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in states:
        raise ContractQuarantine("unknown_order_state")
    return value


def _cursor(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 4096 or not value.isprintable():
        raise ContractQuarantine("invalid_cursor")
    return value


@dataclass(frozen=True)
class OfflineOrderSnapshot:
    provider: str
    order_id: str
    line_id: str
    product_id: str
    shipment_id: str | None
    status: str
    initial_quantity: int
    remaining_quantity: int
    amounts_krw: tuple[tuple[str, int | None], ...]
    claim_review_required: bool
    source_digest: str
    executable: bool = field(default=False, init=False)


@dataclass(frozen=True)
class OfflineOrderPage:
    provider: str
    snapshots: tuple[OfflineOrderSnapshot, ...]
    next_cursor: str | None = field(repr=False)
    source_digest: str

    def canonical_payload(self) -> str:
        return canonical_json(asdict(self))


def parse_naver_details(body: Any, *, requested_ids: tuple[str, ...]) -> OfflineOrderPage:
    digest = _hash(body)
    data = _rows(_object(body).get("data"), 300)
    if not isinstance(requested_ids, tuple) or not 1 <= len(requested_ids) <= 300:
        raise ContractQuarantine("requested_ids_required")
    expected = {_identifier(value) for value in requested_ids}
    if len(expected) != len(requested_ids):
        raise ContractQuarantine("duplicate_requested_id")
    snapshots = []
    seen = set()
    for row in data:
        row = _object(row)
        order = _object(row.get("order"))
        product = _object(row.get("productOrder"))
        line_id = _identifier(product.get("productOrderId"))
        if line_id in seen:
            raise ContractQuarantine("duplicate_product_order")
        seen.add(line_id)
        initial = _integer(product.get("initialQuantity"), positive=True)
        remaining = _integer(product.get("remainQuantity"))
        initial_payment = _integer(product.get("initialPaymentAmount"))
        remaining_payment = _integer(product.get("remainPaymentAmount"))
        if remaining > initial or remaining_payment > initial_payment:
            raise ContractQuarantine("contradictory_remaining_amount")
        snapshots.append(OfflineOrderSnapshot(
            "naver", _identifier(order.get("orderId")), line_id,
            _identifier(product.get("productId")), None,
            _state(product.get("productOrderStatus"), NAVER_STATES), initial, remaining,
            (("initial_payment", initial_payment), ("remaining_payment", remaining_payment),
             ("unit_price", _integer(product.get("unitPrice")))),
            bool(product.get("claimStatus") or row.get("currentClaim")
                 or row.get("cancel") or row.get("return") or row.get("exchange")), _hash(row)))
    if seen != expected:
        raise ContractQuarantine("requested_id_coverage_mismatch")
    return OfflineOrderPage("naver", tuple(snapshots), None, digest)


def _krw(value: Any, *, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    money = _object(value)
    if money.get("currencyCode") != "KRW" or type(money.get("nanos")) is not int or money["nanos"] != 0:
        raise ContractQuarantine("unsupported_money")
    return _integer(money.get("units"))


def parse_coupang_day_page(body: Any) -> OfflineOrderPage:
    digest = _hash(body)
    body = _object(body)
    if type(body.get("code")) is not int or body["code"] != 200:
        raise ContractQuarantine("provider_response_error")
    snapshots = []
    seen = set()
    for row in _rows(body.get("data"), 50):
        row = _object(row)
        order_id = _identifier(row.get("orderId"), numeric=True)
        shipment_id = _identifier(row.get("shipmentBoxId"), numeric=True)
        status = _state(row.get("status"), COUPANG_STATES)
        shipping, remote = _krw(row.get("shippingPrice")), _krw(row.get("remotePrice"), nullable=True)
        lines = _rows(row.get("orderItems"), 100)
        if not lines:
            raise ContractQuarantine("empty_order_items")
        for line in lines:
            line = _object(line)
            sequence = line.get("sequenceNo")
            if not isinstance(sequence, str) or not re.fullmatch(r"[0-9]{3}", sequence):
                raise ContractQuarantine("invalid_sequence")
            line_id = shipment_id + ":" + sequence
            if line_id in seen:
                raise ContractQuarantine("duplicate_shipment_line")
            seen.add(line_id)
            initial = _integer(line.get("shippingCount"), positive=True)
            cancelled = _integer(line.get("cancelCount"))
            pending = _integer(line.get("holdCountForCancel"))
            remaining = initial - cancelled - pending
            if remaining < 0:
                raise ContractQuarantine("contradictory_cancellation_count")
            unit, order_price = (_krw(line.get(key)) for key in ("salesPrice", "orderPrice"))
            discount = _krw(line.get("discountPrice"), nullable=True)
            if order_price != unit * initial:
                raise ContractQuarantine("order_price_mismatch")
            snapshots.append(OfflineOrderSnapshot(
                "coupang", order_id, line_id, _identifier(line.get("vendorItemId"), numeric=True),
                shipment_id, status, initial, remaining,
                (("unit_price", unit), ("order_price", order_price), ("discount", discount),
                 ("shipment_shipping", shipping), ("shipment_remote", remote)),
                bool(cancelled or pending), _hash(row)))
    next_token = body.get("nextToken")
    # Official sample uses an empty string at the end of pagination.
    if next_token is not None and next_token != "":
        next_token = _cursor(next_token)
    else:
        next_token = None
    return OfflineOrderPage("coupang", tuple(snapshots), next_token, digest)


@dataclass(frozen=True)
class NaverChangeCursor:
    more_from: str
    more_sequence: str = field(repr=False)

    def next_query(self) -> tuple[tuple[str, str], ...]:
        return (("lastChangedFrom", self.more_from), ("moreSequence", self.more_sequence))


@dataclass(frozen=True)
class NaverChangePage:
    product_order_ids: tuple[str, ...]
    changed_at: tuple[str, ...]
    next_cursor: NaverChangeCursor | None
    source_digest: str


def parse_naver_changes(body: Any) -> NaverChangePage:
    digest = _hash(body)
    data = _object(_object(body).get("data"))
    rows = _rows(data.get("lastChangeStatuses"), 300)
    if _integer(data.get("count")) != len(rows):
        raise ContractQuarantine("change_count_mismatch")
    ids, stamps = [], []
    for row in rows:
        row = _object(row)
        ids.append(_identifier(row.get("productOrderId")))
        stamps.append(_stamp(row.get("lastChangedDate")))
        _state(row.get("productOrderStatus"), NAVER_STATES)
    if len(set(ids)) != len(ids):
        raise ContractQuarantine("duplicate_change_id")
    more = data.get("more")
    cursor = None
    if more is not None:
        more = _object(more)
        cursor = NaverChangeCursor(_stamp(more.get("moreFrom")), _cursor(more.get("moreSequence")))
    return NaverChangePage(tuple(ids), tuple(stamps), cursor, digest)
