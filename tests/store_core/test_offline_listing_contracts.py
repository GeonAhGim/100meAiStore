import copy
import unittest
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from packages.store_core.channel_order_contracts import ContractQuarantine
from packages.store_core.offline_listing_contracts import (
    build_coupang_listing_review, interpret_listing_creation_fixture, verify_listing_fixture_review,
    reconcile_listing_fixture,
)
from packages.store_core.supplier_file_contracts import HEADERS, parse_supplier_csv


def listing_fixture():
    return {
        "displayCategoryCode": 123, "sellerProductName": "Synthetic product", "vendorId": "fixture-vendor",
        "saleStartedAt": "2026-09-07T09:00:00", "saleEndedAt": "2026-10-07T09:00:00",
        "deliveryMethod": "SEQUENCIAL", "deliveryCompanyCode": "KDEXP", "deliveryChargeType": "FREE",
        "deliveryCharge": 0, "freeShipOverAmount": 0, "deliveryChargeOnReturn": 3000,
        "remoteAreaDeliverable": "N", "unionDeliveryType": "NOT_UNION_DELIVERY",
        "returnCenterCode": "fixture-center", "returnChargeName": "PRIVATE FIXTURE",
        "companyContactNumber": "PRIVATE FIXTURE", "returnZipCode": "PRIVATE FIXTURE",
        "returnAddress": "PRIVATE FIXTURE", "returnAddressDetail": "PRIVATE FIXTURE",
        "returnCharge": 3000, "outboundShippingPlaceCode": 123, "vendorUserId": "PRIVATE FIXTURE",
        "requested": False, "items": [{
            "itemName": "Synthetic item", "originalPrice": 1000, "salePrice": 1000,
            "maximumBuyCount": 3, "maximumBuyForPerson": 0, "maximumBuyForPersonPeriod": 1,
            "outboundShippingTimeDay": 1, "unitCount": 1, "adultOnly": "EVERYONE", "taxType": "TAX",
            "parallelImported": "NOT_PARALLEL_IMPORTED", "overseasPurchased": "NOT_OVERSEAS_PURCHASED",
            "pccNeeded": False, "externalVendorSku": "001", "offerCondition": "NEW",
            "certifications": [{"certificationType": "NOT_REQUIRED", "certificationCode": ""}],
            "images": [{"imageOrder": 0, "imageType": "REPRESENTATION", "vendorPath": "https://fixture.invalid/product.png"}],
            "attributes": [{"attributeTypeName": "수량", "attributeValueName": "1개"}],
            "notices": [{"noticeCategoryName": "fixture-notice", "noticeCategoryDetailName": "제조자",
                         "content": "Synthetic manufacturer"}],
            "contents": [{"contentsType": "TEXT", "contentDetails": [{"detailType": "TEXT", "content": "Synthetic content"}]}]}]}


class OfflineListingContractTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 7, tzinfo=timezone.utc)
        csv = ','.join(HEADERS) + '\n001,000123,fixture-model,fixture-brand,fixture-spec,blue,one,10,500,KRW,2026-09-07T00:00:00+00:00\n'
        self.report = parse_supplier_csv(csv.encode(), supplier_ref="fixture-supplier")
        self.metadata = {"fixtureSchema": "listing-category-v1", "categoryCode": 123,
                         "requiredAttributes": ["수량"], "noticeCategory": "fixture-notice",
                         "requiredNotices": ["제조자"], "certificationRequired": False}
        self.kw = dict(tenant_ref="fixture-tenant", connection_ref="fixture-channel", vendor_id="fixture-vendor",
                       supplier_sku="001", rights_digest="a" * 64, now=self.now, observed_at=self.now,
                       expires_at=self.now + timedelta(seconds=60), reserved_quantity=2, safety_buffer=1,
                       quantity_cap=6, variable_cost_minor=50, fee_rate="0.05")

    def build(self, body=None, report=None, metadata=None, **changes):
        return build_coupang_listing_review(listing_fixture() if body is None else body,
            self.report if report is None else report, self.metadata if metadata is None else metadata,
            **(self.kw | changes))

    def verify(self, plan, **changes):
        return verify_listing_fixture_review(plan, **(dict(approval_digest=plan.approval_digest,
            tenant_ref="fixture-tenant", connection_ref="fixture-channel", now=self.now) | changes))

    def test_synthetic_roundtrip_is_immutable_private_and_needs_readback(self):
        with patch("socket.socket", side_effect=AssertionError("network prohibited")):
            plan = self.build()
            self.assertEqual("FIXTURE_REVIEW_ONLY", self.verify(plan))
            result = interpret_listing_creation_fixture({"code": "200", "data": {"code": "SUCCESS", "data": 9007199254740997}})
        self.assertEqual("CREATED_ID_REQUIRES_READBACK", result.decision)
        self.assertEqual(9007199254740997, result.seller_product_id)
        self.assertFalse(result.real_listing_confirmed)
        self.assertFalse(result.resend_authorized)
        self.assertFalse(plan.external_write_authorized)
        self.assertNotIn("PRIVATE FIXTURE", str(asdict(plan)))
        self.assertNotIn("9007199254740997", repr(result))
        with self.assertRaises(FrozenInstanceError):
            plan.quantity = 8

    def test_required_fields_and_unsupported_variants_stop(self):
        valid = listing_fixture()
        for key in valid:
            body = copy.deepcopy(valid)
            del body[key]
            with self.subTest(key=key), self.assertRaises(ContractQuarantine):
                self.build(body)
        for key in valid["items"][0]:
            body = copy.deepcopy(valid)
            del body["items"][0][key]
            with self.subTest(item_key=key), self.assertRaises(ContractQuarantine):
                self.build(body)
        for change in ({"requested": True}, {"requested": 0}, {"vendorId": "other"},
                       {"items": []}, {"items": valid["items"] * 2}, {"autoPricingInfo": {}},
                       {"deliveryCharge": False}, {"outboundShippingPlaceCode": "123"},
                       {"returnCharge": 4501}, {"returnCharge": 2999},
                       {"saleEndedAt": "2026-09-06T09:00:00"}, {"saleStartedAt": "2026-9-7T09:00:00"}):
            body = copy.deepcopy(valid)
            body.update(change)
            with self.subTest(change=change), self.assertRaises(ContractQuarantine):
                self.build(body)

    def test_category_attribute_notice_and_content_checks(self):
        changes = [{"attributes": []}, {"attributes": [{"attributeTypeName": "other", "attributeValueName": "1"}]},
                   {"notices": []}, {"images": []}, {"pccNeeded": "false"},
                   {"externalVendorSku": "other"}, {"certifications": []}, {"salePrice": True},
                   {"contents": [{"contentsType": "HTML", "contentDetails": []}]}]
        for change in changes:
            body = listing_fixture()
            body["items"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ContractQuarantine):
                self.build(body)
        for field in ("attributes", "notices"):
            body = listing_fixture()
            body["items"][0][field] *= 2
            with self.assertRaises(ContractQuarantine):
                self.build(body)
        for change in ({"categoryCode": 7}, {"certificationRequired": True},
                       {"noticeCategory": "other"}, {"requiredAttributes": ["수량", "수량"]}):
            with self.assertRaises(ContractQuarantine):
                self.build(metadata=self.metadata | change)
        body = listing_fixture()
        body["items"][0]["contents"][0]["contentDetails"][0]["content"] = "<script>private</script>"
        with self.assertRaisesRegex(ContractQuarantine, "fixture_plain_text_required"):
            self.build(body)

    def test_margin_stock_cost_and_freshness_fail_closed(self):
        for change in (dict(quantity_cap=2), dict(reserved_quantity=8), dict(safety_buffer=True),
                       dict(fee_rate="NaN"), dict(variable_cost_minor=450),
                       dict(observed_at=self.now - timedelta(seconds=300)),
                       dict(observed_at=self.now + timedelta(seconds=1)),
                       dict(expires_at=self.now), dict(max_age_seconds=True)):
            with self.subTest(change=change), self.assertRaises(ContractQuarantine):
                self.build(**change)
        for timestamp in ("invalid", "2026-09-06T23:55:00+00:00", "2026-09-07T00:00:01+00:00"):
            report = replace(self.report, rows=(replace(self.report.rows[0], observed_at=timestamp),))
            with self.assertRaises(ContractQuarantine):
                self.build(report=report)
        with self.assertRaises(ContractQuarantine):
            self.build(report=replace(self.report, rows=()))

    def test_approval_digest_binds_context_source_and_policy(self):
        plan = self.build()
        for change in (dict(tenant_ref="other"), dict(connection_ref="other"),
                       dict(now=self.now + timedelta(seconds=60)),
                       dict(now=self.now - timedelta(seconds=1))):
            with self.assertRaises(ContractQuarantine):
                self.verify(plan, **change)
        body = listing_fixture()
        body["returnAddress"] = "CHANGED PRIVATE FIXTURE"
        variants = [self.build(body), self.build(rights_digest="b" * 64),
                    self.build(safety_buffer=2), self.build(observed_at=self.now - timedelta(seconds=1)),
                    replace(plan, supplier_digest="c" * 64), replace(plan, metadata_digest="d" * 64)]
        for changed in variants:
            with self.assertRaisesRegex(ContractQuarantine, "approval_digest_mismatch"):
                self.verify(changed, approval_digest=plan.approval_digest)

    def test_unknown_warning_and_malformed_creation_never_allow_resend(self):
        responses = [None, {}, {"code": "ERROR"}, {"code": "SUCCES", "data": 12},
                     {"code": "SUCCESS", "data": True}, {"code": "SUCCESS", "data": "12"},
                     {"code": "SUCCESS", "data": 12, "details": "PRIVATE FIXTURE"},
                     {"code": "SUCCESS", "data": 12, "errorItems": [{}]},
                     {"code": "SUCCESS", "data": 12, "ettorItems": [{}]},
                     {"code": "200", "details": "warning", "data": {"code": "SUCCESS", "data": 12}}]
        for response in responses:
            result = interpret_listing_creation_fixture(response)
            self.assertEqual("RECONCILE_REQUIRED", result.decision)
            self.assertFalse(result.resend_authorized)
            self.assertNotIn("PRIVATE FIXTURE", repr(result))

    def readback(self, status="임시저장"):
        data = listing_fixture()
        data.update(sellerProductId=9007199254740997, statusName=status)
        data["items"][0].update(sellerProductItemId=9007199254740998, isAutoGenerated="false",
                                 vendorItemId=None if status == "임시저장" else 9007199254740999)
        return {"code": "SUCCESS", "data": data}

    def reconcile(self, plan, readback=None, **changes):
        return reconcile_listing_fixture(plan, {"code": "SUCCESS", "data": 9007199254740997},
            **(dict(original_payload=listing_fixture(), readback=self.readback() if readback is None else readback,
                    observed_at=self.now, now=self.now) | changes))

    def test_exact_listing_readback_distinguishes_draft_and_approved_observations(self):
        plan = self.build()
        with patch("socket.socket", side_effect=AssertionError("network prohibited")):
            draft = self.reconcile(plan)
            approved = self.reconcile(plan, self.readback("승인완료"))
        self.assertEqual("MATCHED_DRAFT_FIXTURE", draft.decision)
        self.assertIsNone(draft.vendor_item_id)
        self.assertEqual("MATCHED_APPROVED_FIXTURE", approved.decision)
        self.assertEqual(9007199254740999, approved.vendor_item_id)
        self.assertFalse(approved.real_listing_confirmed)
        self.assertFalse(approved.resend_authorized)
        self.assertNotIn("PRIVATE FIXTURE", repr(approved))

    def test_listing_readback_unknown_partial_foreign_or_changed_is_quarantined(self):
        plan = self.build()
        bodies = []
        for change in ({"sellerProductId": 7}, {"vendorId": "other"}, {"statusName": "부분승인완료"},
                       {"statusName": []}, {"returnAddress": "CHANGED"}, {"items": []}):
            body = self.readback()
            body["data"].update(change)
            bodies.append(body)
        for change in ({"vendorItemId": 7}, {"sellerProductItemId": True}, {"salePrice": 900},
                       {"externalVendorSku": "other"}, {"maximumBuyCount": 4},
                       {"isAutoGenerated": "true"}, {"autoPricingInfoView": {"active": True}}):
            body = self.readback()
            body["data"]["items"][0].update(change)
            bodies.append(body)
        body = self.readback()
        body["data"]["items"] *= 2
        bodies.append(body)
        for body in bodies:
            self.assertEqual("RECONCILE_REQUIRED", self.reconcile(plan, body).decision)
        payload = listing_fixture()
        payload["returnAddress"] = "CHANGED"
        for change in (dict(original_payload=payload), dict(observed_at=self.now - timedelta(seconds=1)),
                       dict(observed_at=self.now + timedelta(seconds=1)), dict(now=self.now + timedelta(seconds=60))):
            self.assertEqual("RECONCILE_REQUIRED", self.reconcile(plan, **change).decision)


if __name__ == "__main__":
    unittest.main()
