import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from packages.store_core import ApprovalKind
from packages.store_core.domain import AdapterCapability, AdapterCapabilityManifest, AttemptState, Role
from packages.store_core.execution import DemoExecutionControlPlane
from packages.store_core.offline_tracking_batch import build_tracking_batch
from packages.store_core.sqlite_repository import SQLiteRepository
from packages.store_core.synthetic_provider import DurableSyntheticProvider
from packages.store_core.tool_worker import DemoToolCommandWorker
from packages.store_core.tracking_batch_gateway import (
    request_tracking_batch_approval, submit_approved_tracking_batch,
)
from tests.store_core import test_offline_tracking_contracts as tracking_fixtures


class TrackingBatchGatewayTests(unittest.TestCase):
    def _batch(self, now, tenant_ref):
        fixture = tracking_fixtures.NaverDispatchContractTest()
        fixture.setUp()
        self.assertEqual(fixture.now, now)
        review = replace(fixture.build(), tenant_ref=tenant_ref)
        return build_tracking_batch(
            (review, replace(review, product_order_id="second")),
            tenant_ref=tenant_ref, connection_ref=review.connection_ref, now=now,
        )

    def test_batch_is_bound_to_one_purchase_approval_and_durable_demo_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            now = datetime(2026, 9, 7, tzinfo=timezone.utc)
            repo = SQLiteRepository(Path(directory) / "store.sqlite3")
            service = DemoExecutionControlPlane(repo, lambda: now)
            master = service.bootstrap_tenant("Tracking batch", "master@example.test")
            funds = service.add_member(master, "funds@example.test", [Role.FUNDS])
            batch = self._batch(now, master.tenant_id)
            command, approval = request_tracking_batch_approval(
                service, master, batch, connection_ref=batch.connection_ref,
                idempotency_key="batch-approval", policy_version=1, target_version=1)
            self.assertEqual(ApprovalKind.PURCHASE, command.kind)
            self.assertEqual("blocked", submit_approved_tracking_batch(
                service, master, batch, connection_ref=batch.connection_ref, approval_id=approval.id,
                idempotency_key="pending-batch", policy_version=1)["state"])
            service.decide(funds, command.id, True, "batch evidence reviewed")
            service.set_demo_control(master, command.id, 1, 1)
            service.register_adapter_manifest(master, AdapterCapabilityManifest(
                master.tenant_id, "synthetic", "demo", "synthetic-v1",
                frozenset({AdapterCapability.DEMO_EXECUTE, AdapterCapability.DEMO_LOOKUP}),
                frozenset(), now))
            accepted = submit_approved_tracking_batch(
                service, master, batch, connection_ref=batch.connection_ref, approval_id=approval.id,
                idempotency_key="batch-command", policy_version=1)
            self.assertEqual("accepted", accepted["state"])
            event = next(row for row in repo.outbox_for(master.tenant_id) if row.topic == "tool.command")
            provider = DurableSyntheticProvider(Path(directory) / "provider.sqlite3")
            result = DemoToolCommandWorker(service, provider, "tracking-batch-worker").process(master, event.id)
            self.assertEqual(AttemptState.VERIFIED_SUCCESS, result.state)
            self.assertEqual(1, provider.effect_count(master.tenant_id))
            self.assertFalse(batch.external_write_authorized)
            provider.close(); repo.close()

    def test_changed_batch_or_cross_tenant_cannot_emit_command(self):
        now = datetime(2026, 9, 7, tzinfo=timezone.utc)
        repo = SQLiteRepository(":memory:")
        service = DemoExecutionControlPlane(repo, lambda: now)
        master = service.bootstrap_tenant("Tracking batch", "master@example.test")
        other = service.bootstrap_tenant("Other", "other@example.test")
        batch = self._batch(now, master.tenant_id)
        command, approval = request_tracking_batch_approval(
            service, master, batch, connection_ref=batch.connection_ref,
            idempotency_key="batch-approval", policy_version=1, target_version=1)
        with self.assertRaises(Exception):
            submit_approved_tracking_batch(service, other, batch, connection_ref=batch.connection_ref,
                                           approval_id=approval.id, idempotency_key="cross", policy_version=1)
        changed = replace(batch, reviews=(replace(batch.reviews[0], invoice_number="999"), batch.reviews[1]))
        self.assertEqual("blocked", submit_approved_tracking_batch(
            service, master, changed, connection_ref=batch.connection_ref, approval_id=approval.id,
            idempotency_key="changed", policy_version=1)["state"])
        self.assertEqual([], [row for row in repo.outbox_for(master.tenant_id) if row.topic == "tool.command"])
        repo.close()


if __name__ == "__main__":
    unittest.main()
