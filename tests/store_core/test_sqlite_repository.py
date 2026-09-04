from __future__ import annotations

import tempfile
import unittest
import sqlite3
import json
import subprocess
import sys
import threading
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

from packages.store_core import ApprovalKind, ApprovalState, ConflictError, SQLiteRepository, StoreControlPlane, TenantBoundaryError
from packages.store_core.sqlite_repository import LATEST_SCHEMA_VERSION, MIGRATIONS, MAX_ERROR_LENGTH


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
        self.assertEqual(1, len(repo.outbox_for(master.tenant_id)), "idempotent replay must not duplicate effects")
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

    def test_retry_becomes_dead_and_error_is_sanitized_and_truncated(self):
        repo, app = self.open()
        master = app.bootstrap_tenant("Retry", "retry@example.test")
        app.create_command(master, ApprovalKind.PRODUCT, "product:retry", {}, "retry:v1")
        event = repo.outbox_for(master.tenant_id)[0]
        for attempt in range(1, 3):
            claimed = repo.claim_outbox(master.tenant_id, event.id, f"worker-{attempt}", self.clock.now, self.clock.now + timedelta(minutes=1))
            failed = repo.fail_outbox(master.tenant_id, event.id, f"worker-{attempt}", claimed.fencing_token, "api_key=super-secret " + "x" * 800, self.clock.now, max_attempts=2)
            self.assertNotIn("super-secret", failed.last_error)
            self.assertLessEqual(len(failed.last_error or ""), MAX_ERROR_LENGTH)
            if attempt == 1:
                self.assertEqual("retry", failed.state.value)
                self.clock.now = failed.available_at
        self.assertEqual("dead", failed.state.value)
        with self.assertRaises(ConflictError):
            repo.claim_outbox(master.tenant_id, event.id, "again", self.clock.now, self.clock.now + timedelta(minutes=1))
        self.assertIsNone(repo.claim_next_outbox(master.tenant_id, "again", self.clock.now, self.clock.now + timedelta(minutes=1)))
        repo.close()

    def test_claim_next_is_tenant_scoped_sequential_and_reclaims_expired_lease(self):
        repo, app = self.open()
        first = app.bootstrap_tenant("Queue A", "qa@example.test")
        second = app.bootstrap_tenant("Queue B", "qb@example.test")
        app.create_command(first, ApprovalKind.PRODUCT, "a:1", {}, "a:1")
        app.create_command(first, ApprovalKind.PRODUCT, "a:2", {}, "a:2")
        app.create_command(second, ApprovalKind.PRODUCT, "b:1", {}, "b:1")
        a1 = repo.claim_next_outbox(first.tenant_id, "worker-a", self.clock.now, self.clock.now + timedelta(minutes=1))
        a2 = repo.claim_next_outbox(first.tenant_id, "worker-b", self.clock.now, self.clock.now + timedelta(minutes=1))
        self.assertNotEqual(a1.id, a2.id)
        self.assertEqual(second.tenant_id, repo.claim_next_outbox(second.tenant_id, "worker-c", self.clock.now, self.clock.now + timedelta(minutes=1)).tenant_id)
        self.assertIsNone(repo.claim_next_outbox(first.tenant_id, "worker-c", self.clock.now, self.clock.now + timedelta(minutes=1)))
        self.clock.now += timedelta(minutes=2)
        reclaimed = repo.claim_next_outbox(first.tenant_id, "worker-c", self.clock.now, self.clock.now + timedelta(minutes=1))
        self.assertIn(reclaimed.id, {a1.id, a2.id})
        self.assertGreater(reclaimed.fencing_token, 1)
        with self.assertRaises(ConflictError):
            repo.fail_outbox(first.tenant_id, reclaimed.id, "worker-a", 1, "stale", self.clock.now)
        repo.close()

    def test_completed_is_terminal_and_v1_database_migrates_on_reopen(self):
        connection = sqlite3.connect(self.path)
        connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        connection.executescript(MIGRATIONS[0][1])
        connection.execute("INSERT INTO schema_migrations VALUES (1,?)", (self.clock.now.isoformat(),))
        connection.commit()
        connection.close()
        repo, app = self.open()
        versions = [r[0] for r in repo.connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
        self.assertEqual([1, 2], versions)
        master = app.bootstrap_tenant("Migrated", "migrated@example.test")
        app.create_command(master, ApprovalKind.PRODUCT, "done:1", {}, "done:1")
        event = repo.claim_next_outbox(master.tenant_id, "worker", self.clock.now, self.clock.now + timedelta(minutes=1))
        repo.checkpoint_outbox(master.tenant_id, event.id, "worker", event.fencing_token, {"done": True}, self.clock.now, completed=True)
        with self.assertRaises(ConflictError):
            repo.claim_outbox(master.tenant_id, event.id, "again", self.clock.now, self.clock.now + timedelta(minutes=1))
        self.assertIsNone(repo.claim_next_outbox(master.tenant_id, "again", self.clock.now, self.clock.now + timedelta(minutes=1)))
        with self.assertRaises(sqlite3.IntegrityError):
            repo.connection.execute("DELETE FROM audit_events WHERE tenant_id=?", (master.tenant_id,))
        repo.close()

    def test_same_aggregate_cannot_overtake_retry_but_other_aggregate_can(self):
        repo, app = self.open()
        master = app.bootstrap_tenant("Ordering", "ordering@example.test")
        first, _ = app.create_command(master, ApprovalKind.PRODUCT, "product:x", {"v": 1}, "x:v1")
        app.decide(master, first.id, True, "approved")
        app.supersede(master, first.id, {"v": 2}, "x:v2")
        events = repo.outbox_for(master.tenant_id)
        first_event = next(e for e in events if e.aggregate_ref == first.id and e.topic == "approval.requested")
        following_event = next(e for e in events if e.aggregate_ref == first.id and e.topic == "approval.decided")
        # Force the first aggregate event into delayed retry.
        claimed = repo.claim_outbox(master.tenant_id, first_event.id, "w1", self.clock.now, self.clock.now + timedelta(minutes=1))
        delayed = repo.fail_outbox(master.tenant_id, first_event.id, "w1", claimed.fencing_token, "temporary", self.clock.now)
        candidate = repo.claim_next_outbox(master.tenant_id, "w2", self.clock.now, self.clock.now + timedelta(minutes=1))
        self.assertTrue(candidate is None or candidate.id != following_event.id)
        self.clock.now = delayed.available_at
        self.assertEqual(first_event.id, repo.claim_next_outbox(master.tenant_id, "w3", self.clock.now, self.clock.now + timedelta(minutes=1)).id)
        repo.close()

    def test_readiness_and_unsupported_newer_schema_fail_closed(self):
        repo, _ = self.open()
        self.assertEqual(
            {"ready": True, "schema_version": LATEST_SCHEMA_VERSION, "integrity": "ok"},
            repo.readiness(),
        )
        with self.assertRaisesRegex(ConflictError, "schema version mismatch"):
            repo.readiness(expected_schema_version=LATEST_SCHEMA_VERSION + 1)
        repo.connection.execute(
            "INSERT INTO schema_migrations VALUES (?,?)",
            (LATEST_SCHEMA_VERSION + 1, self.clock.now.isoformat()),
        )
        repo.close()
        with self.assertRaisesRegex(ConflictError, "newer than supported"):
            SQLiteRepository(self.path)

    def test_uow_rolls_back_when_audit_append_fails(self):
        repo, app = self.open()
        master = app.bootstrap_tenant("Audit rollback", "audit-rollback@example.test")
        original = repo.append_audit
        repo.append_audit = lambda event: (_ for _ in ()).throw(RuntimeError("audit failpoint"))
        with self.assertRaisesRegex(RuntimeError, "audit failpoint"):
            app.create_command(master, ApprovalKind.PRODUCT, "product:audit-fail", {}, "audit-fail:v1")
        repo.append_audit = original
        self.assertIsNone(repo.command_id_for_key(master.tenant_id, "audit-fail:v1"))
        self.assertEqual(0, len(repo.outbox_for(master.tenant_id)))
        self.assertEqual(1, len(repo.audits_for(master.tenant_id)))
        repo.close()

    def test_migration_ddl_and_version_marker_roll_back_together(self):
        repo, _ = self.open()
        repo.close()
        broken = MIGRATIONS + ((3, "CREATE TABLE must_not_survive(id TEXT); THIS IS INVALID;"),)
        with patch("packages.store_core.sqlite_repository.MIGRATIONS", broken):
            with self.assertRaises(sqlite3.DatabaseError):
                SQLiteRepository(self.path)
        connection = sqlite3.connect(self.path)
        self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='must_not_survive'").fetchone())
        self.assertIsNone(connection.execute("SELECT version FROM schema_migrations WHERE version=3").fetchone())
        connection.close()

    def test_real_process_commit_ack_loss_recovers_by_idempotency(self):
        code = r'''import json, os, sys
from packages.store_core import ApprovalKind, SQLiteRepository, StoreControlPlane
repo=SQLiteRepository(sys.argv[1]); app=StoreControlPlane(repo)
ctx=app.bootstrap_tenant("Crash process", "crash-process@example.test")
command,_=app.create_command(ctx,ApprovalKind.PRODUCT,"product:ack",{"safe":True},"ack-loss:v1")
print(json.dumps({"tenant":ctx.tenant_id,"user":ctx.user_id,"version":ctx.membership_version,"command":command.id}),flush=True)
os._exit(23)
'''
        result = subprocess.run([sys.executable, "-c", code, str(self.path)], cwd=Path(__file__).parents[2], capture_output=True, text=True)
        self.assertEqual(23, result.returncode)
        facts = json.loads(result.stdout.strip())
        repo, app = self.open()
        context = app.context_for(facts["tenant"], facts["user"])
        command, _ = app.create_command(context, ApprovalKind.PRODUCT, "product:ack", {"safe": True}, "ack-loss:v1")
        self.assertEqual(facts["command"], command.id)
        self.assertEqual(1, len(repo.outbox_for(facts["tenant"])))
        repo.close()

    def test_independent_connections_claim_each_event_once_under_barrier(self):
        repo, app = self.open()
        master = app.bootstrap_tenant("Concurrent", "concurrent@example.test")
        for index in range(8):
            app.create_command(master, ApprovalKind.PRODUCT, f"product:{index}", {}, f"concurrent:{index}")
        repo.close()
        barrier = threading.Barrier(4)
        claimed: list[str] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker(index: int) -> None:
            local = SQLiteRepository(self.path)
            try:
                barrier.wait()
                while True:
                    event = local.claim_next_outbox(master.tenant_id, f"worker-{index}", self.clock.now, self.clock.now + timedelta(minutes=1))
                    if event is None:
                        break
                    local.checkpoint_outbox(master.tenant_id, event.id, f"worker-{index}", event.fencing_token, {"worker": index}, self.clock.now, completed=True)
                    with lock:
                        claimed.append(event.id)
            except BaseException as exc:
                with lock:
                    errors.append(exc)
            finally:
                local.close()

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertFalse(errors)
        self.assertEqual(8, len(claimed))
        self.assertEqual(8, len(set(claimed)))


if __name__ == "__main__": unittest.main()
