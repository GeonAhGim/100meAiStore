from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from packages.store_core import ApprovalKind, ConflictError
from packages.store_core.domain import AdapterCapability, AdapterCapabilityManifest, AttemptState, OutboxState
from packages.store_core.execution import DemoExecutionControlPlane
from packages.store_core.sqlite_repository import SQLiteRepository
from packages.store_core.synthetic_provider import DurableSyntheticProvider
from packages.store_core.tool_worker import DemoToolCommandWorker


class DemoToolCommandWorkerTests(unittest.TestCase):
    def test_approved_tool_command_executes_once_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store_path = Path(directory) / "store.sqlite3"
            provider_path = Path(directory) / "provider.sqlite3"
            now = datetime(2026, 9, 8, tzinfo=timezone.utc)
            repo = SQLiteRepository(store_path)
            service = DemoExecutionControlPlane(repo, lambda: now)
            context = service.bootstrap_tenant("worker", "worker@example.test")
            command, approval = service.request_approval(
                context, ApprovalKind.PRODUCT, "offer:offer-1",
                {"price_minor": 12000}, "worker-approval", 1, 1)
            service.decide(context, command.id, True, "reviewed")
            service.set_demo_control(context, command.id, 1, 1)
            service.register_adapter_manifest(context, AdapterCapabilityManifest(
                context.tenant_id, "synthetic", "demo", "synthetic-v1",
                frozenset({AdapterCapability.DEMO_EXECUTE, AdapterCapability.DEMO_LOOKUP}),
                frozenset(), now))
            submitted = service.submit_demo_tool(
                context, actor_type="agent", actor_id="agent-1", tool="update_price",
                target_type="offer", target_id="offer-1", input_value={"price_minor": 12000},
                idempotency_key="worker-tool", requested_policy_version=1, approval_id=approval.id)
            event = next(row for row in repo.outbox_for(context.tenant_id) if row.topic == "tool.command")
            provider = DurableSyntheticProvider(provider_path)

            result = DemoToolCommandWorker(service, provider, "worker-1").process(context, event.id)
            self.assertEqual(AttemptState.VERIFIED_SUCCESS, result.state)
            self.assertEqual(1, provider.effect_count(context.tenant_id))
            self.assertEqual(OutboxState.COMPLETED, next(
                row for row in repo.outbox_for(context.tenant_id) if row.id == event.id).state)
            provider.close(); repo.close()

            repo = SQLiteRepository(store_path)
            service = DemoExecutionControlPlane(repo, lambda: now)
            provider = DurableSyntheticProvider(provider_path)
            with self.assertRaises(ConflictError):
                DemoToolCommandWorker(service, provider, "worker-2").process(context, event.id)
            self.assertEqual(1, provider.effect_count(context.tenant_id))
            stored = repo.get_tool_command(context.tenant_id, submitted["command_id"])
            self.assertEqual(command.id, stored.approval_command_id)
            provider.close(); repo.close()

    def test_timeout_after_effect_reconciles_after_process_restart_without_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            store_path = Path(directory) / "store.sqlite3"
            provider_path = Path(directory) / "provider.sqlite3"
            clock = [datetime(2026, 9, 8, tzinfo=timezone.utc)]
            repo = SQLiteRepository(store_path)
            service = DemoExecutionControlPlane(repo, lambda: clock[0])
            context = service.bootstrap_tenant("recover", "recover@example.test")
            command, approval = service.request_approval(
                context, ApprovalKind.PRODUCT, "offer:offer-r",
                {"price_minor": 13000}, "recover-approval", 1, 1)
            service.decide(context, command.id, True, "reviewed")
            service.set_demo_control(context, command.id, 1, 1)
            service.register_adapter_manifest(context, AdapterCapabilityManifest(
                context.tenant_id, "synthetic", "demo", "synthetic-v1",
                frozenset({AdapterCapability.DEMO_EXECUTE, AdapterCapability.DEMO_LOOKUP}),
                frozenset(), clock[0]))
            service.submit_demo_tool(
                context, actor_type="agent", actor_id="agent-1", tool="update_price",
                target_type="offer", target_id="offer-r", input_value={"price_minor": 13000},
                idempotency_key="recover-tool", requested_policy_version=1, approval_id=approval.id)
            event = next(row for row in repo.outbox_for(context.tenant_id) if row.topic == "tool.command")
            provider = DurableSyntheticProvider(provider_path, mode="timeout_after")
            first = DemoToolCommandWorker(service, provider, "worker-1").process(context, event.id)
            self.assertEqual(AttemptState.UNKNOWN, first.state)
            self.assertEqual(1, provider.effect_count(context.tenant_id))
            provider.close(); repo.close()

            clock[0] = clock[0].replace(minute=6)
            repo = SQLiteRepository(store_path)
            service = DemoExecutionControlPlane(repo, lambda: clock[0])
            provider = DurableSyntheticProvider(provider_path)
            recovered = DemoToolCommandWorker(service, provider, "worker-2").process(context, event.id)
            self.assertEqual(AttemptState.VERIFIED_SUCCESS, recovered.state)
            self.assertEqual(1, provider.effect_count(context.tenant_id))
            self.assertEqual(OutboxState.COMPLETED, next(
                row for row in repo.outbox_for(context.tenant_id) if row.id == event.id).state)
            provider.close(); repo.close()

    def test_process_next_polls_and_isolates_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            store_path = Path(directory) / "store.sqlite3"
            provider_path = Path(directory) / "provider.sqlite3"
            clock = [datetime(2026, 9, 8, tzinfo=timezone.utc)]
            repo = SQLiteRepository(store_path)
            service = DemoExecutionControlPlane(repo, lambda: clock[0])
            context = service.bootstrap_tenant("poll", "poll@example.test")
            command, approval = service.request_approval(
                context, ApprovalKind.PRODUCT, "offer:offer-p",
                {"price_minor": 14000}, "poll-approval", 1, 1)
            service.decide(context, command.id, True, "reviewed")
            service.set_demo_control(context, command.id, 1, 1)
            service.register_adapter_manifest(context, AdapterCapabilityManifest(
                context.tenant_id, "synthetic", "demo", "synthetic-v1",
                frozenset({AdapterCapability.DEMO_EXECUTE, AdapterCapability.DEMO_LOOKUP}),
                frozenset(), clock[0]))
            service.submit_demo_tool(
                context, actor_type="agent", actor_id="agent-1", tool="update_price",
                target_type="offer", target_id="offer-p", input_value={"price_minor": 14000},
                idempotency_key="poll-tool", requested_policy_version=1, approval_id=approval.id)
            provider = DurableSyntheticProvider(provider_path)
            worker = DemoToolCommandWorker(service, provider, "worker-1")

            # Non tool.command events ahead in the queue are failed (retry), not raised,
            # and the loop keeps going until the tool command completes.
            seen_states = []
            for _ in range(10):
                result = worker.process_next(context)
                seen_states.append(None if result is None else result.state)
                if AttemptState.VERIFIED_SUCCESS in seen_states:
                    break
                clock[0] = clock[0] + timedelta(seconds=30)
            self.assertIn(AttemptState.VERIFIED_SUCCESS, seen_states)
            self.assertEqual(1, provider.effect_count(context.tenant_id))
            tool_event = next(row for row in repo.outbox_for(context.tenant_id) if row.topic == "tool.command")
            self.assertEqual(OutboxState.COMPLETED, tool_event.state)
            others = [row for row in repo.outbox_for(context.tenant_id) if row.topic != "tool.command"]
            failed = [row for row in others if row.state in {OutboxState.RETRY, OutboxState.DEAD}]
            self.assertTrue(failed, [row.state for row in others])
            self.assertTrue(all(row.last_error and row.lease_owner is None for row in failed))
            self.assertFalse([row for row in others if row.state == OutboxState.LEASED])

            # Queue drained: nothing claimable until backoff elapses.
            clock[0] = clock[0] + timedelta(seconds=1)
            self.assertIsNone(worker.process_next(context))
            provider.close(); repo.close()


if __name__ == "__main__":
    unittest.main()
