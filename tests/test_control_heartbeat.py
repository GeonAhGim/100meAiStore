"""Control ledger: heartbeats survive contention, reviewers heartbeat, ids compare as strings."""
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from smart_store_control import pm, state


def _age(stamp: str) -> float:
    return (datetime.now(timezone.utc) - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds()


class ControlHeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tasks = Path(self.tmp.name) / "tasks.json"
        self.tasks.write_text(json.dumps({"version": 1, "next_id": 3, "tasks": [
            {"id": 1, "status": "in_progress", "worker": "impl-1", "heartbeat_at": "2020-01-01T00:00:00Z"},
            {"id": "2", "status": "reviewing", "reviewer": "rev-1", "heartbeat_at": "2020-01-01T00:00:00Z"},
        ]}), encoding="utf-8")
        self.patch = mock.patch.object(pm, "TASKS_PATH", self.tasks)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def _task(self, task_id):
        return next(t for t in json.loads(self.tasks.read_text(encoding="utf-8"))["tasks"] if str(t["id"]) == str(task_id))

    def test_touch_refreshes_worker_and_reviewer_and_matches_string_ids(self):
        self.assertTrue(pm.touch(1, "impl-1", "llm_request"))
        self.assertTrue(pm.touch("2", "rev-1", "review_llm"))      # reviewing tasks heartbeat too
        self.assertFalse(pm.touch(1, "someone-else", "x"))          # not the holder
        self.assertLess(_age(self._task(1)["heartbeat_at"]), 5)
        self.assertLess(_age(self._task(2)["heartbeat_at"]), 5)
        self.assertEqual("review_llm", self._task(2)["phase"])

    def test_heartbeat_loop_survives_a_transient_write_failure(self):
        calls = {"n": 0}
        real = pm.touch

        def flaky(task_id, holder, phase):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError("sharing violation")
            return real(task_id, holder, phase)
        stop = threading.Event()
        with mock.patch.object(pm, "touch", flaky):
            thread = threading.Thread(target=pm.heartbeat_loop, args=(stop, 1, "impl-1", "llm_request", 0.05), daemon=True)
            thread.start()
            time.sleep(0.4)
            stop.set()
            thread.join(2)
        self.assertGreaterEqual(calls["n"], 3)                      # kept beating after the failure
        self.assertLess(_age(self._task(1)["heartbeat_at"]), 5)

    def test_heartbeat_loop_stops_once_the_task_is_finished(self):
        stop = threading.Event()
        thread = threading.Thread(target=pm.heartbeat_loop, args=(stop, 1, "impl-1", "llm_request", 0.05), daemon=True)
        thread.start()
        pm.finish(1, "needs_review", "", "done")
        thread.join(2)
        self.assertFalse(thread.is_alive())                         # touch returned False -> loop exited
        self.assertEqual("needs_review", self._task(1)["status"])

    def test_concurrent_touch_and_finish_do_not_lose_updates(self):
        stop = threading.Event()
        thread = threading.Thread(target=pm.heartbeat_loop, args=(stop, 1, "impl-1", "llm_request", 0.001), daemon=True)
        thread.start()
        time.sleep(0.05)
        pm.finish(1, "needs_review", "artifact.patch", "done")
        stop.set()
        thread.join(2)
        task = self._task(1)
        self.assertEqual("needs_review", task["status"])            # a late heartbeat cannot revert the finish
        self.assertEqual("artifact.patch", task["artifact"])

    def test_write_json_retries_a_windows_sharing_violation(self):
        target = Path(self.tmp.name) / "runtime.json"
        attempts = {"n": 0}
        real_replace = state.os.replace

        def flaky_replace(src, dst):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise PermissionError("in use")
            return real_replace(src, dst)
        with mock.patch.object(state.os, "replace", flaky_replace):
            state.write_json(target, {"ok": True})
        self.assertEqual({"ok": True}, json.loads(target.read_text(encoding="utf-8")))
        self.assertEqual(3, attempts["n"])


if __name__ == "__main__":
    unittest.main()
