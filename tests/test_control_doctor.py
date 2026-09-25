"""Doctor: a failed attempt is diagnosed, the fix reaches the next prompt, a fix that fails hands the task off."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from smart_store_control import agent_engine, doctor, escalation, loopguard, pm, triage

# task 20 on 2026-09-23: the agent imported a class that does not exist, twice
GATE = """REVIEW: FAIL (objective gate)

touched tests (1): rc=1
ERROR: test_m4_6_nonce (unittest.loader._FailedTest)
Traceback (most recent call last):
  File "C:\\smart_store\\data\\control\\scratch\\review-20\\tests\\store_core\\test_m4_6_nonce.py", line 23, in <module>
    from packages.store_core.service import StoreService
ImportError: cannot import name 'StoreService' from 'packages.store_core.service' (C:\\x\\service.py)
"""
NOTE = "gate failed: touched tests (1): rc=1"


def _stamp(delta=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=delta)).isoformat().replace("+00:00", "Z")


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        pkg = self.root / "packages" / "store_core"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "service.py").write_text(
            "from .x import thing\n\nclass StoreCoreService:\n    pass\n\ndef issue_nonce(tenant, *, ttl=60):\n    pass\n",
            encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_import_lists_the_real_names(self):
        hit = doctor.probe(GATE, self.root)
        self.assertEqual("import:packages.store_core.service.StoreService", hit["key"])
        self.assertIn("StoreCoreService", hit["fix"])
        self.assertIn("의도한 것은 아마 StoreCoreService", hit["fix"])

    def test_missing_module_lists_siblings(self):
        hit = doctor.probe("ModuleNotFoundError: No module named 'packages.store_core.nonces'", self.root)
        self.assertIn("service", hit["fix"])

    def test_bad_call_shows_the_real_signature(self):
        hit = doctor.probe("TypeError: issue_nonce() got an unexpected keyword argument 'ttl_seconds'", self.root)
        self.assertIn("def issue_nonce(tenant, *, ttl=60)", hit["fix"])

    def test_excerpt_keeps_errors_tests_and_repo_relative_location(self):
        found = doctor.excerpt(GATE)
        self.assertTrue(found["errors"][0].startswith("ImportError: cannot import name 'StoreService'"))
        self.assertEqual(["unittest.loader._FailedTest.test_m4_6_nonce"], found["tests"])
        self.assertEqual(["tests/store_core/test_m4_6_nonce.py:23"], found["raised_at"])

    def test_unexplained_failure_falls_back_to_the_rule(self):
        task = {"id": 1, "note": "agent produced no change: hit max turns (45) without editing a file",
                "attempts": [{"at": _stamp(), "cause": "max_turns", "sig": "x"}]}
        result = doctor.diagnose(task, self.root, use_model=False)
        self.assertEqual("rule", result["source"])
        self.assertEqual(loopguard.instruction_for("max_turns"), result["fix"])


class DoctorLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        self.root = root
        (root / "packages" / "store_core").mkdir(parents=True)
        (root / "packages" / "store_core" / "service.py").write_text("class StoreCoreService:\n    pass\n", encoding="utf-8")
        self.review = root / "task-1.md"
        self.review.write_text(GATE, encoding="utf-8")
        self.tasks = root / "tasks.json"
        self.patches = [mock.patch.object(pm, "TASKS_PATH", self.tasks),
                        mock.patch.object(triage, "TRIAGE_PATH", root / "triage.json"),
                        mock.patch.object(escalation, "HANDOFF_PATH", root / "handoff.md"),
                        mock.patch.object(triage, "hand_to_codex", lambda task: None),
                        mock.patch.object(pm, "effective_capacity", lambda role="local-impl": {
                            "effective": 2, "engine": "claude-local", "aios_available": 2, "handoff_granted": True,
                            "operator_floor": 2})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _row(self):
        return json.loads(self.tasks.read_text(encoding="utf-8"))["tasks"][0]

    def _fail_review(self):
        data = {"next_id": 2, "tasks": [dict(self._row(), status="reviewing", reviewer="r")]} if self.tasks.exists() else \
            {"next_id": 2, "tasks": [{"id": 1, "title": "nonce", "role": "local-impl", "prompt": "do it", "priority": 50,
                                      "status": "reviewing", "reviewer": "r", "started_at": _stamp(60)}]}
        self.tasks.write_text(json.dumps(data), encoding="utf-8")
        pm.finish_review(1, "fail", str(self.review), NOTE)

    def test_a_failure_waits_for_its_diagnosis_and_the_fix_reaches_the_prompt(self):
        self._fail_review()
        pm.requeue_blocked(limit=1)
        self.assertEqual("ready", self._row()["status"])
        self.assertIsNone(pm.claim("local-impl-1"), "claimed before the doctor diagnosed the failure")
        self.assertEqual("diagnosed", doctor.run_once(self.root, use_model=False)["status"])
        row = self._row()
        self.assertEqual("import:packages.store_core.service.StoreService", row["attempts"][-1]["key"])
        prompt = agent_engine.task_prompt(row)
        self.assertIn("이전 시도 실패 진단", prompt)
        self.assertIn("StoreCoreService", prompt)
        self.assertEqual(1, pm.claim("local-impl-1")["id"])

    def test_an_undiagnosed_failure_does_not_stall_the_pool(self):
        self._fail_review()
        data = json.loads(self.tasks.read_text(encoding="utf-8"))
        data["tasks"][0]["attempts"][-1]["at"] = _stamp(loopguard.DIAGNOSIS_WAIT_SECONDS + 5)
        data["tasks"][0]["status"] = "ready"
        self.tasks.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(1, pm.claim("local-impl-1")["id"])

    def test_a_fix_that_does_not_work_hands_the_task_off(self):
        runs = 0
        while self._row_or_none() is None or self._row().get("phase") != "needs_claude" and self._row().get("status") != "escalated":
            self._fail_review()
            doctor.run_once(self.root, use_model=False)
            triage.run_triage(force=True)
            runs += 1
            self.assertLess(runs, 8, "the same diagnosed error kept running")
        # 3 identical errors to detect the loop, 1 more after the diagnosed remedy
        self.assertEqual(loopguard.REPEAT_LIMIT + 1, runs)
        self.assertIn("diagnosed fix did not work", self._row()["loop_guard"]["reason"])
        remedy = [i for i in self._row()["operator_instructions"] if i.get("source") == "loopguard"]
        self.assertIn("StoreCoreService", remedy[0]["text"])

    def _row_or_none(self):
        return self._row() if self.tasks.exists() else None

    def test_a_lane_quota_is_not_the_tasks_failure(self):
        self.tasks.write_text(json.dumps({"next_id": 2, "tasks": [
            {"id": 1, "status": "in_progress", "worker": "gemini-impl-1", "last_error": "real error", "attempts": []}]}),
            encoding="utf-8")
        pm.release(1, "lane paused until X: gemini quota; task returned to the queue")
        row = self._row()
        self.assertEqual(("ready", [], "real error"), (row["status"], row["attempts"], row["last_error"]))


if __name__ == "__main__":
    unittest.main()
