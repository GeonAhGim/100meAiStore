"""Landed task branches reach main only through a green full suite, and complete their milestone."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from smart_store_control import integrate, pm

PASSING = "import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n"
FAILING = "import unittest\n\nclass U(unittest.TestCase):\n    def test_bad(self):\n        self.fail('red')\n"


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


class IntegrateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name) / "repo"
        (self.root / "tests").mkdir(parents=True)
        _git(self.root, "init", "-q", "-b", "main")
        _git(self.root, "config", "user.email", "t@example.test")
        _git(self.root, "config", "user.name", "t")
        (self.root / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (self.root / "tests" / "test_base.py").write_text(PASSING, encoding="utf-8")
        (self.root / "mod.py").write_text("A = 1\n", encoding="utf-8")
        (self.root / ".gitignore").write_text("data/\n", encoding="utf-8")
        _git(self.root, "add", "-A"); _git(self.root, "commit", "-q", "-m", "base")
        control = Path(self.tmp.name) / "control"
        control.mkdir()
        self.tasks = control / "tasks.json"
        self.milestones = control / "milestones.json"
        self.milestones.write_text(json.dumps({"milestones": [{"id": "M9", "status": "ready", "evidence": "mod.py"}]}), encoding="utf-8")
        self.patches = [mock.patch.object(pm, "TASKS_PATH", self.tasks),
                        mock.patch.object(integrate, "MILESTONES_PATH", self.milestones)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        subprocess.run(["git", "-C", str(self.root), "worktree", "prune"], capture_output=True)
        self.tmp.cleanup()

    def _branch(self, task_id, path, text):
        _git(self.root, "checkout", "-q", "-b", f"control/task-{task_id}")
        (self.root / path).write_text(text, encoding="utf-8")
        _git(self.root, "add", "-A"); _git(self.root, "commit", "-q", "-m", f"task {task_id}")
        _git(self.root, "checkout", "-q", "main")

    def _ledger(self, *tasks):
        self.tasks.write_text(json.dumps({"tasks": [
            {"id": i, "status": "done", "phase": "landed", "branch": f"control/task-{i}", "title": f"t{i}",
             "milestone": "M9", "source": "milestone-workflow"} for i in tasks]}), encoding="utf-8")

    def _row(self, task_id):
        return next(t for t in json.loads(self.tasks.read_text(encoding="utf-8"))["tasks"] if t["id"] == task_id)

    def test_green_branch_is_merged_into_main_and_completes_its_milestone(self):
        self._branch(1, "mod.py", "A = 2\n")
        self._ledger(1)
        [row] = integrate.integrate_landed(self.root)
        self.assertEqual("merged", row["phase"])
        self.assertEqual(_git(self.root, "rev-parse", "main"), row["merged_commit"])
        self.assertEqual("A = 2\n", (self.root / "mod.py").read_text(encoding="utf-8"))   # live checkout fast-forwarded
        self.assertEqual("", _git(self.root, "status", "--porcelain"))
        milestone = json.loads(self.milestones.read_text(encoding="utf-8"))["milestones"][0]
        self.assertEqual("done", milestone["status"])
        self.assertIn("task 1 merged", milestone["evidence"])
        self.assertEqual([], integrate.integrate_landed(self.root))                         # nothing left to do
        self.assertNotIn("integrate-", _git(self.root, "worktree", "list", "--porcelain"))  # scratch removed

    def test_a_merge_is_progress_until_the_milestone_done_check_passes(self):
        # M4.7, 2026-09-25: half the audit gaps fixed, merged, and marked done.
        check = ["python", "-c", "import sys; t=open('mod.py').read().strip(); print('left:', t); sys.exit(0 if t == 'A = 3' else 1)"]
        self.milestones.write_text(json.dumps({"milestones": [{"id": "M9", "status": "ready", "done_check": check}]}),
                                   encoding="utf-8")
        self._branch(1, "mod.py", "A = 2\n")
        self._ledger(1)
        [row] = integrate.integrate_landed(self.root)
        self.assertEqual("A = 2\n", (self.root / "mod.py").read_text(encoding="utf-8"))  # the progress is kept
        self.assertEqual(("ready", "done_check_failed"), (row["status"], row["phase"]))
        self.assertIn("left: A = 2", row["last_error"])
        self.assertEqual("done_check", row["attempts"][-1]["cause"])
        self.assertEqual("ready", json.loads(self.milestones.read_text(encoding="utf-8"))["milestones"][0]["status"])
        # the next run finishes the job: now the milestone completes
        _git(self.root, "branch", "-D", "control/task-1")
        self._branch(1, "mod.py", "A = 3\n")
        data = json.loads(self.tasks.read_text(encoding="utf-8"))
        data["tasks"][0].update({"status": "done", "phase": "landed"})
        self.tasks.write_text(json.dumps(data), encoding="utf-8")
        [row] = integrate.integrate_landed(self.root)
        self.assertEqual("merged", row["phase"])
        self.assertEqual("done", json.loads(self.milestones.read_text(encoding="utf-8"))["milestones"][0]["status"])

    def test_red_merge_leaves_main_alone_and_requeues_with_the_failure(self):
        self._branch(2, "tests/test_new.py", FAILING)
        self._ledger(2)
        before = _git(self.root, "rev-parse", "main")
        [row] = integrate.integrate_landed(self.root)
        self.assertEqual(before, _git(self.root, "rev-parse", "main"))
        self.assertEqual(("ready", "rebase_needed"), (row["status"], row["phase"]))
        self.assertIn("test_bad", row["last_error"])
        self.assertEqual("gate_tests", row["attempts"][-1]["cause"])
        self.assertEqual("ready", json.loads(self.milestones.read_text(encoding="utf-8"))["milestones"][0]["status"])

    def test_conflict_requeues_as_base_moved(self):
        self._branch(3, "mod.py", "A = 3\n")
        (self.root / "mod.py").write_text("A = 4\n", encoding="utf-8")
        _git(self.root, "commit", "-q", "-am", "main moved")
        self._ledger(3)
        [row] = integrate.integrate_landed(self.root)
        self.assertEqual("rebase_needed", row["phase"])
        self.assertIn("mod.py", row["note"])
        self.assertEqual("patch_apply", row["attempts"][-1]["cause"])
        self.assertEqual("A = 4\n", (self.root / "mod.py").read_text(encoding="utf-8"))

    def test_refused_fast_forward_backs_off_instead_of_rerunning_the_suite(self):
        self._branch(4, "mod.py", "A = 5\n")
        self._ledger(4)
        (self.root / "mod.py").write_text("operator edit\n", encoding="utf-8")         # uncommitted work in the checkout
        [row] = integrate.integrate_landed(self.root)
        self.assertEqual(("done", "landed"), (row["status"], row["phase"]))
        self.assertIn("refused", row["integration_note"])
        self.assertEqual("operator edit\n", (self.root / "mod.py").read_text(encoding="utf-8"))
        self.assertEqual([], integrate.pending(json.loads(self.tasks.read_text(encoding="utf-8"))["tasks"]))


if __name__ == "__main__":
    unittest.main()
