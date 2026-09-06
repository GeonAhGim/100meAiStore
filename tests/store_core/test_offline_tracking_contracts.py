import copy
import json
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from packages.store_core.channel_order_contracts import ContractQuarantine, parse_coupang_day_page
from packages.store_core.offline_tracking_contracts import (
    build_coupang_tracking_review, interpret_coupang_tracking_fixture, verify_fixture_review,
)


class OfflineTrackingContractTest(unittest.TestCase):
    def setUp(self):
        self.body = json.loads((Path(__file__).parents[1] / "fixtures" / "channel_orders.json").read_text(encoding="utf-8"))["coupang"]
        item = self.body["data"][0]["orderItems"][0]
        item["cancelCount"] = item["holdCountForCancel"] = 0
        self.now = datetime(2026, 9, 7, tzinfo=timezone.utc)
        self.kw = dict(tenant_ref="fixture-tenant", connection_ref="fixture-connection",
                       order_id="9007199254740993", shipment_id="9007199254740995",
                       vendor_item_id="9007199254740997", invoice_number="000012340001",
                       observed_at=self.now, now=self.now,
                       review_expires_at=self.now + timedelta(seconds=60),
                       preparation_review_digest="a" * 64, post_preparation_reviewed=True)

    def build(self, body=None, **changes):
        return build_coupang_tracking_review(parse_coupang_day_page(self.body if body is None else body),
                                             **(self.kw | changes))

    def verify(self, plan, **changes):
        return verify_fixture_review(plan, **(dict(approval_digest=plan.approval_digest,
                                     tenant_ref="fixture-tenant", connection_ref="fixture-connection",
                                     now=self.now) | changes))

    def response(self):
        return {"code": "200", "data": {"responseCode": 0, "responseList": [
            {"shipmentBoxId": 9007199254740995, "succeed": True,
             "resultCode": "OK", "retryRequired": False}]}}

    def test_fixture_roundtrip_never_authorizes_real_write(self):
        with patch("socket.socket", side_effect=AssertionError("network prohibited")):
            plan = self.build()
            self.assertEqual("FIXTURE_REVIEW_ONLY", self.verify(plan))
            result = interpret_coupang_tracking_fixture(plan, self.response())
        self.assertEqual("MATCHED_SUCCESS_FIXTURE", result.decision)
        self.assertFalse(plan.external_write_authorized)
        self.assertFalse(result.real_shipment_confirmed)
        self.assertFalse(result.resend_authorized)
        self.assertEqual("000012340001", plan.invoice_number)
        self.assertNotIn(plan.invoice_number, repr(plan))
        self.assertNotIn(plan.shipment_id, repr(plan))
        with self.assertRaises(FrozenInstanceError):
            plan.invoice_number = "changed"

    def test_exact_identity_and_claim_checks(self):
        for key in ("order_id", "shipment_id", "vendor_item_id"):
            with self.subTest(key=key), self.assertRaises(ContractQuarantine):
                self.build(**{key: "different"})
        for status in ("ACCEPT", "DEPARTURE", "FINAL_DELIVERY"):
            body = copy.deepcopy(self.body)
            body["data"][0]["status"] = status
            with self.assertRaises(ContractQuarantine):
                self.build(body)
        body = copy.deepcopy(self.body)
        body["data"][0]["orderItems"][0]["holdCountForCancel"] = 1
        with self.assertRaises(ContractQuarantine):
            self.build(body)

    def test_multi_item_and_empty_shipment_stop(self):
        body = copy.deepcopy(self.body)
        second = copy.deepcopy(body["data"][0]["orderItems"][0])
        second["vendorItemId"], second["sequenceNo"] = 8, "002"
        body["data"][0]["orderItems"].append(second)
        with self.assertRaises(ContractQuarantine):
            self.build(body)
        body["data"] = []
        with self.assertRaises(ContractQuarantine):
            self.build(body)

    def test_freshness_review_and_expiry_fail_closed(self):
        cases = [dict(observed_at=self.now - timedelta(seconds=300)),
                 dict(observed_at=self.now + timedelta(seconds=1)),
                 dict(now=self.now.replace(tzinfo=None)), dict(max_age_seconds=True),
                 dict(post_preparation_reviewed=1), dict(preparation_review_digest="bad"),
                 dict(review_expires_at=self.now),
                 dict(review_expires_at=self.now + timedelta(seconds=301))]
        for change in cases:
            with self.subTest(change=change), self.assertRaises(ContractQuarantine):
                self.build(**change)

    def test_approval_binds_context_payload_source_and_time(self):
        plan = self.build()
        for change in (dict(tenant_ref="other"), dict(connection_ref="other"),
                       dict(approval_digest="b" * 64),
                       dict(now=self.now + timedelta(seconds=60)),
                       dict(now=self.now - timedelta(seconds=1))):
            with self.subTest(change=change), self.assertRaises(ContractQuarantine):
                self.verify(plan, **change)
        for changed in (replace(plan, invoice_number="123"), replace(plan, source_digest="c" * 64),
                        replace(plan, preparation_review_digest="d" * 64)):
            with self.assertRaises(ContractQuarantine):
                self.verify(changed, approval_digest=plan.approval_digest)

    def test_unknown_partial_mismatch_and_retry_flag_require_reconciliation(self):
        plan = self.build()
        cases = [None, {}, {"code": True}, {"code": 200, "data": []}]
        for code in (-1, 1, 99, True):
            body = self.response()
            body["data"]["responseCode"] = code
            cases.append(body)
        for change in (dict(shipmentBoxId=7), dict(shipmentBoxId="9007199254740995"),
                       dict(succeed=False), dict(succeed=1), dict(resultCode="FUTURE_CODE"),
                       dict(retryRequired=True), dict(retryRequired=0)):
            body = self.response()
            body["data"]["responseList"][0].update(change)
            cases.append(body)
        body = self.response()
        body["data"]["responseList"] *= 2
        cases.append(body)
        for body in cases:
            with self.subTest(body=body):
                result = interpret_coupang_tracking_fixture(plan, body)
                self.assertEqual("RECONCILE_REQUIRED", result.decision)
                self.assertFalse(result.resend_authorized)

    def test_invoice_diagnostics_do_not_echo_input(self):
        for value in ("PRIVATE-NAME", "=123", "1\n2", 123, "", "1" * 41):
            with self.assertRaisesRegex(ContractQuarantine, "^invalid_fixture_invoice$"):
                self.build(invoice_number=value)


if __name__ == "__main__":
    unittest.main()
