"""Control dashboard: operator actions on the ledger and the read/act API."""
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from smart_store_control import pm, recovery, server


LEDGER = {"version": 1, "next_id": 6, "tasks": [
    {"id": 1, "title": "a", "status": "needs_decision", "milestone": "M1", "artifact": "", "note": "gate passed; model objected"},
    {"id": 2, "title": "b", "status": "blocked", "retry_count": 3, "artifact": "", "note": "gate failed: full suite"},
    {"id": 3, "title": "c", "status": "in_progress", "worker": "w", "heartbeat_at": "2020-01-01T00:00:00Z"},
    {"id": 4, "title": "d", "status": "done", "branch": "control/task-4", "commit": "abc1234"},
    {"id": 5, "title": "e", "status": "planned"},
]}


class ControlDashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tasks = Path(self.tmp.name) / "tasks.json"
        self.tasks.write_text(json.dumps(LEDGER), encoding="utf-8")
        self.patcher = mock.patch.object(pm, "TASKS_PATH", self.tasks)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def _status(self, task_id):
        return next(t for t in json.loads(self.tasks.read_text(encoding="utf-8"))["tasks"] if t["id"] == task_id)

    def test_operator_actions_transition_the_ledger_and_refuse_active_tasks(self):
        self.assertEqual("reviewed", pm.operator_action(1, "approve", "looks fine")["status"])
        self.assertIn("looks fine", self._status(1)["note"])
        self.assertEqual("ready", pm.operator_action(2, "retry")["status"])
        self.assertEqual(0, self._status(2)["retry_count"])
        self.assertIn("gate failed", self._status(2)["last_error"])   # why it was blocked is kept
        with self.assertRaises(ValueError):
            pm.operator_action(3, "retry")                            # never yank a running task
        with self.assertRaises(ValueError):
            pm.operator_action(4, "approve")                          # approve only from needs_decision
        self.assertEqual("planned", pm.operator_action(5, "park")["status"])
        self.assertEqual("superseded", pm.operator_action(5, "supersede", "duplicate of 4")["status"])
        self.assertIn("duplicate of 4", self._status(5)["note"])
        with self.assertRaises(ValueError):
            pm.operator_action(5, "explode")

    def test_progress_and_task_detail_views(self):
        view = {"milestones": {"milestones": [
            {"id": "M0.1", "parent": "M0", "status": "done"}, {"id": "M0.2", "parent": "M0", "status": "ready"},
            {"id": "M1.1", "parent": "M1", "status": "done"}]}}
        progress = server.milestone_progress(view)
        self.assertEqual({"total": 3, "done": 2, "percent": 67}, {k: progress[k] for k in ("total", "done", "percent")})
        self.assertEqual([{"id": "M0", "total": 2, "done": 1}, {"id": "M1", "total": 1, "done": 1}], progress["groups"])
        self.assertEqual({"total": 5, "done": 1, "percent": 20}, server.task_progress(LEDGER["tasks"]))
        with mock.patch.object(server, "pm_status", lambda: {"tasks": LEDGER["tasks"]}):
            detail = server.task_detail("4")
            self.assertEqual("control/task-4", detail["task"]["branch"])
            self.assertIsNone(detail["patch"])
            self.assertIsNone(server.task_detail("99"))

    def test_http_api_serves_page_snapshot_and_actions(self):
        fake_view = {"pm": {"tasks": LEDGER["tasks"], "capacity": {}}, "milestones": {"milestones": []},
                     "runtime": {}, "autopilot": {}, "pm_recovery": {}}
        with mock.patch.object(server, "control_view", lambda: fake_view), \
             mock.patch.object(server, "pm_status", lambda: {"tasks": LEDGER["tasks"]}):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{httpd.server_port}"
            try:
                page = urlopen(base + "/").read().decode("utf-8")
                self.assertIn("내가 결정할 일", page)
                snap = json.loads(urlopen(base + "/api/control").read())
                self.assertEqual(5, len(snap["pm"]["tasks"]))
                detail = json.loads(urlopen(base + "/api/task/1").read())
                self.assertEqual("needs_decision", detail["task"]["status"])
                req = Request(base + "/api/task/1/approve", data=b'{"reason":"ok"}', method="POST",
                              headers={"Content-Type": "application/json"})
                self.assertEqual("reviewed", json.loads(urlopen(req).read())["status"])
                with self.assertRaises(HTTPError) as ctx:
                    urlopen(Request(base + "/api/task/3/retry", data=b"{}", method="POST"))
                self.assertEqual(400, ctx.exception.code)
            finally:
                httpd.shutdown()
                httpd.server_close()


if __name__ == "__main__":
    unittest.main()
