"""Periodic triage: mechanical remedies for known causes, escalation for the rest, 10-minute throttle."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from smart_store_control import pm, triage


def _stamp(delta=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=delta)).isoformat().replace("+00:00", "Z")


class ControlTriageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        self.tasks = root / "tasks.json"
        self.state = root / "triage.json"
        self.tasks.write_text(json.dumps({"tasks": [
            {"id": 1, "status": "blocked", "retry_count": 1, "note": "local LLM error: TimeoutError"},
            {"id": 2, "status": "blocked", "retry_count": 0, "note": "landing failed: base moved: error: patch failed"},
            {"id": 3, "status": "blocked", "retry_count": 2, "note": "model refused the task: I cannot", "artifact": str(root / "task-3.patch")},
            {"id": 4, "status": "blocked", "retry_count": 3, "note": "gate failed: full suite: rc=1"},
            {"id": 5, "status": "blocked", "retry_count": 1, "note": "gate failed: touched tests"},
            {"id": 6, "status": "needs_decision", "updated_at": _stamp(7 * 3600), "note": "gate passed; model objected"},
            {"id": 7, "status": "in_progress", "worker": "w", "note": "x"},
        ]}), encoding="utf-8")
        self.p1 = mock.patch.object(pm, "TASKS_PATH", self.tasks); self.p1.start()
        self.p2 = mock.patch.object(triage, "TRIAGE_PATH", self.state); self.p2.start()

    def tearDown(self):
        self.p1.stop(); self.p2.stop(); self.tmp.cleanup()

    def _rows(self):
        return {t["id"]: t for t in json.loads(self.tasks.read_text(encoding="utf-8"))["tasks"]}

    def test_classification(self):
        self.assertEqual(("transient", "ready"), triage.classify({"note": "review error: TimeoutError"}))
        self.assertEqual(("base_moved", "rebase"), triage.classify({"note": "landing: base moved"}))
        self.assertEqual(("stale_engine", "ready"), triage.classify({"note": "model refused the task", "artifact": "/nowhere/x.patch"}))
        self.assertEqual(("retries_exhausted", None), triage.classify({"note": "gate failed", "retry_count": 3}))
        self.assertEqual(("implementer", None), triage.classify({"note": "gate failed", "retry_count": 1}))

    def test_run_applies_remedies_escalates_the_rest_and_logs(self):
        result = triage.run_triage()
        rows = self._rows()
        self.assertEqual("ready", rows[1]["status"])                       # transient
        self.assertEqual(("ready", "rebase_needed"), (rows[2]["status"], rows[2]["phase"]))
        self.assertEqual("ready", rows[3]["status"])                       # refusal from the retired engine
        self.assertEqual(("blocked", "needs_operator"), (rows[4]["status"], rows[4]["phase"]))
        self.assertEqual("blocked", rows[5]["status"])                     # left to the normal bounded retry
        self.assertEqual(("needs_decision", "needs_operator"), (rows[6]["status"], rows[6]["phase"]))
        self.assertEqual("in_progress", rows[7]["status"])                 # running tasks untouched
        self.assertEqual(1, rows[1]["retry_count"])                        # no retry cost
        self.assertIn("gate failed", rows[4]["note"])
        kinds = {(a["task"], a["action"]) for a in result["actions"]}
        self.assertEqual({(1, "requeued"), (2, "requeued"), (3, "requeued"), (4, "escalated"), (6, "escalated")}, kinds)
        self.assertEqual(1, len(triage.status()["recent"]))

    def test_throttled_to_the_interval_unless_forced(self):
        triage.run_triage()
        again = triage.run_triage()
        self.assertTrue(again["skipped"])
        self.assertLessEqual(again["next_in_seconds"], triage.INTERVAL_SECONDS)
        forced = triage.run_triage(force=True)
        self.assertFalse(forced["skipped"])
        self.assertEqual([], forced["actions"])                           # idempotent second pass


if __name__ == "__main__":
    unittest.main()
