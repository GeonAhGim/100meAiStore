import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packages.store_core.channel_order_contracts import (
    ContractQuarantine, canonical_json, parse_coupang_day_page,
    parse_naver_changes, parse_naver_details,
)
from packages.store_core.offline_contract_journal import OfflineContractJournal


class ChannelOrderContractTest(unittest.TestCase):
    def setUp(self):
        self.fixtures = json.loads((Path(__file__).parents[1] / "fixtures" /
                                    "channel_orders.json").read_text(encoding="utf-8"))

    def naver(self, body=None):
        return parse_naver_details(self.fixtures["naver"] if body is None else body,
                                   requested_ids=("fixture-product-order-1",))

    def test_naver_preserves_partial_quantity_and_discounted_payment(self):
        with patch("socket.socket", side_effect=AssertionError("network prohibited")):
            page = self.naver()
        snapshot = page.snapshots[0]
        self.assertEqual((3, 2, True), (snapshot.initial_quantity, snapshot.remaining_quantity,
                                      snapshot.claim_review_required))
        self.assertEqual(1900, dict(snapshot.amounts_krw)["remaining_payment"])
        self.assertEqual("fixture-product-order-1", snapshot.line_id)
        self.assertFalse(snapshot.executable)
        self.assertNotIn("PRIVATE", page.canonical_payload())
        self.assertEqual(64, len(snapshot.source_digest))

    def test_naver_detail_id_coverage_and_unknown_state_quarantine(self):
        for change in ("missing", "unexpected", "duplicate", "unknown", "remaining", "legacy"):
            body = copy.deepcopy(self.fixtures["naver"])
            product = body["data"][0]["productOrder"]
            if change == "missing": body["data"] = []
            if change == "unexpected": product["productOrderId"] = "other-id"
            if change == "duplicate": body["data"].append(copy.deepcopy(body["data"][0]))
            if change == "unknown": product["productOrderStatus"] = "SYNTHETIC PRIVATE NAME"
            if change == "remaining": product["remainQuantity"] = 4
            if change == "legacy": product["quantity"] = product.pop("initialQuantity")
            with self.subTest(change=change), self.assertRaises(ContractQuarantine) as error:
                self.naver(body)
            self.assertNotIn("PRIVATE", str(error.exception))

    def test_coupang_split_shipments_preserve_large_ids_and_cancel_quantities(self):
        body = self.fixtures["coupang"]
        second = copy.deepcopy(body["data"][0])
        second["shipmentBoxId"] += 2
        body["data"].append(second)
        page = parse_coupang_day_page(body)
        first, second = page.snapshots
        self.assertEqual("9007199254740993", first.order_id)
        self.assertEqual(first.order_id, second.order_id)
        self.assertNotEqual(first.line_id, second.line_id)
        self.assertEqual((5, 2, True), (first.initial_quantity, first.remaining_quantity, first.claim_review_required))
        self.assertIsNone(dict(first.amounts_krw)["discount"])
        self.assertIsNone(dict(first.amounts_krw)["shipment_remote"])
        self.assertEqual("fixture+/=cursor", page.next_cursor)
        self.assertNotIn("PRIVATE", page.canonical_payload())

    def test_coupang_quantity_money_and_duplicate_boundaries(self):
        for change in ("overcancel", "float", "nanos", "currency", "price", "duplicate", "unknown", "error"):
            body = copy.deepcopy(self.fixtures["coupang"])
            row = body["data"][0]
            line = row["orderItems"][0]
            if change == "overcancel": line["cancelCount"] = 99
            if change == "float": row["orderId"] = float(row["orderId"])
            if change == "nanos": line["salesPrice"]["nanos"] = 1
            if change == "currency": line["salesPrice"]["currencyCode"] = "USD"
            if change == "price": line["orderPrice"]["units"] = 3
            if change == "duplicate": body["data"].append(copy.deepcopy(row))
            if change == "unknown": row["status"] = "UNKNOWN"
            if change == "error": body["code"] = 500
            with self.subTest(change=change), self.assertRaises(ContractQuarantine):
                parse_coupang_day_page(body)

    def test_empty_coupang_page_is_not_an_error(self):
        page = parse_coupang_day_page({"code": 200, "data": [], "nextToken": ""})
        self.assertEqual((), page.snapshots)
        self.assertIsNone(page.next_cursor)

    def test_naver_same_time_continuation_keeps_sequence_and_count(self):
        first = parse_naver_changes(self.fixtures["naver_changes"])
        query = dict(first.next_cursor.next_query())
        self.assertEqual(first.changed_at[0], query["lastChangedFrom"])
        self.assertEqual("fixture-product-order-2", query["moreSequence"])
        second_body = copy.deepcopy(self.fixtures["naver_changes"])
        second_body["data"]["lastChangeStatuses"][0]["productOrderId"] = "fixture-product-order-2"
        second_body["data"].pop("more")
        second = parse_naver_changes(second_body)
        self.assertEqual(first.changed_at, second.changed_at)
        self.assertNotEqual(first.product_order_ids, second.product_order_ids)
        self.assertIsNone(second.next_cursor)
        second_body["data"]["count"] = 2
        with self.assertRaises(ContractQuarantine): parse_naver_changes(second_body)

    def test_invalid_paging_never_becomes_a_completed_page(self):
        for more in ({"moreFrom": "2026-09-06"}, {"moreFrom": "2026-09-06T10:00:00+09:00"}, [],
                     {"moreFrom": "2026-09-06T10:00:00+09:00", "moreSequence": ""}):
            body = copy.deepcopy(self.fixtures["naver_changes"])
            body["data"]["more"] = more
            with self.assertRaises(ContractQuarantine): parse_naver_changes(body)

    def test_journal_restart_replay_cas_and_tenant_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixtures.sqlite"
            journal = OfflineContractJournal(path)
            page = self.naver()
            continuation = canonical_json(dict(parse_naver_changes(self.fixtures["naver_changes"]).next_cursor.next_query()))
            self.assertEqual((1, False), journal.record("t1", "c1", "page1", page, expected_version=0, continuation=continuation))
            journal.close()
            journal = OfflineContractJournal(path)
            self.assertEqual((1, continuation), journal.checkpoint("t1", "c1", "naver"))
            self.assertEqual((1, True), journal.record("t1", "c1", "page1", page, expected_version=0, continuation=continuation))
            self.assertEqual((2, False), journal.record("t1", "c1", "page2", page, expected_version=1))
            self.assertEqual((2, True), journal.record("t1", "c1", "page1", page, expected_version=0, continuation=continuation))
            self.assertEqual((2, None), journal.checkpoint("t1", "c1", "naver"))
            with self.assertRaises(ContractQuarantine):
                journal.record("t1", "c1", "page3", page, expected_version=0)
            with self.assertRaises(ContractQuarantine):
                journal.record("t1", "c1", "page1", page, expected_version=2)
            self.assertEqual(2, journal.page_count("t1", "c1", "naver"))
            self.assertEqual(0, journal.page_count("t2", "c1", "naver"))
            self.assertEqual((1, False), journal.record("t2", "c1", "page1", page, expected_version=0))
            journal.close()

    def test_journal_checkpoint_failure_rolls_back_page_insert(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = OfflineContractJournal(Path(temp) / "fixtures.sqlite")
            journal.db.execute("""CREATE TRIGGER fail_checkpoint BEFORE INSERT ON checkpoints
                                BEGIN SELECT RAISE(ABORT, 'fixture failure'); END""")
            with self.assertRaises(sqlite3.IntegrityError):
                journal.record("t1", "c1", "page1", self.naver(), expected_version=0)
            self.assertEqual(0, journal.page_count("t1", "c1", "naver"))
            self.assertEqual((0, None), journal.checkpoint("t1", "c1", "naver"))
            journal.close()

    def test_journal_refuses_unrelated_database_without_changing_it(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "unrelated.sqlite"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE untouched(value TEXT)")
            db.commit()
            db.close()
            before = path.read_bytes()
            with self.assertRaises(ContractQuarantine): OfflineContractJournal(path)
            self.assertEqual(before, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
