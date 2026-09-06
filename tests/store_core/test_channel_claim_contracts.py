import copy
import unittest
from dataclasses import FrozenInstanceError, asdict
from unittest.mock import patch

from packages.store_core.channel_claim_contracts import RECEIPT_STATES, parse_coupang_claim_page
from packages.store_core.channel_order_contracts import ContractQuarantine


def claim_fixture():
    return {"code": 200, "data": [{
        "receiptId": 9007199254740989, "orderId": 9007199254740993,
        "receiptType": "RETURN", "receiptStatus": "RELEASE_STOP_UNCHECKED",
        "preRefund": True, "cancelCountSum": 1,
        "requesterName": "PRIVATE FIXTURE", "requesterAddress": "PRIVATE FIXTURE",
        "cancelReason": "PRIVATE FIXTURE", "returnShippingCharge": {
            "currencyCode": "KRW", "units": -3000, "nanos": 0},
        "returnItems": [{"shipmentBoxId": 9007199254740995,
                         "vendorItemId": 9007199254740997,
                         "purchaseCount": 2, "cancelCount": 1}]}], "nextToken": ""}


class ChannelClaimContractTest(unittest.TestCase):
    def test_partial_claim_signed_money_and_privacy_without_network(self):
        with patch("socket.socket", side_effect=AssertionError("network prohibited")):
            page = parse_coupang_claim_page(claim_fixture())
        line, = page.lines
        self.assertEqual("9007199254740997", line.vendor_item_id)
        self.assertEqual((2, 1, -3000), (line.purchase_quantity, line.cancel_quantity,
                                       line.return_shipping_charge_krw))
        self.assertTrue(line.pre_refund_observed)
        self.assertFalse(line.bank_refund_verified)
        self.assertNotIn("PRIVATE FIXTURE", str(asdict(page)))
        self.assertNotIn(line.order_id, repr(page))
        with self.assertRaises(FrozenInstanceError):
            line.cancel_quantity = 2

    def test_known_states_remain_observations(self):
        for state in RECEIPT_STATES:
            for kind in ("RETURN", "CANCEL"):
                body = claim_fixture()
                body["data"][0].update(receiptStatus=state, receiptType=kind)
                line, = parse_coupang_claim_page(body).lines
                self.assertEqual(state, line.receipt_status)
                self.assertFalse(line.bank_refund_verified)

    def test_unsafe_receipts_quarantine_with_safe_diagnostics(self):
        changes = [{"receiptType": "PRIVATE FIXTURE"}, {"receiptType": []},
                   {"receiptStatus": "PRIVATE FIXTURE"}, {"receiptStatus": {}},
                   {"preRefund": 1}, {"receiptId": True}, {"orderId": "42"},
                   {"cancelCountSum": 2}, {"returnItems": []},
                   {"returnShippingCharge": {"currencyCode": "USD", "units": 1, "nanos": 0}},
                   {"returnShippingCharge": {"currencyCode": "KRW", "units": 1, "nanos": 1}},
                   {"returnShippingCharge": {"currencyCode": "KRW", "units": True, "nanos": 0}},
                   {"returnShippingCharge": {"currencyCode": "KRW", "units": 2**63, "nanos": 0}}]
        for change in changes:
            body = claim_fixture()
            body["data"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ContractQuarantine) as raised:
                parse_coupang_claim_page(body)
            self.assertNotIn("PRIVATE FIXTURE", str(raised.exception))

    def test_duplicate_receipts_items_and_inconsistent_quantities_quarantine(self):
        bodies = []
        body = claim_fixture()
        body["data"] *= 2
        bodies.append(body)
        body = claim_fixture()
        body["data"][0]["returnItems"] *= 2
        bodies.append(body)
        for change in ({"purchaseCount": 0}, {"cancelCount": 0},
                       {"cancelCount": 3}, {"cancelCount": True},
                       {"vendorItemId": 1.5}, {"shipmentBoxId": -1}):
            body = claim_fixture()
            body["data"][0]["returnItems"][0].update(change)
            bodies.append(body)
        for body in bodies:
            with self.assertRaises(ContractQuarantine):
                parse_coupang_claim_page(body)

    def test_multi_line_partial_receipt_exact_sum(self):
        body = claim_fixture()
        receipt = body["data"][0]
        second = copy.deepcopy(receipt["returnItems"][0])
        second.update(vendorItemId=9007199254740998, cancelCount=2)
        receipt["returnItems"].append(second)
        receipt["cancelCountSum"] = 3
        page = parse_coupang_claim_page(body)
        self.assertEqual(2, len(page.lines))
        self.assertEqual(3, sum(line.cancel_quantity for line in page.lines))

    def test_page_boundaries_cursor_and_source_replay(self):
        for body in (None, {}, {"code": "200", "data": []},
                     {"code": True, "data": []}, {"code": 500, "data": []},
                     {"code": 200, "data": [None]},
                     {"code": 200, "data": [None] * 51},
                     {"code": 200, "data": [], "nextToken": 1}):
            with self.assertRaises(ContractQuarantine):
                parse_coupang_claim_page(body)
        body = claim_fixture()
        first = parse_coupang_claim_page(body)
        self.assertEqual(first, parse_coupang_claim_page(copy.deepcopy(body)))
        body["nextToken"] = "PRIVATE-CURSOR"
        later = parse_coupang_claim_page(body)
        self.assertNotEqual(first.source_digest, later.source_digest)
        self.assertEqual("PRIVATE-CURSOR", later.next_cursor)
        self.assertNotIn("PRIVATE-CURSOR", repr(later))


if __name__ == "__main__":
    unittest.main()
