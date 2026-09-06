import copy
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from packages.store_core.channel_claim_contracts import parse_coupang_claim_page
from packages.store_core.channel_order_contracts import ContractQuarantine
from packages.store_core.offline_claim_contracts import (
    build_coupang_return_review, reconcile_return_fixture, verify_return_fixture_review,
)
from tests.store_core.test_channel_claim_contracts import claim_fixture


class OfflineReturnContractTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 7, tzinfo=timezone.utc)
        self.body = claim_fixture()
        self.body["data"][0].update(receiptStatus="VENDOR_WAREHOUSE_CONFIRM", preRefund=False)
        self.kw = dict(tenant_ref="fixture-tenant", connection_ref="fixture-channel", vendor_ref="fixture-vendor",
                       receipt_id="9007199254740989", order_id="9007199254740993", shipment_id="9007199254740995",
                       vendor_item_id="9007199254740997", cancel_quantity=1, fixture_amount_krw=1000,
                       fixture_amount_cap_krw=2000, withdrawal_review_digest="a" * 64, withdrawal_reviewed=True,
                       observed_at=self.now, withdrawal_observed_at=self.now, now=self.now,
                       expires_at=self.now + timedelta(seconds=60))

    def build(self, body=None, **changes):
        return build_coupang_return_review(parse_coupang_claim_page(self.body if body is None else body),
                                          **(self.kw | changes))

    def verify(self, plan, **changes):
        return verify_return_fixture_review(plan, **(dict(approval_digest=plan.approval_digest,
            tenant_ref="fixture-tenant", connection_ref="fixture-channel", now=self.now) | changes))

    def completed_body(self):
        body = copy.deepcopy(self.body)
        body["data"][0]["receiptStatus"] = "RETURNS_COMPLETED"
        return body

    def reconcile(self, plan, body=None, **changes):
        return reconcile_return_fixture(plan, {"code": "200", "message": "OK"}, **(
            dict(readback=parse_coupang_claim_page(self.completed_body() if body is None else body),
                 readback_observed_at=self.now, now=self.now) | changes))

    def test_return_roundtrip_requires_readback_and_never_proves_bank_refund(self):
        with patch("socket.socket", side_effect=AssertionError("network prohibited")):
            plan = self.build()
            self.assertEqual("FIXTURE_REVIEW_ONLY", self.verify(plan))
            self.assertEqual("READBACK_REQUIRED", reconcile_return_fixture(plan,
                {"code": 200, "message": "OK"}, now=self.now).decision)
            result = self.reconcile(plan)
        self.assertEqual("MATCHED_COMPLETED_FIXTURE", result.decision)
        self.assertFalse(result.bank_refund_verified)
        self.assertFalse(result.real_return_confirmed)
        self.assertFalse(result.resend_authorized)
        self.assertFalse(plan.external_write_authorized)
        self.assertNotIn(plan.receipt_id, repr(plan))

    def test_prefunded_cancel_incomplete_and_multi_line_receipts_stop(self):
        for change in ({"preRefund": True}, {"receiptType": "CANCEL"},
                       {"receiptStatus": "RETURNS_COMPLETED"}, {"receiptStatus": "RETURNS_UNCHECKED"}):
            body = copy.deepcopy(self.body)
            body["data"][0].update(change)
            with self.assertRaises(ContractQuarantine):
                self.build(body)
        body = copy.deepcopy(self.body)
        second = dict(body["data"][0]["returnItems"][0], vendorItemId=8)
        body["data"][0]["returnItems"].append(second)
        body["data"][0]["cancelCountSum"] = 2
        with self.assertRaisesRegex(ContractQuarantine, "single_exact_receipt_line_required"):
            self.build(body)

    def test_identity_amount_cap_and_review_guards(self):
        for change in (dict(receipt_id="7"), dict(order_id="7"), dict(shipment_id="7"),
                       dict(vendor_item_id="7"), dict(cancel_quantity=2), dict(cancel_quantity=True),
                       dict(fixture_amount_krw=2001), dict(fixture_amount_cap_krw=0),
                       dict(withdrawal_reviewed=1), dict(withdrawal_review_digest="bad"),
                       dict(observed_at=self.now - timedelta(seconds=300)),
                       dict(withdrawal_observed_at=self.now + timedelta(seconds=1)),
                       dict(expires_at=self.now), dict(max_age_seconds=True)):
            with self.subTest(change=change), self.assertRaises(ContractQuarantine):
                self.build(**change)

    def test_changed_evidence_context_time_and_amount_invalidate_review(self):
        plan = self.build()
        for change in (dict(tenant_ref="other"), dict(connection_ref="other"),
                       dict(now=self.now + timedelta(seconds=60)),
                       dict(now=self.now - timedelta(seconds=1))):
            with self.assertRaises(ContractQuarantine):
                self.verify(plan, **change)
        for changed in (replace(plan, source_digest="b" * 64), replace(plan, fixture_amount_krw=999),
                        self.build(withdrawal_observed_at=self.now - timedelta(seconds=1)),
                        self.build(withdrawal_review_digest="c" * 64)):
            with self.assertRaisesRegex(ContractQuarantine, "approval_digest_mismatch"):
                self.verify(changed, approval_digest=plan.approval_digest)

    def test_partial_foreign_stale_and_unknown_outcomes_never_resend(self):
        plan = self.build()
        for response in (None, {}, {"code": True}, {"code": 500, "message": "OK"},
                         {"code": 200, "message": "OK", "errorItems": [{}]},
                         {"code": 200, "message": "PRIVATE FIXTURE"}):
            result = reconcile_return_fixture(plan, response, now=self.now)
            self.assertEqual("RECONCILE_REQUIRED", result.decision)
            self.assertFalse(result.resend_authorized)
        for field in ("receiptId", "orderId"):
            body = self.completed_body()
            body["data"][0][field] = 7
            self.assertEqual("RECONCILE_REQUIRED", self.reconcile(plan, body).decision)
        for field, value in (("shipmentBoxId", 7), ("vendorItemId", 8), ("purchaseCount", 3)):
            body = self.completed_body()
            body["data"][0]["returnItems"][0][field] = value
            self.assertEqual("RECONCILE_REQUIRED", self.reconcile(plan, body).decision)
        self.assertEqual("RECONCILE_REQUIRED", self.reconcile(plan, self.body).decision)
        for change in (dict(readback_observed_at=self.now - timedelta(seconds=1)),
                       dict(readback_observed_at=self.now + timedelta(seconds=1)),
                       dict(now=self.now + timedelta(seconds=60))):
            self.assertEqual("RECONCILE_REQUIRED", self.reconcile(plan, **change).decision)


if __name__ == "__main__":
    unittest.main()
