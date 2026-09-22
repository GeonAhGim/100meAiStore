"""Local-LLM implementation worker: repository context, apply-check, retry feedback."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from smart_store_control.context import check_patch, named_files, repository_context


class ControlWorkerContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        for args in (["init", "-q"], ["config", "user.email", "t@example.test"], ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)
        (self.root / "smart_store_control").mkdir()
        (self.root / "smart_store_control" / "pm.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "__init__.py").write_text("", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-q", "-m", "base"], check=True, capture_output=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_named_files_are_parsed_from_the_task_prompt(self):
        prompt = "Implement X. Evidence/files to inspect: smart_store_control/pm.py; smart_store_control/worker.py. Work offline."
        self.assertEqual(["smart_store_control/pm.py", "smart_store_control/worker.py"], named_files(prompt))
        self.assertEqual([], named_files("no file hint"))

    def test_context_lists_real_paths_and_inlines_named_files_with_feedback(self):
        prompt = "Do it. Evidence/files to inspect: smart_store_control/pm.py; smart_store/worker.py."
        text = repository_context(self.root, prompt, feedback="error: smart_store/worker.py: No such file")
        self.assertIn("smart_store_control/pm.py", text)
        self.assertIn("VALUE = 1", text)                       # named existing file inlined
        self.assertIn("FILE smart_store/worker.py: does not exist", text)
        self.assertIn("PREVIOUS ATTEMPT FAILED", text)
        self.assertIn("No such file", text)
        self.assertIn("git apply --check", text)

    def test_check_patch_ignores_crlf_working_tree_files(self):
        # Simulate the Windows checkout: the working file has CRLF while the
        # index (and any git-produced diff) has LF.
        target = self.root / "smart_store_control" / "pm.py"
        target.write_bytes(b"VALUE = 1\r\n")
        good = self.root / "good.patch"
        good.write_bytes(("--- a/smart_store_control/pm.py\n+++ b/smart_store_control/pm.py\n@@ -1,1 +1,2 @@\n VALUE = 1\n+OTHER = 2\n").encode("utf-8"))
        self.assertIsNone(check_patch(self.root, good))

    def test_check_patch_rejects_invented_paths_and_accepts_real_ones(self):
        bad = self.root / "bad.patch"
        bad.write_bytes(("--- a/smart_store/worker.py\n+++ b/smart_store/worker.py\n@@ -1,1 +1,2 @@\n import x\n+import y\n").encode("utf-8"))
        error = check_patch(self.root, bad)
        self.assertIsNotNone(error)
        self.assertIn("smart_store/worker.py", error)
        good = self.root / "good.patch"
        good.write_bytes(("--- a/smart_store_control/pm.py\n+++ b/smart_store_control/pm.py\n@@ -1,1 +1,2 @@\n VALUE = 1\n+OTHER = 2\n").encode("utf-8"))
        self.assertIsNone(check_patch(self.root, good))
        empty = self.root / "empty.patch"
        empty.write_bytes(("\n").encode("utf-8"))
        self.assertEqual("patch is empty", check_patch(self.root, empty))


if __name__ == "__main__":
    unittest.main()
