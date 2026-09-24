"""Rules adopted from the AIOS fleet (2026-09-24): Claude lane before Codex, error-only lane faults, no-op done needs proof."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from smart_store_control import escalation, loopguard, pm, triage, worker


class FleetRuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        self.tasks, self.pools = root / "tasks.json", root / "pools.json"
        self.pools.write_text(json.dumps({"pools": {"local-impl": {"size": 1, "engine": "local-http"},
                                                    "claude-impl": {"size": 2, "engine": "claude"}}}), encoding="utf-8")
        self.codex = []
        self.patches = [mock.patch.object(pm, "TASKS_PATH", self.tasks), mock.patch.object(pm, "POOLS_PATH", self.pools),
                        mock.patch.object(triage, "TRIAGE_PATH", root / "triage.json"),
                        mock.patch.object(escalation, "HANDOFF_PATH", root / "handoff.md"),
                        mock.patch.object(triage, "hand_to_codex", lambda task: self.codex.append(task["id"]))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _row(self):
        return json.loads(self.tasks.read_text(encoding="utf-8"))["tasks"][0]

    def test_a_spent_local_task_goes_to_the_claude_lane_once_before_codex(self):
        self.tasks.write_text(json.dumps({"tasks": [{"id": 1, "status": "blocked", "retry_count": 3, "note": "gate failed: x"}]}),
                              encoding="utf-8")
        self.assertEqual("claude_lane", triage.run_triage(force=True)["actions"][0]["action"])
        row = self._row()
        self.assertEqual(("ready", "claude-impl", 0), (row["status"], row["preferred_lane"], row["retry_count"]))
        data = json.loads(self.tasks.read_text(encoding="utf-8"))
        data["tasks"][0].update({"status": "blocked", "retry_count": 3})
        self.tasks.write_text(json.dumps(data), encoding="utf-8")
        triage.run_triage(force=True)
        self.assertEqual([1], self.codex)  # the Claude lane failed too: now the next rung

    def test_report_only_patch_is_rejected_and_classified(self):
        reason = worker.report_only_patch("diff --git a/M4.7-COMPLETION-REPORT.md b/M4.7-COMPLETION-REPORT.md\n")
        self.assertIn("report-only patch", reason)
        self.assertEqual("noop_claim", loopguard.cause_of(reason))
        self.assertIsNone(worker.report_only_patch("diff --git a/docs/implementation/x.md b/docs/implementation/x.md\n"
                                                   "diff --git a/tests/test_x.py b/tests/test_x.py\n"))

    def test_an_already_done_claim_is_not_a_turn_budget_problem(self):
        self.assertEqual("noop_claim", loopguard.cause_of("agent produced no change: 모든 완료조건이 이미 만족되고 있다"))
        self.assertIn("직접 실행", loopguard.instruction_for("noop_claim"))

    def test_reviews_are_not_blocked_by_a_claude_lane_run(self):
        self.tasks.write_text(json.dumps({"tasks": [
            {"id": 1, "role": "local-impl", "status": "in_progress", "worker": "claude-impl-1"},
            {"id": 2, "role": "local-impl", "status": "needs_review", "priority": 50}]}), encoding="utf-8")
        with mock.patch.object(pm, "effective_capacity", lambda role="local-impl": {"effective": 1}):
            self.assertEqual(2, pm.claim_review("local-review-1")["id"])


if __name__ == "__main__":
    unittest.main()
