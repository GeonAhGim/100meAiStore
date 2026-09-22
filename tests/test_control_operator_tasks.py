"""Operator-dictated tasks and instructions reach the worker pool through the normal pipeline."""
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.request import Request, urlopen

from smart_store_control import agent_engine, pm, server


class OperatorTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        self.tasks = root / "tasks.json"
        self.pools = root / "pools.json"
        self.tasks.write_text(json.dumps({"version": 1, "next_id": 20, "tasks": []}), encoding="utf-8")
        self.pools.write_text(json.dumps({"pools": {"local-impl": {"size": 1, "engine": "local-http"},
                                                    "gemini-impl": {"size": 1, "engine": "gemini"}}}), encoding="utf-8")
        (root / "runtime.json").write_text(json.dumps({"handoff_granted": True, "slots": {"operator_floor": 1}}), encoding="utf-8")
        self.patches = [mock.patch.object(pm, "TASKS_PATH", self.tasks), mock.patch.object(pm, "POOLS_PATH", self.pools),
                        mock.patch.object(pm, "CONTROL_DIR", root),
                        mock.patch.object(pm, "snapshot", lambda: {"aios_live": {"available_slots": 0}})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_dictated_task_enters_the_queue_with_lane_preference_and_files(self):
        task = pm.create_operator_task("중복 ID 거부", "주문 라우팅에서 중복 line id를 거부하고 테스트를 추가한다.",
                                       files=["packages/store_core/orders.py", " tests/store_core/test_order_routing.py "], lane="gemini-impl")
        self.assertEqual(20, task["id"])
        self.assertEqual(("ready", "local-impl", "operator", "gemini-impl"), (task["status"], task["role"], task["source"], task["preferred_lane"]))
        self.assertIn("Evidence/files to inspect: packages/store_core/orders.py; tests/store_core/test_order_routing.py.", task["prompt"])
        self.assertIsNone(pm.claim("local-impl-1", "local-impl"))          # preferred lane is gemini
        self.assertEqual(20, pm.claim("gemini-impl-1", "gemini-impl")["id"])
        with self.assertRaises(ValueError):
            pm.create_operator_task("", "x")
        with self.assertRaises(ValueError):
            pm.create_operator_task("t", "x", lane="copilot-impl")

    def test_instructions_are_kept_and_reach_the_next_prompt(self):
        task = pm.create_operator_task("t", "do the thing")
        pm.add_instruction(task["id"], "테스트 이름에 M1-4를 넣어라")
        updated = pm.add_instruction(task["id"], "기존 fixture를 재사용해라")
        self.assertEqual(2, len(updated["operator_instructions"]))
        prompt = agent_engine.task_prompt(updated)
        self.assertIn("운영자 추가 지시", prompt)
        self.assertIn("M1-4", prompt)
        self.assertIn("fixture", prompt)
        with self.assertRaises(ValueError):
            pm.add_instruction(task["id"], "   ")

    def test_http_endpoints_create_and_instruct(self):
        with mock.patch.object(server, "start_recovery", lambda: {"status": "diagnosing"}):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{httpd.server_port}"
            try:
                body = json.dumps({"title": "via api", "instruction": "add a negative test", "files": "tests/a.py, tests/b.py",
                                   "lane": "", "priority": 90, "run_now": True}).encode()
                created = json.loads(urlopen(Request(base + "/api/task/new", data=body, method="POST",
                                                     headers={"Content-Type": "application/json"})).read())
                self.assertEqual("ready", created["task"]["status"])
                self.assertEqual({"status": "diagnosing"}, created["recovery"])
                tid = created["task"]["id"]
                instructed = json.loads(urlopen(Request(f"{base}/api/task/{tid}/instruct", data=b'{"text":"use Decimal"}', method="POST",
                                                        headers={"Content-Type": "application/json"})).read())
                self.assertEqual("use Decimal", instructed["operator_instructions"][0]["text"])
            finally:
                httpd.shutdown()
                httpd.server_close()


if __name__ == "__main__":
    unittest.main()
