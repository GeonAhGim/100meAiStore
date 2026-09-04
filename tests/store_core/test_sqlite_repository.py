from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from packages.store_core import ApprovalKind, ApprovalState, ConflictError, SQLiteRepository, StoreControlPlane, TenantBoundaryError


class Clock:
    def __init__(self): self.now = datetime(2026, 9, 4, tzinfo=timezone.utc)
    def __call__(self): return self.now


class SQLiteRepositoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "demo.sqlite3"
        self.clock = Clock()

    def tearDown(self): self.temp.cleanup()

    def open(self):
        repo = SQLiteRepository(self.path)
        return repo, StoreControlPlane(repo, self.clock)

    def test_restart_restores_tenant_membership_command_approval_audit_and_outbox(self):
        repo, app = self.open()
        master = app.bootstrap_tenant("Durable", "master@example.test")
        command, _ = app.create_command(master, ApprovalKind.PRODUCT, "product:1", {"price": 1000}, "product:1:v1")
        self.assertTrue(app.verify_audit_chain(master.tenant_id))
        repo.close()

        repo, restarted = self.open()
        restored = restarted.context_for(master.tenant_id, master.user_id)
        self.assertEqual(command.id, repo.get_command(master.tenant_id, command.id).id)
        self.assertEqual(ApprovalState.PENDING, repo.get_approval_for_command(master.tenant_id, command.id).state)
        self.assertEqual(1, len(repo.outbox_for(master.tenant_id)))
        self.assertTrue(restarted.verify_audit_chain(master.tenant_id))
        self.assertEqual(command.id, restarted.create_command(restored, ApprovalKind.PRODUCT, "product:1", {"price": 1000}, "product:1:v1")[0].id)
        repo.close()

    def test_command_unit_is_atomic_when_outbox_write_fails(self):
        repo, app = self.open()
        master = app.bootstrap_tenant("Atomic", "atomic@example.test")
        original = repo.append_outbox
        repo.append_outbox = lambda event: (_ for _ in ()).throw(RuntimeError("simulated crash"))
        with self.assertRaises(RuntimeError):
            app.create_command(master, ApprovalKind.PRODUCT, "product:crash", {}, "crash:v1")
        repo.append_outbox = original
        self.assertIsNone(repo.command_id_for_key(master.tenant_id, "crash:v1"))
        self.assertEqual(1, len(repo.audits_for(master.tenant_id)))
        self.assertEqual(0, len(repo.outbox_for(master.tenant_id)))
        repo.close()

    def test_outbox_checkpoint_survives_restart_and_fencing_rejects_stale_worker(self):
        repo, app = self.open()
        master = app.bootstrap_tenant("Worker", "worker@example.test")
        app.create_command(master, ApprovalKind.PRODUCT, "product:work", {}, "work:v1")
        event = repo.outbox_for(master.tenant_id)[0]
        first = repo.claim_outbox(master.tenant_id, event.id, "worker-a", self.clock.now, self.clock.now + timedelta(minutes=1))
        repo.checkpoint_outbox(master.tenant_id, event.id, "worker-a", first.fencing_token, {"step": 2}, self.clock.now)
        repo.close()

        repo, _ = self.open()
        self.clock.now += timedelta(minutes=2)
        second = repo.claim_outbox(master.tenant_id, event.id, "worker-b", self.clock.now, self.clock.now + timedelta(minutes=1))
        self.assertGreater(second.fencing_token, first.fencing_token)
        self.assertEqual({"step": 2}, second.checkpoint)
        with self.assertRaises(ConflictError):
            repo.checkpoint_outbox(master.tenant_id, event.id, "worker-a", first.fencing_token, {"step": 3}, self.clock.now)
        repo.checkpoint_outbox(master.tenant_id, event.id, "worker-b", second.fencing_token, {"step": "done"}, self.clock.now, completed=True)
        repo.close()

        repo, _ = self.open()
        self.assertEqual("completed", repo.outbox_for(master.tenant_id)[0].state.value)
        self.assertEqual({"step": "done"}, repo.outbox_for(master.tenant_id)[0].checkpoint)
        repo.close()

    def test_outbox_tenant_boundary(self):
        repo, app = self.open()
        first = app.bootstrap_tenant("First", "first@example.test")
        second = app.bootstrap_tenant("Second", "second@example.test")
        app.create_command(first, ApprovalKind.PRODUCT, "product:one", {}, "one:v1")
        event = repo.outbox_for(first.tenant_id)[0]
        with self.assertRaises(TenantBoundaryError):
            repo.claim_outbox(second.tenant_id, event.id, "worker", self.clock.now, self.clock.now + timedelta(minutes=1))
        command = repo.get_command(first.tenant_id, event.aggregate_ref)
        with self.assertRaises(TenantBoundaryError):
            app.decide(second, command.id, True, "blocked")
        self.assertEqual("command.cross_tenant_access", repo.audits_for(second.tenant_id)[-1].action)
        repo.close()


if __name__ == "__main__": unittest.main()
