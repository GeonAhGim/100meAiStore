import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from packages.store_core import ApprovalKind
from packages.store_core.channel_order_contracts import ContractQuarantine
from packages.store_core.domain import AdapterCapability, AdapterCapabilityManifest, AttemptState, Role
from packages.store_core.execution import DemoExecutionControlPlane
from packages.store_core.offline_return_batch import (
    FixtureReturnResult, build_return_batch, reconcile_return_batch, verify_return_batch,
)
from packages.store_core.return_batch_gateway import (
    request_return_batch_approval, submit_approved_return_batch,
)
from packages.store_core.sqlite_repository import SQLiteRepository
from packages.store_core.synthetic_provider import DurableSyntheticProvider
from packages.store_core.tool_worker import DemoToolCommandWorker
from tests.store_core.test_offline_claim_contracts import OfflineReturnContractTest


class ReturnBatchGatewayTests(unittest.TestCase):
    def _batch(self, now, tenant_ref):
        fixture = OfflineReturnContractTest(); fixture.setUp()
        self.assertEqual(fixture.now, now)
        first = fixture.build(tenant_ref=tenant_ref)
        second = replace(first, receipt_id="9007199254740999", order_id="9007199254740998",
                         shipment_id="9007199254740996", vendor_item_id="9007199254740994")
        return build_return_batch((first, second), tenant_ref=tenant_ref,
                                  connection_ref=first.connection_ref, now=now)

    def test_batch_scope_replay_and_result_coverage_fail_closed(self):
        now = datetime(2026, 9, 7, tzinfo=timezone.utc)
        batch = self._batch(now, "fixture-tenant")
        self.assertEqual("FIXTURE_REVIEW_ONLY", verify_return_batch(
            batch, approval_digest=batch.approval_digest, tenant_ref="fixture-tenant",
            connection_ref="fixture-channel", now=now))
        matched = {item.receipt_id: FixtureReturnResult("MATCHED_COMPLETED_FIXTURE") for item in batch.reviews}
        self.assertEqual("MATCHED_COMPLETED_BATCH", reconcile_return_batch(batch, matched).decision)
        for bad in ({}, {batch.reviews[0].receipt_id: FixtureReturnResult("MATCHED_COMPLETED_FIXTURE")},
                    {**matched, "foreign": FixtureReturnResult("MATCHED_COMPLETED_FIXTURE")},
                    {key: FixtureReturnResult("RECONCILE_REQUIRED") for key in matched}):
            self.assertEqual("RECONCILE_REQUIRED", reconcile_return_batch(batch, bad).decision)
        with self.assertRaises(ContractQuarantine):
            build_return_batch((batch.reviews[0], batch.reviews[0]), tenant_ref="fixture-tenant",
                               connection_ref="fixture-channel", now=now)
        with self.assertRaises(ContractQuarantine):
            build_return_batch((replace(batch.reviews[0], tenant_ref="other"),), tenant_ref="fixture-tenant",
                               connection_ref="fixture-channel", now=now)

    def test_batch_approval_is_durable_and_changed_or_foreign_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            now = datetime(2026, 9, 7, tzinfo=timezone.utc)
            repo = SQLiteRepository(Path(directory) / "store.sqlite3")
            clock = [now]
            service = DemoExecutionControlPlane(repo, lambda: clock[0])
            master = service.bootstrap_tenant("Return batch", "master@example.test")
            other = service.bootstrap_tenant("Other", "other@example.test")
            funds = service.add_member(master, "funds@example.test", [Role.FUNDS])
            batch = self._batch(now, master.tenant_id)
            command, approval = request_return_batch_approval(service, master, batch,
                connection_ref=batch.connection_ref, idempotency_key="batch-review", policy_version=1, target_version=1)
            self.assertEqual(ApprovalKind.REFUND, command.kind)
            self.assertEqual("blocked", submit_approved_return_batch(service, master, batch,
                connection_ref=batch.connection_ref, approval_id=approval.id, idempotency_key="pending", policy_version=1)["state"])
            with self.assertRaises(Exception):
                submit_approved_return_batch(service, other, batch, connection_ref=batch.connection_ref,
                    approval_id=approval.id, idempotency_key="foreign", policy_version=1)
            changed = replace(batch, reviews=(replace(batch.reviews[0], fixture_amount_krw=999), batch.reviews[1]))
            self.assertEqual("blocked", submit_approved_return_batch(service, master, changed,
                connection_ref=batch.connection_ref, approval_id=approval.id, idempotency_key="changed", policy_version=1)["state"])
            self.assertEqual([], [row for row in repo.outbox_for(master.tenant_id) if row.topic == "tool.command"])
            service.decide(funds, command.id, True, "batch refund fixture reviewed")
            service.set_demo_control(master, command.id, 1, 1)
            service.register_adapter_manifest(master, AdapterCapabilityManifest(master.tenant_id, "synthetic", "demo", "synthetic-v1",
                frozenset({AdapterCapability.DEMO_EXECUTE, AdapterCapability.DEMO_LOOKUP}), frozenset(), now))
            accepted = submit_approved_return_batch(service, master, batch, connection_ref=batch.connection_ref,
                approval_id=approval.id, idempotency_key="execute", policy_version=1)
            self.assertEqual("accepted", accepted["state"])
            replay = submit_approved_return_batch(service, master, batch, connection_ref=batch.connection_ref,
                approval_id=approval.id, idempotency_key="execute", policy_version=1)
            self.assertEqual(accepted["command_id"], replay["command_id"])
            event = next(row for row in repo.outbox_for(master.tenant_id) if row.topic == "tool.command")
            provider = DurableSyntheticProvider(Path(directory) / "provider.sqlite3", mode="timeout_after")
            worker = DemoToolCommandWorker(service, provider, "return-batch-worker", lease_seconds=1)
            self.assertEqual(AttemptState.UNKNOWN, worker.process(master, event.id).state)
            provider.mode = "success"
            clock[0] += timedelta(seconds=31)
            self.assertEqual(AttemptState.VERIFIED_SUCCESS, worker.process(master, event.id).state)
            self.assertEqual(1, provider.effect_count(master.tenant_id))
            self.assertFalse(batch.external_write_authorized)
            provider.close(); repo.close()
