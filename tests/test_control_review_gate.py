"""Review = objective gate + advisory model read; reviewed patches land on a branch."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from smart_store_control import land, pm, review


GOOD_PATCH = (
    "--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -1,1 +1,2 @@\n A = 1\n+B = 2\n"
    "--- /dev/null\n+++ b/tests/test_mod.py\n@@ -0,0 +1,5 @@\n+import unittest\n+from pkg.mod import B\n+class T(unittest.TestCase):\n+    def test_b(self):\n+        self.assertEqual(2, B)\n"
)
BAD_PATCH = GOOD_PATCH.replace("self.assertEqual(2, B)", "self.assertEqual(3, B)")


class ReviewGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        for args in (["init", "-q"], ["config", "user.email", "t@example.test"], ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (self.root / "pkg" / "mod.py").write_text("A = 1\n", encoding="utf-8")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (self.root / ".gitignore").write_text("data/\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-q", "-m", "base"], check=True, capture_output=True)
        self.artifacts = self.root / "data" / "control" / "artifacts"
        self.artifacts.mkdir(parents=True)

    def tearDown(self):
        subprocess.run(["git", "-C", str(self.root), "worktree", "prune"], capture_output=True)
        self.tmp.cleanup()

    def _patch(self, name, text):
        path = self.artifacts / name
        path.write_bytes(text.encode("utf-8"))
        return path

    def test_gate_runs_touched_tests_and_full_suite_in_a_scratch_worktree(self):
        passed, report = review.run_gate(self.root, self._patch("good.patch", GOOD_PATCH), 1)
        self.assertTrue(passed, report)
        self.assertIn("touched tests (1)", report)
        self.assertIn("full suite", report)
        self.assertFalse((self.root / "data" / "control" / "scratch" / "review-1").exists())
        self.assertEqual("A = 1\n", (self.root / "pkg" / "mod.py").read_text(encoding="utf-8"))  # HEAD untouched

    def test_gate_failure_carries_the_test_output(self):
        passed, report = review.run_gate(self.root, self._patch("bad.patch", BAD_PATCH), 2)
        self.assertFalse(passed)
        self.assertIn("3 != 2", report)

    def test_review_prompt_shows_current_files_and_forbids_missing_claims(self):
        prompt = review.review_prompt(GOOD_PATCH, "full suite: rc=0", self.root)
        self.assertIn("<<<CURRENT pkg/mod.py>>>", prompt)
        self.assertIn("A = 1", prompt)
        self.assertIn("do not claim a function", prompt)
        self.assertIn("GATE RESULTS", prompt)

    def test_decisions_map_to_reviewed_needs_decision_or_blocked(self):
        tasks = self.root / "data" / "control" / "tasks.json"
        tasks.write_text(json.dumps({"tasks": [{"id": 5, "status": "reviewing", "reviewer": "r"}]}), encoding="utf-8")
        with mock.patch.object(pm, "TASKS_PATH", tasks):
            self.assertEqual("reviewed", pm.finish_review(5, "pass")["status"])
            self.assertEqual("needs_decision", pm.finish_review(5, "needs_decision")["status"])
            self.assertEqual("blocked", pm.finish_review(5, "fail")["status"])

    def test_stale_reviewed_patch_goes_back_to_ready_and_leaves_no_branch(self):
        patch = self._patch("task-8.patch", GOOD_PATCH)
        (self.root / "pkg" / "mod.py").write_text("A = 100\n", encoding="utf-8")   # HEAD moved under the patch
        subprocess.run(["git", "-C", str(self.root), "commit", "-q", "-am", "moved"], check=True, capture_output=True)
        tasks = self.root / "data" / "control" / "tasks.json"
        tasks.write_text(json.dumps({"tasks": [{"id": 8, "status": "reviewed", "title": "stale", "artifact": str(patch)}]}), encoding="utf-8")
        with mock.patch.object(pm, "TASKS_PATH", tasks):
            changed = land.land_reviewed(self.root)
        self.assertEqual("ready", changed[0]["status"])
        self.assertEqual("rebase_needed", changed[0]["phase"])
        branches = subprocess.run(["git", "-C", str(self.root), "branch", "--list", "control/*"], capture_output=True, text=True).stdout
        self.assertEqual("", branches.strip())

    def test_reviewed_patch_lands_on_a_task_branch_and_marks_done(self):
        patch = self._patch("task-9.patch", GOOD_PATCH)
        tasks = self.root / "data" / "control" / "tasks.json"
        tasks.write_text(json.dumps({"tasks": [{"id": 9, "status": "reviewed", "title": "demo", "artifact": str(patch)}]}), encoding="utf-8")
        with mock.patch.object(pm, "TASKS_PATH", tasks):
            changed = land.land_reviewed(self.root)
        self.assertEqual("done", changed[0]["status"])
        self.assertEqual("control/task-9", changed[0]["branch"])
        show = subprocess.run(["git", "-C", str(self.root), "show", "--stat", "control/task-9"], capture_output=True, text=True).stdout
        self.assertIn("tests/test_mod.py", show)
        self.assertIn("control(task-9): demo", show)
        head = subprocess.run(["git", "-C", str(self.root), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.assertIn(head, ("master", "main"))                   # the main branch never moved
        self.assertEqual("A = 1\n", (self.root / "pkg" / "mod.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
