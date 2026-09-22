"""Whole-file exchange: plan parsing, file blocks, git-built patch, rewrite guard."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from smart_store_control.context import check_patch
from smart_store_control.filepatch import build_patch, parse_files, parse_plan


class FilePatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        for args in (["init", "-q"], ["config", "user.email", "t@example.test"], ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "mod.py").write_text("A = 1\nB = 2\n", encoding="utf-8")
        big = "\n".join(f"line{i} = {i}" for i in range(60)) + "\n"
        (self.root / "pkg" / "big.py").write_text(big, encoding="utf-8")
        (self.root / "tests").mkdir()
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-q", "-m", "base"], check=True, capture_output=True)

    def tearDown(self):
        subprocess.run(["git", "-C", str(self.root), "worktree", "prune"], capture_output=True)
        self.tmp.cleanup()

    def test_plan_keeps_only_existing_paths(self):
        existing = {"pkg/mod.py", "pkg/big.py"}
        self.assertEqual(["pkg/mod.py"], parse_plan('Sure: ["pkg/mod.py", "smart_store/worker.py", "pkg/mod.py"]', existing))
        self.assertEqual([], parse_plan("no json here", existing))

    def test_file_blocks_are_parsed(self):
        text = "<<<FILE pkg/mod.py>>>\nA = 1\nB = 3\n<<<END>>>\n<<<FILE tests/test_new.py>>>\nimport unittest\n<<<END>>>"
        files = parse_files(text)
        self.assertEqual({"pkg/mod.py": "A = 1\nB = 3\n", "tests/test_new.py": "import unittest\n"}, files)
        self.assertEqual({}, parse_files("<<<FILE ../x>>>\nbad\n<<<END>>>"))

    def test_git_builds_an_applicable_patch_and_rejects_out_of_plan_or_rewrites(self):
        out = self.root / "data" / "control" / "artifacts" / "task-1.patch"
        files = {"pkg/mod.py": "A = 1\nB = 3\n", "tests/test_mod.py": "def test_b():\n    assert True\n"}
        self.assertIsNone(build_patch(self.root, files, ["pkg/mod.py"], out))
        self.assertIsNone(check_patch(self.root, out))
        self.assertIn("+B = 3", out.read_text(encoding="utf-8"))
        self.assertIn("tests/test_mod.py", out.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "data" / "control" / "scratch" / "task-1").exists())  # scratch cleaned

        self.assertIn("outside the plan", build_patch(self.root, {"pkg/big.py": "x = 1\n"}, ["pkg/mod.py"], out))
        self.assertIn("wholesale rewrite", build_patch(self.root, {"pkg/big.py": "x = 1\n"}, ["pkg/big.py"], out))
        self.assertIn("unchanged", build_patch(self.root, {"pkg/mod.py": "A = 1\nB = 2\n"}, ["pkg/mod.py"], out))
        self.assertIn("no <<<FILE", build_patch(self.root, {}, ["pkg/mod.py"], out))


if __name__ == "__main__":
    unittest.main()
