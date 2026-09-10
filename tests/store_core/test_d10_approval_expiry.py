from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import unittest

from packages.store_core import (ApprovalExpiryWorker, ApprovalKind,
                                 ApprovalState, SQLiteRepository,
                                 StoreControlPlane)
from packages.store_core.domain import CommandState
from packages.store_core.repository import InMemoryRepository


class D10ApprovalExpiryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "expiry.sqlite3"
        self.now = datetime(2026, 1, 2, 3, 0, tzinfo=timezone.utc)
        self.repo = SQLiteRepository(self.path)
        self.app = StoreControlPlane(self.repo, clock=lambda: self.now)
        self.context = self.app.bootstrap_tenant("Expiry", "expiry@example.test")
        self.foreign = self.app.bootstrap_tenant("Foreign", "foreign-expiry@example.test")

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def due(self, context, key):
        return self.app.request_approval(
            context, ApprovalKind.PRODUCT, key, {"sku": key}, key, 1, 1)

    def test_reads_are_pure_even_when_approval_is_due(self):
        command, approval = self.due(self.context, "pure-read")
        self.now += timedelta(hours=24)
        before = (len(self.repo.audits_for(self.context.tenant_id)),
                  len(self.repo.outbox_for(self.context.tenant_id)))
        detail = self.app.approval_detail(self.context, approval.id)
        inbox = self.app.approval_inbox(self.context)
        self.assertEqual("expired", detail["state"])
        self.assertEqual([], detail["actions"])
        self.assertEqual([], inbox["items"])
        self.assertEqual(ApprovalState.PENDING,
                         self.repo.get_approval(self.context.tenant_id, approval.id).state)
        self.assertEqual(CommandState.AWAITING_APPROVAL,
                         self.repo.get_command(self.context.tenant_id, command.id).state)
        self.assertEqual(before, (len(self.repo.audits_for(self.context.tenant_id)),
                                  len(self.repo.outbox_for(self.context.tenant_id))))

    def test_expiry_is_tenant_scoped_bounded_and_restart_idempotent(self):
        first = self.due(self.context, "first")
        second = self.due(self.context, "second")
        foreign = self.due(self.foreign, "foreign")
        self.now += timedelta(hours=24)
        result = self.app.expire_due_approvals(self.context.tenant_id, limit=1)
        self.assertEqual(1, result["expired_count"])
        states = [self.repo.get_approval(self.context.tenant_id, row[1].id).state
                  for row in (first, second)]
        self.assertEqual(1, states.count(ApprovalState.EXPIRED))
        self.assertEqual(ApprovalState.PENDING,
                         self.repo.get_approval(self.foreign.tenant_id, foreign[1].id).state)

        self.repo.close()
        self.repo = SQLiteRepository(self.path)
        self.app = StoreControlPlane(self.repo, clock=lambda: self.now)
        self.app.expire_due_approvals(self.context.tenant_id, limit=10)
        replay = self.app.expire_due_approvals(self.context.tenant_id, limit=10)
        self.assertEqual(0, replay["expired_count"])
        expired = [a for a in self.repo.approvals_for(self.context.tenant_id)
                   if a.state == ApprovalState.EXPIRED]
        events = [e for e in self.repo.outbox_for(self.context.tenant_id)
                  if e.topic == "approval.expired"]
        audits = [e for e in self.repo.audits_for(self.context.tenant_id)
                  if e.action == "approval.expire"]
        self.assertEqual((2, 2, 2), (len(expired), len(events), len(audits)))

    def test_limit_must_be_strictly_bounded(self):
        for value in (0, -1, 1001, True, "1"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.app.expire_due_approvals(self.context.tenant_id, limit=value)

    def test_failure_rolls_back_the_whole_expiry_unit(self):
        command, approval = self.due(self.context, "rollback")
        self.now += timedelta(hours=24)
        original = self.repo.append_outbox
        self.repo.append_outbox = lambda event: (_ for _ in ()).throw(RuntimeError("failpoint"))
        with self.assertRaisesRegex(RuntimeError, "failpoint"):
            self.app.expire_due_approvals(self.context.tenant_id)
        self.repo.append_outbox = original
        self.assertEqual(ApprovalState.PENDING,
                         self.repo.get_approval(self.context.tenant_id, approval.id).state)
        self.assertEqual(CommandState.AWAITING_APPROVAL,
                         self.repo.get_command(self.context.tenant_id, command.id).state)
        self.assertFalse(any(e.action == "approval.expire"
                             for e in self.repo.audits_for(self.context.tenant_id)))
        self.assertFalse(any(e.topic == "approval.expired"
                             for e in self.repo.outbox_for(self.context.tenant_id)))

    def test_independent_workers_have_one_winner(self):
        _, approval = self.due(self.context, "concurrent")
        self.now += timedelta(hours=24)
        tenant_id = self.context.tenant_id
        results, errors = [], []
        barrier = threading.Barrier(2)

        def run():
            repo = SQLiteRepository(self.path)
            app = StoreControlPlane(repo, clock=lambda: self.now)
            try:
                barrier.wait(timeout=5)
                results.append(app.expire_due_approvals(tenant_id)["expired_count"])
            except BaseException as exc:  # captured for an assertion in the main test thread
                errors.append(exc)
            finally:
                repo.close()

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual([], errors)
        self.assertEqual([0, 1], sorted(results))
        self.assertEqual(ApprovalState.EXPIRED,
                         self.repo.get_approval(tenant_id, approval.id).state)
        self.assertEqual(1, sum(e.topic == "approval.expired"
                                for e in self.repo.outbox_for(tenant_id)))

    def test_in_memory_adapter_has_matching_idempotency(self):
        repo = InMemoryRepository()
        app = StoreControlPlane(repo, clock=lambda: self.now)
        context = app.bootstrap_tenant("Memory", "memory-expiry@example.test")
        _, approval = app.request_approval(
            context, ApprovalKind.PRODUCT, "memory", {}, "memory", 1, 1)
        self.now += timedelta(hours=24)
        self.assertEqual(1, app.expire_due_approvals(context.tenant_id)["expired_count"])
        self.assertEqual(0, app.expire_due_approvals(context.tenant_id)["expired_count"])
        self.assertEqual(ApprovalState.EXPIRED,
                         repo.get_approval(context.tenant_id, approval.id).state)

    def test_scheduler_neutral_worker_runs_one_tenant_batch(self):
        _, approval = self.due(self.context, "worker")
        self.now += timedelta(hours=24)
        result = ApprovalExpiryWorker(
            self.app, self.context.tenant_id, batch_size=1).run_once()
        self.assertEqual([approval.id], result["approval_ids"])


if __name__ == "__main__":
    unittest.main()
