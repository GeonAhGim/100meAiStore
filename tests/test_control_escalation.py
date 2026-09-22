"""Escalation ladder: local pool -> Codex dev.task -> Claude Code."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from smart_store_aios.db import StoreDB
from smart_store_control import escalation, pm, triage


def _stamp(delta=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=delta)).isoformat().replace("+00:00", "Z")


class EscalationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        self.tasks = root / "tasks.json"
        self.runtime = root / "runtime.json"
        self.codex_db = root / "store.db"
        StoreDB(self.codex_db).initialize()
        self.runtime.write_text(json.dumps({"escalation": {"codex": True, "codex_database": str(self.codex_db)}}), encoding="utf-8")
        self.tasks.write_text(json.dumps({"tasks": [
            {"id": 4, "status": "blocked", "retry_count": 3, "title": "readback contract", "milestone": "M3",
             "prompt": "Implement milestone M3.2 for smart_store only. Scope: readback. Exit criteria: duplicate ids are rejected; roundtrip preserved. Evidence/files to inspect: packages/store_core/orders.py; packages/store_core/order02.py. Work offline.",
             "note": "gate failed: full suite: rc=1", "last_error": "TypeError: create_command() got an unexpected keyword argument"},
        ]}), encoding="utf-8")
        self.patches = [mock.patch.object(pm, "TASKS_PATH", self.tasks), mock.patch.object(triage, "TRIAGE_PATH", root / "triage.json"), mock.patch.object(escalation, "HANDOFF_PATH", root / "handoff.md"),
                        mock.patch.object(escalation, "read_json", lambda path, default: json.loads(self.runtime.read_text(encoding="utf-8")))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _task(self):
        return json.loads(self.tasks.read_text(encoding="utf-8"))["tasks"][0]

    def test_payload_carries_what_the_local_pool_learned(self):
        payload = escalation.dev_task_payload(self._task())
        self.assertEqual("ctl-4", payload["task_id"])
        self.assertEqual(["duplicate ids are rejected", "roundtrip preserved"], payload["acceptance"])
        self.assertEqual(["packages/store_core/orders.py", "packages/store_core/order02.py"], payload["files"])
        self.assertIn("already tried this and failed with: TypeError", payload["goal"])

    def test_exhausted_local_task_is_handed_to_codex_then_follows_the_job(self):
        result = triage.run_triage(force=True)
        self.assertEqual([{"task": 4, "cause": "retries_exhausted", "action": "codex"}], result["actions"])
        task = self._task()
        self.assertEqual(("escalated", "codex"), (task["status"], task["phase"]))
        job = StoreDB(self.codex_db).find_job("dev.task", "ctl-4")
        self.assertEqual("queued", job["status"])
        # waiting: nothing changes but the job state is mirrored
        self.assertEqual([], triage.run_triage(force=True)["actions"])
        self.assertEqual("queued", self._task()["escalation"]["codex"]["status"])
        # Codex finishes and published a commit -> control task done with the branch
        db = StoreDB(self.codex_db)
        with db.connect() as c:
            c.execute("UPDATE jobs SET status='done', checkpoint=? WHERE id=?", (json.dumps({"stage": "done", "commit": "abc123", "spec": "docs/x.md"}), job["id"]))
        result = triage.run_triage(force=True)
        self.assertEqual("done", result["actions"][0]["action"])
        self.assertEqual(("done", "worker/ctl-4", "abc123"), (self._task()["status"], self._task()["branch"], self._task()["commit"]))

    def test_dead_codex_job_raises_to_claude(self):
        triage.run_triage(force=True)
        db = StoreDB(self.codex_db)
        job = db.find_job("dev.task", "ctl-4")
        with db.connect() as c:
            c.execute("UPDATE jobs SET status='dead', last_error='fix budget exhausted' WHERE id=?", (job["id"],))
        result = triage.run_triage(force=True)
        self.assertEqual("claude", result["actions"][0]["action"])
        task = self._task()
        self.assertEqual(("blocked", "needs_claude"), (task["status"], task["phase"]))
        self.assertIn("fix budget exhausted", task["last_error"])

    def test_without_codex_the_task_goes_straight_to_claude(self):
        self.runtime.write_text(json.dumps({"escalation": {"codex": False}}), encoding="utf-8")
        result = triage.run_triage(force=True)
        self.assertEqual("claude", result["actions"][0]["action"])
        self.assertEqual("needs_claude", self._task()["phase"])


if __name__ == "__main__":
    unittest.main()
