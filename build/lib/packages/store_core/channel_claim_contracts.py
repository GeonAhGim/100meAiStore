"""Offline Coupang claim observations; no refund/payment or claim write API."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .channel_order_contracts import (ContractQuarantine, _cursor, _hash, _identifier,
                                     _integer, _object, _rows)


RECEIPT_STATES = frozenset({"RELEASE_STOP_UNCHECKED", "RETURNS_UNCHECKED",
    "VENDOR_WAREHOUSE_CONFIRM", "REQUEST_COUPANG_CHECK", "RETURNS_COMPLETED"})


@dataclass(frozen=True)
class OfflineClaimLine:
    receipt_id: str = field(repr=False)
    order_id: str = field(repr=False)
    shipment_id: str = field(repr=False)
    vendor_item_id: str = field(repr=False)
    receipt_type: str
    receipt_status: str
    purchase_quantity: int
    cancel_quantity: int
    pre_refund_observed: bool
    return_shipping_charge_krw: int | None
    source_digest: str
    bank_refund_verified: bool = field(default=False, init=False)


@dataclass(frozen=True)
class OfflineClaimPage:
    lines: tuple[OfflineClaimLine, ...]
    source_digest: str
    next_cursor: str | None = field(repr=False)
    provider: str = field(default="coupang", init=False)


def parse_coupang_claim_page(body: Any) -> OfflineClaimPage:
    digest = _hash(body)
    body = _object(body)
    if type(body.get("code")) is not int or body["code"] != 200:
        raise ContractQuarantine("claim_response_not_success")
    lines, seen_receipts = [], set()
    # 50 is this fixture's bounded page policy, not an inferred vendor maximum.
    for receipt in _rows(body.get("data"), 50):
        receipt = _object(receipt)
        receipt_id = _identifier(receipt.get("receiptId"), numeric=True)
        order_id = _identifier(receipt.get("orderId"), numeric=True)
        if receipt_id in seen_receipts:
            raise ContractQuarantine("duplicate_claim_receipt")
        seen_receipts.add(receipt_id)
        kind, state = receipt.get("receiptType"), receipt.get("receiptStatus")
        if not isinstance(kind, str) or kind not in {"RETURN", "CANCEL"}:
            raise ContractQuarantine("unknown_receipt_type")
        if not isinstance(state, str) or state not in RECEIPT_STATES:
            raise ContractQuarantine("unknown_receipt_state")
        if type(receipt.get("preRefund")) is not bool:
            raise ContractQuarantine("invalid_pre_refund_observation")
        charge = receipt.get("returnShippingCharge")
        if charge is not None:
            charge = _object(charge)
            if (charge.get("currencyCode") != "KRW" or type(charge.get("units")) is not int
                    or not -(2**63) <= charge["units"] < 2**63
                    or type(charge.get("nanos")) is not int or charge["nanos"] != 0):
                raise ContractQuarantine("unsupported_claim_money")
            charge = charge["units"]
        items = _rows(receipt.get("returnItems"), 1000)
        if not items:
            raise ContractQuarantine("claim_items_required")
        count_sum, seen_items = 0, set()
        for item in items:
            item = _object(item)
            shipment_id = _identifier(item.get("shipmentBoxId"), numeric=True)
            vendor_item_id = _identifier(item.get("vendorItemId"), numeric=True)
            key = shipment_id, vendor_item_id
            if key in seen_items:
                raise ContractQuarantine("duplicate_claim_item")
            seen_items.add(key)
            purchase = _integer(item.get("purchaseCount"), positive=True)
            cancel = _integer(item.get("cancelCount"), positive=True)
            if cancel > purchase:
                raise ContractQuarantine("claim_quantity_exceeds_purchase")
            count_sum += cancel
            lines.append(OfflineClaimLine(receipt_id, order_id, shipment_id, vendor_item_id,
                kind, state, purchase, cancel, receipt["preRefund"], charge, _hash(receipt)))
        if count_sum != _integer(receipt.get("cancelCountSum"), positive=True):
            raise ContractQuarantine("claim_count_sum_mismatch")
    cursor = body.get("nextToken")
    return OfflineClaimPage(tuple(lines), digest, None if cursor in (None, "") else _cursor(cursor))
