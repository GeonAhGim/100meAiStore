import unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from packages.store_core.channel_order_contracts import ContractQuarantine
from packages.store_core.offline_tracking_batch import build_tracking_batch, interpret_tracking_batch, verify_tracking_batch
from tests.store_core import test_offline_tracking_contracts as tracking_fixtures


class TrackingBatchTest(unittest.TestCase):
    def setUp(self):
        coupang, naver = tracking_fixtures.OfflineTrackingContractTest(), tracking_fixtures.NaverDispatchContractTest()
        coupang.setUp()
        naver.setUp()
        self.now = coupang.now
        self.coupang = coupang.build()
        self.naver = replace(naver.build(), connection_ref=self.coupang.connection_ref)
        self.kw = dict(tenant_ref=self.coupang.tenant_ref, connection_ref=self.coupang.connection_ref, now=self.now)

    def test_naver_thirty_unordered_results_and_coupang_batch_without_network(self):
        with patch("socket.socket", side_effect=AssertionError("network prohibited")):
            reviews = tuple(replace(self.naver, product_order_id=f"fixture-{number}") for number in range(30))
            batch = build_tracking_batch(reviews, **self.kw)
            self.assertEqual("FIXTURE_REVIEW_ONLY", verify_tracking_batch(batch, approval_digest=batch.approval_digest, **self.kw))
            response = {"data": {"successProductOrderIds": [r.product_order_id for r in reversed(reviews)], "failProductOrderInfos": []}}
            result = interpret_tracking_batch(batch, response)
            self.assertEqual("MATCHED_BATCH_FIXTURE", result.decision)
            coupang = build_tracking_batch((self.coupang, replace(self.coupang, shipment_id="7")), **self.kw)
            result = interpret_tracking_batch(coupang, self.coupang_response())
        self.assertEqual("MATCHED_BATCH_FIXTURE", result.decision)
        self.assertFalse(result.resend_authorized)
        self.assertFalse(result.real_shipment_confirmed)
        self.assertFalse(batch.external_write_authorized)

    def coupang_response(self):
        return {"code": "200", "data": {"responseCode": 0, "responseList": [
            {"shipmentBoxId": identity, "succeed": True, "resultCode": "OK", "retryRequired": False}
            for identity in (7, int(self.coupang.shipment_id))]}}

    def test_batch_scope_duplicates_expiry_and_changed_digest_fail_closed(self):
        for reviews in ((), (self.naver,) * 31, (self.naver,) * 2, (self.naver, self.coupang),
                        (replace(self.naver, tenant_ref="other"),),
                        (replace(self.naver, review_expires_at=self.now.isoformat()),)):
            with self.assertRaises(ContractQuarantine):
                build_tracking_batch(reviews, **self.kw)
        batch = build_tracking_batch((self.naver,), **self.kw)
        changed = replace(batch, reviews=(replace(self.naver, invoice_number="999"),))
        with self.assertRaises(ContractQuarantine):
            verify_tracking_batch(changed, approval_digest=batch.approval_digest, **self.kw)
        with self.assertRaises(ContractQuarantine):
            verify_tracking_batch(batch, approval_digest=batch.approval_digest, **(self.kw | {"now": self.now + timedelta(seconds=60)}))

    def test_naver_partial_duplicate_missing_and_unknown_results_require_reconciliation(self):
        batch = build_tracking_batch((self.naver, replace(self.naver, product_order_id="second")), **self.kw)
        for ids in ([], [self.naver.product_order_id], [self.naver.product_order_id] * 2,
                    [self.naver.product_order_id, "other"], [[], "second"]):
            result = interpret_tracking_batch(batch, {"data": {"successProductOrderIds": ids, "failProductOrderInfos": []}})
            self.assertEqual("RECONCILE_REQUIRED", result.decision)
        response = {"data": {"successProductOrderIds": [self.naver.product_order_id, "second"], "failProductOrderInfos": [{}]}}
        self.assertEqual("RECONCILE_REQUIRED", interpret_tracking_batch(batch, response).decision)

    def test_coupang_partial_duplicate_foreign_and_retry_results_require_reconciliation(self):
        batch = build_tracking_batch((self.coupang, replace(self.coupang, shipment_id="7")), **self.kw)
        bodies = [None, {}, {"code": True}, {"code": "200", "data": []}]
        for change in ({"succeed": False}, {"succeed": 1}, {"retryRequired": True},
                       {"shipmentBoxId": 9}, {"resultCode": "UNKNOWN"}):
            body = self.coupang_response()
            body["data"]["responseList"][0].update(change)
            bodies.append(body)
        for rows in ([], self.coupang_response()["data"]["responseList"][:1],
                     self.coupang_response()["data"]["responseList"][:1] * 2):
            body = self.coupang_response()
            body["data"]["responseList"] = rows
            bodies.append(body)
        body = self.coupang_response()
        body["data"]["responseCode"] = 1
        bodies.append(body)
        for body in bodies:
            result = interpret_tracking_batch(batch, body)
            self.assertEqual("RECONCILE_REQUIRED", result.decision)
            self.assertFalse(result.resend_authorized)


if __name__ == "__main__":
    unittest.main()
