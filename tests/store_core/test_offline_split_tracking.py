import copy
import unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from packages.store_core.channel_claim_contracts import parse_coupang_claim_page
from packages.store_core.channel_order_contracts import ContractQuarantine, parse_coupang_day_page
from packages.store_core.offline_split_tracking import build_initial_split_review, build_followup_split_review, verify_split_review
from tests.store_core import test_offline_tracking_contracts as tracking_fixtures
from tests.store_core.test_channel_claim_contracts import claim_fixture


class SplitTrackingTest(unittest.TestCase):
    def setUp(self):
        fixture = tracking_fixtures.OfflineTrackingContractTest()
        fixture.setUp()
        self.now, self.body = fixture.now, fixture.body
        self.body["nextToken"] = ""
        first = self.body["data"][0]["orderItems"][0]
        second = dict(first, sequenceNo="002", vendorItemId=9007199254740998)
        self.body["data"][0]["orderItems"].append(second)
        self.claims = parse_coupang_claim_page({"code": 200, "data": [], "nextToken": ""})
        self.common = dict(tenant_ref="fixture-tenant", connection_ref="fixture-channel",
                           preparation_digest="a" * 64, post_preparation_reviewed=True,
                           observed_at=self.now, claims_observed_at=self.now, now=self.now,
                           expires_at=self.now + timedelta(seconds=60))
        self.kw = self.common | dict(order_id="9007199254740993", shipment_id="9007199254740995",
                                      shipping_item_id="9007199254740997", invoice_number="0000123",
                                      deferred_date="2026-09-08")

    def build(self, body=None, claims=None, **changes):
        return build_initial_split_review(parse_coupang_day_page(self.body if body is None else body),
            self.claims if claims is None else claims, **(self.kw | changes))

    def remapped(self):
        body = copy.deepcopy(self.body)
        shipped = body["data"][0]
        waiting = copy.deepcopy(shipped)
        shipped["shipmentBoxId"] = 9007199254740999
        shipped["status"] = "DEPARTURE"
        shipped["orderItems"] = shipped["orderItems"][:1]
        waiting["orderItems"] = waiting["orderItems"][1:]
        body["data"].append(waiting)
        return body

    def followup(self, prior, body=None, **changes):
        return build_followup_split_review(prior, parse_coupang_day_page(self.remapped() if body is None else body),
            self.claims, **(self.common | dict(prior_approval_digest=prior.approval_digest,
                                               invoice_number="0000456") | changes))

    def test_two_stage_fixture_preserves_item_identity_after_shipment_remap(self):
        with patch("socket.socket", side_effect=AssertionError("network prohibited")):
            initial = self.build()
            following = self.followup(initial)
            for plan in (initial, following):
                self.assertEqual("FIXTURE_REVIEW_ONLY", verify_split_review(plan,
                    approval_digest=plan.approval_digest, tenant_ref="fixture-tenant",
                    connection_ref="fixture-channel", now=self.now))
        self.assertTrue(all(item.split_shipping and not item.pre_split_shipped for item in initial.items))
        self.assertEqual([True, False], [bool(item.invoice_number) for item in initial.items])
        item, = following.items
        self.assertTrue(item.pre_split_shipped)
        self.assertEqual("9007199254740998", item.vendor_item_id)
        self.assertEqual("9007199254740995", item.shipment_id)
        self.assertEqual("", item.estimated_shipping_date)
        self.assertFalse(following.external_write_authorized)
        self.assertNotIn("0000456", repr(following))

    def test_initial_requires_two_complete_items_prepared_review_and_valid_date(self):
        for change in (dict(shipping_item_id="7"), dict(shipment_id="7"), dict(post_preparation_reviewed=1),
                       dict(deferred_date="2026-09-06"), dict(deferred_date="2026-09-15"),
                       dict(deferred_date="2026-9-8"), dict(invoice_number="bad"),
                       dict(observed_at=self.now - timedelta(seconds=300)), dict(max_age_seconds=True)):
            with self.subTest(change=change), self.assertRaises(ContractQuarantine):
                self.build(**change)
        bodies = []
        body = copy.deepcopy(self.body)
        body["data"][0]["orderItems"] = body["data"][0]["orderItems"][:1]
        bodies.append(body)
        body = copy.deepcopy(self.body)
        body["data"][0]["orderItems"][0]["cancelCount"] = 1
        bodies.append(body)
        body = copy.deepcopy(self.body)
        body["nextToken"] = "more"
        bodies.append(body)
        for body in bodies:
            with self.assertRaises(ContractQuarantine):
                self.build(body)
        with self.assertRaisesRegex(ContractQuarantine, "separate_claim_feed_requires_review"):
            self.build(claims=parse_coupang_claim_page(claim_fixture()))

    def test_followup_requires_actual_identity_remap_not_aggregate_success(self):
        prior = self.build()
        with self.assertRaisesRegex(ContractQuarantine, "split_remapping_reconciliation_required"):
            self.followup(prior, self.body)
        for row_index, change in ((0, {"status": "INSTRUCT"}),
                                  (1, {"shipmentBoxId": 7}), (1, {"status": "DEPARTURE"})):
            body = self.remapped()
            body["data"][row_index].update(change)
            with self.assertRaises(ContractQuarantine):
                self.followup(prior, body)
        body = self.remapped()
        body["data"][0]["orderItems"][0]["vendorItemId"] = 7
        with self.assertRaises(ContractQuarantine):
            self.followup(prior, body)
        for change in (dict(invoice_number="0000123"), dict(prior_approval_digest="b" * 64),
                       dict(tenant_ref="other"), dict(post_preparation_reviewed=False)):
            with self.assertRaises(ContractQuarantine):
                self.followup(prior, **change)

    def test_review_digest_binds_each_split_step_and_expires(self):
        plan = self.build()
        altered_item = replace(plan.items[1], estimated_shipping_date="2026-09-09")
        altered = replace(plan, items=(plan.items[0], altered_item))
        for reviewed, now in ((altered, self.now), (plan, self.now + timedelta(seconds=60))):
            with self.assertRaises(ContractQuarantine):
                verify_split_review(reviewed, approval_digest=plan.approval_digest,
                                     tenant_ref="fixture-tenant", connection_ref="fixture-channel", now=now)
        self.assertNotEqual(plan.approval_digest, self.followup(plan).approval_digest)


if __name__ == "__main__":
    unittest.main()
