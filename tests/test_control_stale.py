"""Stale recovery is keyed on the heartbeat, protected by the ledger lock, and releases orphans at startup."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from smart_store_control import pm


def _stamp(delta_seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=delta_seconds)).isoformat().replace("+00:00", "Z")


class ControlStaleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tasks = Path(self.tmp.name) / "tasks.json"
        self.tasks.write_text(json.dumps({"version": 1, "next_id": 5, "tasks": [
            # long-running but healthy: started 40 min ago, heartbeat 10 s ago
            {"id": 1, "status": "in_progress", "worker": "impl-1", "started_at": _stamp(2400), "heartbeat_at": _stamp(10)},
            # dead holder: heartbeat frozen 20 min ago
            {"id": 2, "status": "in_progress", "worker": "impl-2", "started_at": _stamp(1300), "heartbeat_at": _stamp(1200)},
            # dead reviewer
            {"id": 3, "status": "reviewing", "reviewer": "rev-1", "started_at": _stamp(900), "heartbeat_at": _stamp(900)},
            {"id": 4, "status": "ready"},
        ]}), encoding="utf-8")
        self.patch = mock.patch.object(pm, "TASKS_PATH", self.tasks)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def _statuses(self):
        return {t["id"]: t["status"] for t in json.loads(self.tasks.read_text(encoding="utf-8"))["tasks"]}

    def test_only_frozen_heartbeats_are_requeued(self):
        self.assertEqual([2, 3], pm.requeue_stale())
        self.assertEqual({1: "in_progress", 2: "ready", 3: "needs_review", 4: "ready"}, self._statuses())
        task2 = next(t for t in json.loads(self.tasks.read_text(encoding="utf-8"))["tasks"] if t["id"] == 2)
        self.assertEqual("", task2["worker"])
        self.assertEqual("requeued", task2["phase"])

    def test_startup_releases_every_held_task(self):
        self.assertEqual([1, 2, 3], pm.requeue_stale(stale_after_seconds=0, note="startup"))
        self.assertEqual({1: "ready", 2: "ready", 3: "needs_review", 4: "ready"}, self._statuses())

    def test_requeue_is_idempotent(self):
        pm.requeue_stale()
        self.assertEqual([], pm.requeue_stale())


if __name__ == "__main__":
    unittest.main()
