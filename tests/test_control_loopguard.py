"""Loop guard: a task that keeps failing the same way is diagnosed once, then handed off."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from smart_store_control import escalation, loopguard, pm, triage


def _stamp(delta=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=delta)).isoformat().replace("+00:00", "Z")


def _attempts(note, n, *, seconds=60, start=3600):
    return [{"at": _stamp(start - i), "kind": "implement", "cause": loopguard.cause_of(note),
             "sig": loopguard.signature(note), "seconds": seconds} for i in range(n)]


STRAY = "agent wrote outside its worktree (edits quarantined, checkout restored): tests/store_core/test_x.py"


class LoopGuardUnitTests(unittest.TestCase):
    def test_signature_ignores_ids_paths_and_counts(self):
        a = loopguard.signature("gate failed: touched tests (1): rc=1 C:\\smart_store\\data\\x.patch")
        b = loopguard.signature("gate failed: touched tests (3): rc=1 C:\\other\\y.patch")
        self.assertEqual(a, b)

    def test_causes(self):
        self.assertEqual("patch_apply", loopguard.cause_of("gate failed: patch does not apply at HEAD"))
        self.assertEqual("gate_tests", loopguard.cause_of("gate failed: full suite: rc=1"))
        self.assertEqual("stray_edits", loopguard.cause_of(STRAY))
        self.assertEqual("stray_edits", loopguard.cause_of(
            "agent produced no change: 2 write(s) to the live checkout denied; hit max turns (46) without editing a file"))
        self.assertEqual("max_turns", loopguard.cause_of("agent produced no change: hit max turns (46) without editing a file"))
        self.assertEqual("wall_clock", loopguard.cause_of("lane paused until X: claude-local wall clock exceeded, model congested"))
        self.assertEqual("orphaned", loopguard.cause_of("requeued at startup: holder process is gone"))

    def test_no_loop_below_limits(self):
        self.assertIsNone(loopguard.verdict({"id": 1, "attempts": _attempts(STRAY, 2)}))

    def test_same_failure_three_times_is_remediated_with_an_instruction(self):
        v = loopguard.verdict({"id": 1, "attempts": _attempts(STRAY, 3)})
        self.assertEqual("remediate", v["action"])
        self.assertEqual("stray_edits", v["cause"])
        self.assertIn("상대 경로", v["instruction"])

    def test_mixed_failures_are_bounded_by_max_attempts(self):
        notes = ["gate failed: full suite: rc=1", STRAY, "agent produced no change: x"] * 2
        attempts = [a for n in notes for a in _attempts(n, 1)]
        self.assertEqual(loopguard.MAX_ATTEMPTS, len(attempts))
        self.assertEqual("remediate", loopguard.verdict({"id": 1, "attempts": attempts})["action"])

    def test_unfixable_cause_escalates_without_a_remedy(self):
        v = loopguard.verdict({"id": 1, "attempts": _attempts("requeued at startup: holder process is gone", 3)})
        self.assertEqual(("escalate", "orphaned"), (v["action"], v["cause"]))

    def test_time_budget_escalates_as_inefficient(self):
        v = loopguard.verdict({"id": 1, "attempts": _attempts(STRAY, 2, seconds=loopguard.TIME_BUDGET_SECONDS)})
        self.assertEqual("escalate", v["action"])
        self.assertIn("not efficient", v["reason"])

    def test_after_remedy_the_same_failure_twice_escalates(self):
        guard = {"stage": "remediated", "cause": "stray_edits", "at": _stamp(1000)}
        task = {"id": 1, "loop_guard": guard, "attempts": _attempts(STRAY, 3, start=3600) + _attempts(STRAY, 1, start=500)}
        self.assertIsNone(loopguard.verdict(task))  # one run after the remedy is not a loop yet
        task["attempts"] += _attempts(STRAY, 1, start=100)
        self.assertEqual("escalate", loopguard.verdict(task)["action"])

    def test_a_test_failing_in_two_tasks_gates_is_the_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            review = "REVIEW: FAIL (objective gate)\n\nfull suite: rc=1\nERROR: test_a (tests.store_core.test_b02.B02Tests)\n"
            paths = []
            for i in (1, 2):
                path = Path(tmp) / f"task-{i}.md"
                path.write_text(review, encoding="utf-8")
                paths.append(str(path))
            note = "gate failed: full suite: rc=1"
            mine = {"id": 1, "note": note, "review_artifact": paths[0], "attempts": _attempts(note, 3)}
            other = {"id": 2, "note": note, "review_artifact": paths[1]}
            v = loopguard.verdict(mine, [mine, other])
            self.assertEqual(("escalate", "baseline_red"), (v["action"], v["cause"]))
            self.assertIn("tests.store_core.test_b02.B02Tests.test_a", v["reason"])
            # a single fresh failure is never judged against the base
            self.assertIsNone(loopguard.verdict(dict(mine, attempts=_attempts(note, 1)), [mine, other]))


class LoopGuardLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        self.tasks = root / "tasks.json"
        self.handoff = root / "handoff.md"
        self.patches = [mock.patch.object(pm, "TASKS_PATH", self.tasks),
                        mock.patch.object(triage, "TRIAGE_PATH", root / "triage.json"),
                        mock.patch.object(escalation, "HANDOFF_PATH", self.handoff),
                        mock.patch.object(triage, "hand_to_codex", lambda task: None)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _write(self, *tasks):
        self.tasks.write_text(json.dumps({"next_id": 99, "tasks": list(tasks)}), encoding="utf-8")

    def _row(self, task_id=1):
        return next(t for t in json.loads(self.tasks.read_text(encoding="utf-8"))["tasks"] if t["id"] == task_id)

    def _run_and_fail(self, note):
        """One worker run: claim-free transition to in_progress, then a blocked finish."""
        data = json.loads(self.tasks.read_text(encoding="utf-8"))
        data["tasks"][0].update({"status": "in_progress", "worker": "local-impl-1", "started_at": _stamp()})
        self.tasks.write_text(json.dumps(data), encoding="utf-8")
        pm.finish(1, "blocked", note=note)

    def test_finish_review_and_orphaning_record_attempts(self):
        self._write({"id": 1, "status": "in_progress", "worker": "w", "started_at": _stamp(120)},
                    {"id": 2, "status": "reviewing", "reviewer": "r", "heartbeat_at": _stamp(3600)})
        pm.finish(1, "blocked", note=STRAY)
        self.assertEqual("stray_edits", self._row(1)["attempts"][0]["cause"])
        self.assertGreaterEqual(self._row(1)["attempts"][0]["seconds"], 119)
        pm.requeue_stale(stale_after_seconds=0, note="requeued at startup: holder process is gone")
        self.assertEqual(("needs_review", "orphaned"), (self._row(2)["status"], self._row(2)["attempts"][0]["cause"]))

    def test_an_orphaned_run_is_charged_up_to_its_last_heartbeat_not_the_downtime(self):
        # 2026-09-23: the server was down four hours; charging that as worker
        # time escalated two tasks as "inefficient".
        self._write({"id": 1, "status": "in_progress", "worker": "w", "started_at": _stamp(4 * 3600 + 300),
                     "heartbeat_at": _stamp(4 * 3600)})
        pm.requeue_stale(stale_after_seconds=0, note="requeued at startup: holder process is gone")
        self.assertAlmostEqual(300, self._row(1)["attempts"][0]["seconds"], delta=5)
        self.assertIsNone(loopguard.verdict(self._row(1)))

    def test_operator_retry_starts_the_guard_over(self):
        guard = {"stage": "escalated", "cause": "stray_edits", "at": _stamp(100)}
        self._write({"id": 1, "status": "blocked", "phase": "needs_claude", "retry_count": 3, "note": STRAY,
                     "loop_guard": guard, "attempts": _attempts(STRAY, 5)})
        pm.operator_action(1, "retry", "infra fixed")
        row = self._row(1)
        self.assertEqual(("ready", 0, None), (row["status"], row["retry_count"], row["loop_guard"]))
        self.assertEqual(5, len(row["attempts"]))                  # history kept
        self.assertIsNone(loopguard.verdict(row))                   # but no longer counted
        triage.run_triage(force=True)
        self.assertEqual("ready", self._row(1)["status"])

    def test_free_requeue_loop_ends_in_a_handoff(self):
        # Transient failures are requeued without retry cost, which used to let
        # one task run forever. The guard bounds it: remedy once, then hand off.
        self._write({"id": 1, "title": "readback", "status": "ready", "priority": 70, "prompt": "do it", "retry_count": 0})
        runs = 0
        while self._row()["status"] != "blocked" or self._row().get("phase") != "needs_claude":
            self._run_and_fail("lane paused until X: claude-local wall clock exceeded, model congested")
            runs += 1
            triage.run_triage(force=True)
            self.assertLess(runs, 20, "the loop guard never stopped the task")
        row = self._row()
        self.assertEqual(loopguard.REPEAT_LIMIT + 2, runs)  # 3 to detect, 2 after the remedy
        self.assertEqual("escalated", row["loop_guard"]["stage"])
        self.assertEqual(1, len([i for i in row["operator_instructions"] if i.get("source") == "loopguard"]))
        handoff = self.handoff.read_text(encoding="utf-8")
        self.assertIn("#1 readback", handoff)
        self.assertIn("Claude Code 조치 필요", handoff)

    def test_remedy_requeues_with_instruction_and_fresh_budget(self):
        self._write({"id": 1, "status": "blocked", "retry_count": 2, "note": STRAY, "attempts": _attempts(STRAY, 3)},
                    {"id": 2, "status": "ready", "attempts": []})
        result = triage.run_triage(force=True)
        row = self._row()
        self.assertEqual(("ready", "loop_remediated", 0), (row["status"], row["phase"], row["retry_count"]))
        self.assertIn("[루프 감지]", row["operator_instructions"][-1]["text"])
        self.assertEqual([{"task": 1, "cause": "loop:stray_edits", "action": "remediated"}], result["actions"])

    def test_requeue_blocked_leaves_a_looping_task_to_triage(self):
        self._write({"id": 1, "status": "blocked", "retry_count": 0, "note": STRAY, "attempts": _attempts(STRAY, 3)},
                    {"id": 2, "status": "blocked", "retry_count": 0, "note": STRAY, "attempts": _attempts(STRAY, 1)})
        self.assertEqual([2], [t["id"] for t in pm.requeue_blocked(limit=5)])

    def test_codex_handoff_carries_the_loop_diagnosis(self):
        task = {"id": 7, "title": "t", "prompt": "Exit criteria: works. Evidence/files to inspect: a/b.py.",
                "note": "loop guard: x", "loop_guard": {"reason": "the same failure 3 times in a row", "attempts": "3 attempts"}}
        self.assertIn("Loop guard: the same failure 3 times in a row", escalation.dev_task_payload(task)["goal"])


if __name__ == "__main__":
    unittest.main()
