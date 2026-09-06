import copy
import tempfile
import unittest
from pathlib import Path

from packages.store_core.channel_order_contracts import ContractQuarantine
from packages.store_core.channel_finance_contracts import (
    compare_fixture_ledger, parse_coupang_revenue_page, parse_naver_settlement_page,
)
from packages.store_core.offline_contract_journal import OfflineContractJournal


class ChannelFinanceContractTest(unittest.TestCase):
    def setUp(self):
        self.naver = {"elements": [{
            "orderId": "fixture-order", "productOrderId": "fixture-product-order", "productOrderType": "PROD_ORDER",
            "settleType": "NORMAL_SETTLE_AFTER_CANCEL", "settleBasisDate": "2026-09-01",
            "settleExpectDate": "2026-09-10", "settleCompleteDate": None, "payDate": "2026-08-30",
            "paySettleAmount": -1000, "benefitSettleAmount": 0, "settleExpectAmount": -970,
            "totalPayCommissionAmount": -30, "purchaserName": "SYNTHETIC PRIVATE NAME"}],
            "pagination": {"page": 1, "size": 50, "totalPages": 1, "totalElements": 1}}
        self.coupang = {"code": 200, "hasNext": False, "nextToken": "", "data": [{
            "orderId": 9007199254740993, "saleType": "SALE", "saleDate": "2026-08-30",
            "recognitionDate": "2026-09-01", "settlementDate": "2026-09-10", "finalSettlementDate": "2026-09-20",
            "deliveryFee": {"amount": 3000, "fee": 90, "feeVat": 9, "settlementAmount": 2901},
            "items": [{"vendorItemId": 9007199254740995, "productId": 1,
                       "saleAmount": 1000, "serviceFee": 100, "serviceFeeVat": 10, "settlementAmount": 890,
                       "productName": "SYNTHETIC PRIVATE VALUE"}]}]}

    def test_naver_preserves_signed_refund_fees_and_expected_dates(self):
        page = parse_naver_settlement_page(self.naver)
        item = page.observations[0]
        self.assertEqual(-970, item.expected_minor)
        self.assertEqual(-30, dict(item.amounts_krw)["totalPayCommissionAmount"])
        self.assertIsNone(item.received_minor)
        self.assertFalse(item.cash_receipt_verified)
        self.assertIsNone(dict(item.dates)["settleCompleteDate"])
        self.assertNotIn("PRIVATE", page.canonical_payload())

    def test_coupang_delivery_is_separate_and_vendor_item_is_stable_key(self):
        page = parse_coupang_revenue_page(self.coupang)
        delivery, item = page.observations
        self.assertEqual((2901, 890), (delivery.expected_minor, item.expected_minor))
        self.assertIsNone(delivery.product_ref)
        self.assertEqual("9007199254740995", item.product_ref)
        self.assertEqual(10, dict(item.amounts_krw)["serviceFeeVat"])
        self.assertFalse(item.cash_receipt_verified)
        self.assertNotIn("PRIVATE", page.canonical_payload())

    def test_empty_coupang_items_preserve_delivery_and_warning(self):
        self.coupang["data"][0]["items"] = []
        page = parse_coupang_revenue_page(self.coupang)
        self.assertEqual(1, len(page.observations))
        self.assertIn("items_unavailable_not_zero_revenue", page.warnings)

    def test_bad_money_unknown_kind_and_duplicate_source_quarantine(self):
        for change in ("decimal", "kind", "duplicate", "size"):
            body = copy.deepcopy(self.naver)
            if change == "decimal": body["elements"][0]["settleExpectAmount"] = 1.5
            if change == "kind": body["elements"][0]["settleType"] = "UNKNOWN"
            if change == "duplicate": body["elements"].append(copy.deepcopy(body["elements"][0])); body["pagination"]["totalElements"] = 2
            if change == "size": body["pagination"]["size"] = 0
            with self.subTest(change=change), self.assertRaises(ContractQuarantine): parse_naver_settlement_page(body)

    def test_revenue_paging_requires_consistent_has_next(self):
        self.coupang["hasNext"] = True
        with self.assertRaises(ContractQuarantine): parse_coupang_revenue_page(self.coupang)
        self.coupang["nextToken"] = "fixture+/="
        self.assertEqual("fixture+/=", parse_coupang_revenue_page(self.coupang).next_cursor)
        self.coupang["hasNext"] = False
        with self.assertRaises(ContractQuarantine): parse_coupang_revenue_page(self.coupang)

    def test_explicit_ledger_match_does_not_verify_real_cash_and_missing_is_visible(self):
        page = parse_naver_settlement_page(self.naver)
        self.assertEqual("MISSING_LEDGER", compare_fixture_ledger(page, [])[0].status)
        ledger = {"source_record_ref": page.observations[0].source_record_ref, "ledger_ref": "fixture-ledger",
                  "amount_minor": -970, "currency": "KRW", "date": "2026-09-11"}
        match = compare_fixture_ledger(page, [ledger])[0]
        self.assertEqual("MATCHED_FIXTURE", match.status)
        self.assertFalse(match.cash_receipt_verified)
        self.assertEqual("AMOUNT_MISMATCH", compare_fixture_ledger(page, [{**ledger, "amount_minor": -969}])[0].status)
        for rows in ([ledger, ledger], [{**ledger, "currency": "USD"}], [{**ledger, "source_record_ref": "missing"}]):
            with self.assertRaises(ContractQuarantine): compare_fixture_ledger(page, rows)

    def test_settlement_journal_restart_and_order_scope_separation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixtures.sqlite"
            page = parse_coupang_revenue_page(self.coupang)
            journal = OfflineContractJournal(path)
            self.assertEqual((1, False), journal.record("t1", "c1", "settlement1", page, expected_version=0))
            self.assertEqual((0, None), journal.checkpoint("t1", "c1", "coupang"))
            journal.close()
            journal = OfflineContractJournal(path)
            self.assertEqual((1, True), journal.record("t1", "c1", "settlement1", page, expected_version=0))
            self.assertEqual(1, journal.page_count("t1", "c1", "coupang_settlement"))
            self.assertEqual(0, journal.page_count("t2", "c1", "coupang_settlement"))
            changed = copy.deepcopy(self.coupang)
            changed["data"][0]["items"][0]["settlementAmount"] = 889
            with self.assertRaises(ContractQuarantine):
                journal.record("t1", "c1", "settlement1", parse_coupang_revenue_page(changed), expected_version=1)
            self.assertEqual((1, None), journal.checkpoint("t1", "c1", "coupang_settlement"))
            journal.close()


if __name__ == "__main__":
    unittest.main()
