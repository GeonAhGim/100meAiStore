import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from packages.store_core.channel_claim_contracts import parse_coupang_claim_page
from packages.store_core.channel_order_contracts import parse_coupang_day_page
from packages.store_core.channel_order_contracts import ContractQuarantine
from packages.store_core.domain import AdapterCapability, AdapterCapabilityManifest, AttemptState, Role
from packages.store_core.execution import DemoExecutionControlPlane
from packages.store_core.offline_tracking_contracts import (
    build_coupang_tracking_review, build_naver_dispatch_review,
)
from packages.store_core.sqlite_repository import SQLiteRepository
from packages.store_core.synthetic_provider import DurableSyntheticProvider
from packages.store_core.tool_worker import DemoToolCommandWorker
from packages.store_core.tracking_gateway import request_tracking_approval, submit_approved_tracking


class TrackingGatewayTests(unittest.TestCase):
    def _review(self, now, tenant_ref):
        source = json.loads((Path(__file__).parents[1] / "fixtures" / "channel_orders.json").read_text(encoding="utf-8"))["coupang"]
        source["data"][0]["orderItems"][0].update(cancelCount=0, holdCountForCancel=0)
        claim = {"code": 200, "data": [], "nextToken": ""}
        return build_coupang_tracking_review(
            parse_coupang_day_page(source), tenant_ref=tenant_ref,
            connection_ref="fixture-coupang", order_id="9007199254740993",
            shipment_id="9007199254740995", vendor_item_id="9007199254740997",
            invoice_number="000012340001", observed_at=now, now=now,
            review_expires_at=now + timedelta(seconds=60),
            preparation_review_digest="a" * 64, post_preparation_reviewed=True,
            claim_page=parse_coupang_claim_page(claim), claims_observed_at=now)

    def test_tracking_review_is_bound_to_purchase_approval_and_demo_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            now = datetime(2026, 9, 8, tzinfo=timezone.utc)
            repo = SQLiteRepository(Path(directory) / "store.sqlite3")
            service = DemoExecutionControlPlane(repo, lambda: now)
            master = service.bootstrap_tenant("Tracking demo", "master@example.test")
            funds = service.add_member(master, "funds@example.test", [Role.FUNDS])
            review = self._review(now, master.tenant_id)
            command, approval = request_tracking_approval(
                service, master, review, connection_ref="fixture-coupang",
                idempotency_key="tracking-approval", policy_version=1, target_version=1)
            blocked = submit_approved_tracking(
                service, master, review, connection_ref="fixture-coupang",
                approval_id=approval.id, idempotency_key="tracking-pending",
                policy_version=1)
            self.assertEqual("blocked", blocked["state"])
            service.decide(funds, command.id, True, "tracking reviewed")
            service.set_demo_control(master, command.id, 1, 1)
            service.register_adapter_manifest(master, AdapterCapabilityManifest(
                master.tenant_id, "synthetic", "demo", "synthetic-v1",
                frozenset({AdapterCapability.DEMO_EXECUTE, AdapterCapability.DEMO_LOOKUP}),
                frozenset(), now))
            accepted = submit_approved_tracking(
                service, master, review, connection_ref="fixture-coupang",
                approval_id=approval.id, idempotency_key="tracking-command",
                policy_version=1)
            self.assertEqual("accepted", accepted["state"])
            event = next(row for row in repo.outbox_for(master.tenant_id) if row.topic == "tool.command")
            provider = DurableSyntheticProvider(Path(directory) / "provider.sqlite3")
            result = DemoToolCommandWorker(service, provider, "tracking-worker").process(master, event.id)
            self.assertEqual(AttemptState.VERIFIED_SUCCESS, result.state)
            self.assertEqual(1, provider.effect_count(master.tenant_id))
            self.assertFalse(review.external_write_authorized)
            provider.close(); repo.close()

    def test_changed_tracking_review_and_cross_tenant_scope_cannot_emit_command(self):
        now = datetime(2026, 9, 8, tzinfo=timezone.utc)
        repo = SQLiteRepository(":memory:")
        service = DemoExecutionControlPlane(repo, lambda: now)
        master = service.bootstrap_tenant("Tracking demo", "master@example.test")
        other = service.bootstrap_tenant("Other", "other@example.test")
        review = self._review(now, master.tenant_id)
        _, approval = request_tracking_approval(
            service, master, review, connection_ref="fixture-coupang",
            idempotency_key="tracking-approval", policy_version=1, target_version=1)
        with self.assertRaises(ContractQuarantine):
            submit_approved_tracking(service, other, review, connection_ref="fixture-coupang",
                                     approval_id=approval.id, idempotency_key="cross",
                                     policy_version=1)
        changed = replace(review, invoice_number="000012340002")
        blocked = submit_approved_tracking(
            service, master, changed, connection_ref="fixture-coupang",
            approval_id=approval.id, idempotency_key="changed", policy_version=1)
        self.assertEqual("blocked", blocked["state"])
        self.assertEqual([], [row for row in repo.outbox_for(master.tenant_id) if row.topic == "tool.command"])
        repo.close()

    def test_naver_dispatch_review_uses_same_typed_shipment_target(self):
        now = datetime(2026, 9, 8, tzinfo=timezone.utc)
        body = json.loads((Path(__file__).parents[1] / "fixtures" / "channel_orders.json").read_text(encoding="utf-8"))["naver"]
        product = body["data"][0]["productOrder"]
        product.update(claimStatus=None, placeOrderStatus="OK", placeOrderDate="2026-09-08T08:00:00+09:00")
        review = build_naver_dispatch_review(
            body, tenant_ref="tenant-naver", connection_ref="fixture-naver",
            product_order_id="fixture-product-order-1", invoice_number="000001234567",
            observed_at=now, now=now, dispatch_date=now,
            review_expires_at=now + timedelta(seconds=60))
        repo = SQLiteRepository(":memory:")
        service = DemoExecutionControlPlane(repo, lambda: now)
        context = service.bootstrap_tenant("Naver demo", "naver@example.test")
        review = replace(review, tenant_ref=context.tenant_id)
        command, _ = request_tracking_approval(
            service, context, review, connection_ref="fixture-naver",
            idempotency_key="naver-tracking", policy_version=1, target_version=1)
        self.assertEqual("shipment:fixture-product-order-1", command.target_ref)
        self.assertEqual("naver-dispatch-fixture-v1", command.payload["contract"])
        repo.close()


if __name__ == "__main__":
    unittest.main()
