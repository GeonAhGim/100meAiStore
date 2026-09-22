"""Legacy job queue: failure isolation, lease ownership, retry pacing."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from smart_store_aios.config import ProfitPolicy, Settings
from smart_store_aios.db import LeaseError, StoreDB
from smart_store_aios.worker import Worker


def _settings(directory: str, **overrides) -> Settings:
    base = dict(database=Path(directory) / "jobs.db", dry_run=True, profit=ProfitPolicy(),
                lease_seconds=60, max_attempts=2)
    base.update(overrides)
    return Settings(**base)


class WorkerResilienceTests(unittest.TestCase):
    def test_failed_job_does_not_stop_the_loop_and_retries_in_seconds(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = _settings(directory)
            db = StoreDB(settings.database)
            db.initialize()
            bad = db.enqueue("catalog.scan", {"flaky": True})
            good = db.enqueue("catalog.scan", {"source": "demo"})
            unsupported = db.enqueue("bogus.kind", {})
            worker = Worker(settings)
            original = worker._dispatch

            def dispatch(job):
                if job["payload"].get("flaky"):
                    raise RuntimeError("transient boom")
                return original(job)
            worker._dispatch = dispatch

            self.assertTrue(worker.run_once())  # flaky job: recorded, not raised
            with db.connect() as connection:
                row = connection.execute("SELECT * FROM jobs WHERE id=?", (bad,)).fetchone()
            self.assertEqual("queued", row["status"])
            self.assertIn("transient boom", row["last_error"])
            retry_delay = datetime.fromisoformat(row["available_at"]) - datetime.now(timezone.utc)
            self.assertLess(retry_delay.total_seconds(), 60)  # seconds, not minutes

            self.assertTrue(worker.run_once())  # good job runs while flaky one backs off
            self.assertTrue(worker.run_once())  # unsupported kind is dead-lettered at once
            with db.connect() as connection:
                self.assertEqual("done", connection.execute(
                    "SELECT status FROM jobs WHERE id=?", (good,)).fetchone()[0])
                self.assertEqual("dead", connection.execute(
                    "SELECT status FROM jobs WHERE id=?", (unsupported,)).fetchone()[0])
            self.assertFalse(worker.run_once())

    def test_job_goes_dead_after_max_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StoreDB(Path(directory) / "jobs.db")
            db.initialize()
            job = db.enqueue("bogus.kind", {})
            db.claim("w", 60)
            self.assertEqual("queued", db.fail(job, "w", "first", max_attempts=2))
            with db.connect() as connection:  # make it claimable again immediately
                connection.execute("UPDATE jobs SET available_at='2000-01-01T00:00:00+00:00' WHERE id=?", (job,))
            db.claim("w", 60)
            self.assertEqual("dead", db.fail(job, "w", "second", max_attempts=2))
            self.assertIsNone(db.claim("w", 60))

    def test_complete_and_fail_require_the_current_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StoreDB(Path(directory) / "jobs.db")
            db.initialize()
            job = db.enqueue("catalog.scan", {})
            db.claim("worker-a", 60)
            with self.assertRaises(LeaseError):
                db.complete(job, "worker-b", "x", {})
            with self.assertRaises(LeaseError):
                db.fail(job, "worker-b", "x", 3)
            with db.connect() as connection:  # expire worker-a's lease
                connection.execute("UPDATE jobs SET leased_until='2000-01-01T00:00:00+00:00' WHERE id=?", (job,))
            self.assertEqual(job, db.claim("worker-b", 60)["id"])
            with self.assertRaises(LeaseError):
                db.complete(job, "worker-a", "x", {})  # stale owner cannot overwrite
            db.complete(job, "worker-b", "catalog.scan.completed", {})


if __name__ == "__main__":
    unittest.main()
