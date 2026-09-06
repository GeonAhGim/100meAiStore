import copy
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from packages.store_core.channel_order_contracts import ContractQuarantine
from packages.store_core.offline_catalog_contracts import (
    build_coupang_catalog_review, reconcile_catalog_fixture, verify_catalog_fixture_review,
)
from packages.store_core.supplier_file_contracts import HEADERS, parse_supplier_csv


class OfflineCatalogContractTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 7, tzinfo=timezone.utc)
        csv = ','.join(HEADERS) + '\n001,000123,fixture-model,fixture-brand,fixture-spec,blue,one,10,500,KRW,2026-09-07T00:00:00+00:00\n'
        self.report = parse_supplier_csv(csv.encode(), supplier_ref="fixture-supplier")
        self.body = {"code": "SUCCESS", "data": {"sellerItemId": 9007199254740997,
                     "amountInStock": 3, "salePrice": 1000, "onSale": True}}
        self.kw = dict(tenant_ref="fixture-tenant", connection_ref="fixture-channel",
                       vendor_item_id=9007199254740997, supplier_sku="001", mapping_digest="a" * 64,
                       kind="PRICE", proposed_value=1100, channel_observed_at=self.now,
                       now=self.now, expires_at=self.now + timedelta(seconds=60),
                       reserved_quantity=2, safety_buffer=1, quantity_cap=6,
                       variable_cost_minor=50, fee_rate="0.05")

    def build(self, body=None, report=None, **changes):
        return build_coupang_catalog_review(self.body if body is None else body,
            self.report if report is None else report, **(self.kw | changes))

    def verify(self, plan, **changes):
        return verify_catalog_fixture_review(plan, **(dict(approval_digest=plan.approval_digest,
            tenant_ref="fixture-tenant", connection_ref="fixture-channel", now=self.now) | changes))

    def test_fixture_end_to_end_requires_readback(self):
        with patch("socket.socket", side_effect=AssertionError("network prohibited")):
            plan = self.build()
            self.assertEqual("FIXTURE_REVIEW_ONLY", self.verify(plan))
            response = {"code": "SUCCESS"}
            self.assertEqual("READBACK_REQUIRED", reconcile_catalog_fixture(plan, response).decision)
            readback = copy.deepcopy(self.body)
            readback["data"]["salePrice"] = 1100
            result = reconcile_catalog_fixture(plan, response, readback=readback)
        self.assertEqual("MATCHED_READBACK_FIXTURE", result.decision)
        self.assertFalse(result.real_change_confirmed)
        self.assertFalse(result.resend_authorized)
        self.assertFalse(plan.external_write_authorized)
        self.assertFalse(plan.force_sale_price_update)
        self.assertEqual(1000, plan.before_value)
        self.assertNotIn("9007199254740997", repr(plan))

    def test_quantity_reservations_buffer_and_cap(self):
        plan = self.build(kind="QUANTITY", proposed_value=6)
        self.assertEqual(3, plan.before_value)
        for changes in (dict(proposed_value=7), dict(proposed_value=6, reserved_quantity=5),
                        dict(proposed_value=True), dict(proposed_value=-1), dict(safety_buffer=True)):
            with self.subTest(changes=changes), self.assertRaises(ContractQuarantine):
                self.build(**(dict(kind="QUANTITY") | changes))
        self.assertEqual(0, self.build(kind="QUANTITY", proposed_value=0, reserved_quantity=50).proposed_value)

    def test_price_unit_range_and_margin(self):
        for value in (0, 499, 1001, 2001, 2010, True):
            with self.subTest(value=value), self.assertRaises(ContractQuarantine):
                self.build(proposed_value=value)
        self.assertEqual(2000, self.build(proposed_value=2000).proposed_value)
        with self.assertRaisesRegex(ContractQuarantine, "projected_margin_below_threshold"):
            self.build(proposed_value=600)
        with self.assertRaisesRegex(ContractQuarantine, "invalid_fixture_cost_policy"):
            self.build(fee_rate="NaN")

    def test_readiness_identity_and_currency_stop(self):
        for change in (dict(vendor_item_id=8), dict(supplier_sku="other"), dict(kind="DELETE"),
                       dict(mapping_digest="invalid")):
            with self.assertRaises(ContractQuarantine):
                self.build(**change)
        with self.assertRaises(ContractQuarantine):
            self.build(report=replace(self.report, rows=()))
        row = replace(self.report.rows[0], currency="USD")
        with self.assertRaises(ContractQuarantine):
            self.build(report=replace(self.report, rows=(row,)))
        for change in (dict(onSale=False), dict(onSale=1), dict(sellerItemId="9007199254740997")):
            body = copy.deepcopy(self.body)
            body["data"].update(change)
            with self.assertRaises(ContractQuarantine):
                self.build(body=body)

    def test_stale_supplier_and_channel_observations_stop(self):
        for change in (dict(channel_observed_at=self.now - timedelta(seconds=300)),
                       dict(channel_observed_at=self.now + timedelta(seconds=1)),
                       dict(expires_at=self.now), dict(max_age_seconds=True)):
            with self.assertRaises(ContractQuarantine):
                self.build(**change)
        for stamp in ("2026-09-06T23:55:00+00:00", "2026-09-07T00:00:01+00:00", "invalid"):
            row = replace(self.report.rows[0], observed_at=stamp)
            with self.assertRaises(ContractQuarantine):
                self.build(report=replace(self.report, rows=(row,)))

    def test_digest_binds_cost_mapping_context_and_expiry(self):
        plan = self.build()
        changed_plans = [replace(plan, mapping_digest="b" * 64), self.build(fee_rate="0.06"),
                         self.build(quantity_cap=5), replace(plan, proposed_value=1200)]
        for changed in changed_plans:
            with self.assertRaises(ContractQuarantine):
                self.verify(changed, approval_digest=plan.approval_digest)
        for change in (dict(tenant_ref="other"), dict(connection_ref="other"),
                       dict(now=self.now + timedelta(seconds=60)),
                       dict(now=self.now - timedelta(seconds=1))):
            with self.assertRaises(ContractQuarantine):
                self.verify(plan, **change)

    def test_unknown_or_wrong_readback_never_authorizes_retry(self):
        plan = self.build()
        for response in (None, {}, {"code": "ERROR"}, {"code": 200}):
            self.assertEqual("RECONCILE_REQUIRED", reconcile_catalog_fixture(plan, response).decision)
        for change in (dict(sellerItemId=7), dict(salePrice=1000), dict(onSale=False)):
            body = copy.deepcopy(self.body)
            body["data"].update(change)
            result = reconcile_catalog_fixture(plan, {"code": "SUCCESS"}, readback=body)
            self.assertEqual("RECONCILE_REQUIRED", result.decision)
            self.assertFalse(result.resend_authorized)


if __name__ == "__main__":
    unittest.main()
