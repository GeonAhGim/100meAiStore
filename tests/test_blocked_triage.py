"""Blocked progress items get gate-free preparation work; gates stay human."""
import json
import tempfile
import unittest
from pathlib import Path

from smart_store_aios.blocked_triage import BlockedTriage, offline_prep_status, prep_task_for
from smart_store_aios.config import ProfitPolicy, Settings
from smart_store_aios.db import StoreDB
from smart_store_aios.dev_dashboard import DevDashboardCollector
from smart_store_aios.worker import Worker

PROGRESS = {"schema_version": 2, "default_phase": "p", "phases": [{"id": "p", "title": "P"}], "items": [
    {"id": "P2-09", "title": "platform", "status": "in_progress"},
    {"id": "P2-10", "title": "Authorized Discovery", "status": "blocked", "approval_gate": "G1/G2/G3"},
    {"id": "P2-12", "title": "Bounded release", "status": "blocked", "approval_gate": "G4/G5"},
]}


class BlockedTriageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        (self.root / "docs" / "implementation").mkdir(parents=True)
        (self.root / "docs" / "implementation" / "development-progress.json").write_text(
            json.dumps(PROGRESS), encoding="utf-8")
        self.db = StoreDB(self.root / "data" / "jobs.db")
        self.db.initialize()

    def tearDown(self):
        self.tmp.cleanup()

    def test_prep_task_never_touches_the_gate(self):
        payload = prep_task_for(PROGRESS["items"][1])
        self.assertEqual("prep-p2-10", payload["task_id"])
        self.assertIn("G1:", payload["goal"]); self.assertIn("G3:", payload["goal"])
        self.assertIn("Never register credentials", payload["goal"])
        self.assertEqual(3, len(payload["acceptance"]))
        self.assertIn("fail-closed", payload["acceptance"][2])

    def test_triage_enqueues_once_per_blocked_item_and_is_idempotent(self):
        triage = BlockedTriage(self.db, self.root)
        first = triage.run()
        self.assertEqual(["P2-10", "P2-12"], first["enqueued"])
        second = triage.run()
        self.assertEqual([], second["enqueued"])
        self.assertEqual(["P2-10", "P2-12"], second["kept"])
        self.assertEqual(2, sum(r["count"] for r in self.db.stats()))
        state = json.loads((self.root / "data" / "blocked-triage.json").read_text(encoding="utf-8"))
        self.assertEqual({"P2-10", "P2-12"}, set(state["items"]))
        # progress file untouched: the worker never changes a gated status
        self.assertEqual(PROGRESS, json.loads((self.root / "docs" / "implementation" / "development-progress.json").read_text(encoding="utf-8")))

    def test_dead_prep_job_is_re_enqueued(self):
        triage = BlockedTriage(self.db, self.root)
        triage.run()
        job = self.db.find_job("dev.task", "prep-p2-10")
        self.db.claim("w", 60)
        self.db.fail(job["id"], "w", "boom", 1, permanent=True)
        result = triage.run()
        self.assertEqual(["P2-10"], result["enqueued"])
        state = json.loads((self.root / "data" / "blocked-triage.json").read_text(encoding="utf-8"))
        self.assertEqual(job["id"], state["items"]["P2-10"]["previous_dead_job"])

    def test_worker_runs_triage_job_and_dashboard_shows_prep_status(self):
        settings = Settings(database=self.db.path, dry_run=True, profit=ProfitPolicy(), lease_seconds=60, max_attempts=1)
        worker = Worker(settings)
        worker.repo_root = self.root
        self.db.enqueue("blocked.triage", {"task_id": "blocked-triage"})
        self.assertTrue(worker.run_once())
        self.assertIsNotNone(self.db.find_job("dev.task", "prep-p2-12"))

        view = offline_prep_status(self.root)
        self.assertEqual("queued", view["P2-10"]["status"])
        progress = DevDashboardCollector(self.root, codex_home=self.root / "nocodex")._development_progress()
        blocked = {x["id"]: x for x in progress["items"] if x["status"] == "blocked"}
        self.assertEqual("queued", blocked["P2-10"]["offline_prep"]["status"])
        self.assertEqual("blocked", blocked["P2-10"]["status"])  # status itself is unchanged
        self.assertNotIn("offline_prep", {x["id"]: x for x in progress["items"]}["P2-09"])

    def test_missing_state_file_yields_no_prep_view(self):
        self.assertEqual({}, offline_prep_status(self.root))


if __name__ == "__main__":
    unittest.main()
