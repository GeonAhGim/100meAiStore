import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from packages.store_core import ApprovalKind
from packages.store_core.channel_claim_contracts import parse_coupang_claim_page
from packages.store_core.domain import AdapterCapability, AdapterCapabilityManifest, AttemptState, Role
from packages.store_core.execution import DemoExecutionControlPlane
from packages.store_core.offline_claim_contracts import build_coupang_return_review
from packages.store_core.return_gateway import request_return_approval, submit_approved_return
from packages.store_core.sqlite_repository import SQLiteRepository
from packages.store_core.synthetic_provider import DurableSyntheticProvider
from packages.store_core.tool_worker import DemoToolCommandWorker
from tests.store_core.test_channel_claim_contracts import claim_fixture


class ReturnGatewayTests(unittest.TestCase):
    def _review(self, now, tenant_ref="tenant-return"):
        body = claim_fixture()
        body["data"][0].update(receiptStatus="VENDOR_WAREHOUSE_CONFIRM", preRefund=False)
        return build_coupang_return_review(
            parse_coupang_claim_page(body), tenant_ref=tenant_ref,
            connection_ref="fixture-channel", vendor_ref="fixture-vendor",
            receipt_id="9007199254740989", order_id="9007199254740993",
            shipment_id="9007199254740995", vendor_item_id="9007199254740997",
            cancel_quantity=1, fixture_amount_krw=1000, fixture_amount_cap_krw=2000,
            withdrawal_review_digest="a" * 64, withdrawal_reviewed=True,
            observed_at=now, withdrawal_observed_at=now, now=now,
            expires_at=now + timedelta(seconds=60))

    def test_return_review_is_bound_to_refund_approval_and_demo_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            now = datetime(2026, 9, 8, tzinfo=timezone.utc)
            store_path, provider_path = Path(directory) / "store.sqlite3", Path(directory) / "provider.sqlite3"
            repo = SQLiteRepository(store_path)
            service = DemoExecutionControlPlane(repo, lambda: now)
            master = service.bootstrap_tenant("Return demo", "master@example.test")
            funds = service.add_member(master, "funds@example.test", [Role.FUNDS])
            review = self._review(now, master.tenant_id)
            command, approval = request_return_approval(
                service, master, review, connection_ref="fixture-channel",
                idempotency_key="return-approval", policy_version=1, target_version=1)
            blocked = submit_approved_return(
                service, master, review, connection_ref="fixture-channel",
                approval_id=approval.id, idempotency_key="pending-return", policy_version=1)
            self.assertEqual("blocked", blocked["state"])
            service.decide(funds, command.id, True, "refund evidence reviewed")
            service.set_demo_control(master, command.id, 1, 1)
            service.register_adapter_manifest(master, AdapterCapabilityManifest(
                master.tenant_id, "synthetic", "demo", "synthetic-v1",
                frozenset({AdapterCapability.DEMO_EXECUTE, AdapterCapability.DEMO_LOOKUP}),
                frozenset(), now))
            accepted = submit_approved_return(
                service, master, review, connection_ref="fixture-channel",
                approval_id=approval.id, idempotency_key="return-command", policy_version=1)
            self.assertEqual("accepted", accepted["state"])
            event = next(row for row in repo.outbox_for(master.tenant_id) if row.topic == "tool.command")
            provider = DurableSyntheticProvider(provider_path)
            result = DemoToolCommandWorker(service, provider, "return-worker").process(master, event.id)
            self.assertEqual(AttemptState.VERIFIED_SUCCESS, result.state)
            self.assertEqual(1, provider.effect_count(master.tenant_id))
            self.assertFalse(review.external_write_authorized)
            provider.close(); repo.close()

    def test_changed_review_and_cross_tenant_scope_cannot_emit_command(self):
        now = datetime(2026, 9, 8, tzinfo=timezone.utc)
        repo = SQLiteRepository(":memory:")
        service = DemoExecutionControlPlane(repo, lambda: now)
        master = service.bootstrap_tenant("Return demo", "master@example.test")
        other = service.bootstrap_tenant("Other", "other@example.test")
        review = self._review(now, master.tenant_id)
        command, approval = request_return_approval(
            service, master, review, connection_ref="fixture-channel",
            idempotency_key="return-approval", policy_version=1, target_version=1)
        with self.assertRaises(Exception):
            submit_approved_return(service, other, review, connection_ref="fixture-channel",
                                   approval_id=approval.id, idempotency_key="cross", policy_version=1)
        changed = self._review(now, master.tenant_id)
        changed = changed.__class__(**{**changed.__dict__, "fixture_amount_krw": 999})
        blocked = submit_approved_return(
            service, master, changed, connection_ref="fixture-channel",
            approval_id=approval.id, idempotency_key="changed", policy_version=1)
        self.assertEqual("blocked", blocked["state"])
        self.assertEqual([], [row for row in repo.outbox_for(master.tenant_id) if row.topic == "tool.command"])
        repo.close()


if __name__ == "__main__":
    unittest.main()
