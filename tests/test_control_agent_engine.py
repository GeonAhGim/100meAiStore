"""claude-local engine: isolated worktree, stripped environment, diff capture, cleanup."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from smart_store_control import agent_engine
from smart_store_control.context import check_patch, rewrite_violation


class AgentEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        for args in (["init", "-q"], ["config", "user.email", "t@example.test"], ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "mod.py").write_text("A = 1\n", encoding="utf-8")
        (self.root / ".gitignore").write_text("data/\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-q", "-m", "base"], check=True, capture_output=True)
        self.task = {"id": 7, "title": "t", "milestone": "M1", "prompt": "Add B = 2 to pkg/mod.py and a test."}

    def tearDown(self):
        subprocess.run(["git", "-C", str(self.root), "worktree", "prune"], capture_output=True)
        self.tmp.cleanup()

    def test_environment_is_allow_listed_and_points_at_the_proxy(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "real-secret", "CLAUDE_CODE_X": "1", "PATH": os.environ.get("PATH", "")}):
            env = agent_engine.local_env("qwen", "http://127.0.0.1:8081")
        self.assertEqual("http://127.0.0.1:8081", env["ANTHROPIC_BASE_URL"])
        self.assertEqual("local", env["ANTHROPIC_API_KEY"])          # inherited key never reaches the CLI
        self.assertNotIn("CLAUDE_CODE_X", env)
        self.assertEqual("qwen", env["ANTHROPIC_MODEL"])
        argv = agent_engine.build_argv("claude", "qwen", 45)
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertIn(str(agent_engine.SETTINGS_PATH), argv)
        self.assertIn("로컬 DEMO", agent_engine.task_prompt(self.task))
        self.assertIn('"id": 7', agent_engine.task_prompt(self.task))

    def test_agent_edits_in_a_worktree_and_only_the_diff_survives(self):
        calls = {}

        def fake_spawn(argv, cwd, input, **kwargs):
            calls["cwd"], calls["argv"], calls["env"] = Path(cwd), argv, kwargs["env"]
            (Path(cwd) / "pkg" / "mod.py").write_text("A = 1\nB = 2\n", encoding="utf-8")
            (Path(cwd) / "tests").mkdir(exist_ok=True)
            (Path(cwd) / "tests" / "test_mod.py").write_text("def test_b():\n    assert True\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, json.dumps({"result": "done", "num_turns": 3, "is_error": False}), "")
        with mock.patch.object(agent_engine, "claude_executable", lambda: "claude"):
            diff, summary = agent_engine.run_agent(self.root, self.task, model="qwen", base_url="http://127.0.0.1:8081",
                                                   spawn=fake_spawn, artifact_dir=self.root / "data" / "control" / "artifacts")
        self.assertIn("+B = 2", diff)
        self.assertIn("tests/test_mod.py", diff)
        self.assertEqual(3, summary["num_turns"])
        self.assertTrue(str(calls["cwd"]).endswith("task-7"))
        self.assertFalse(calls["cwd"].exists())                      # worktree removed afterwards
        self.assertEqual("A = 1\n", (self.root / "pkg" / "mod.py").read_text(encoding="utf-8"))  # main checkout untouched
        patch = self.root / "data" / "control" / "artifacts" / "task-7.patch"
        patch.parent.mkdir(parents=True, exist_ok=True)
        patch.write_text(diff, encoding="utf-8")
        self.assertIsNone(check_patch(self.root, patch))
        self.assertIsNone(rewrite_violation(self.root, patch))
        self.assertTrue((self.root / "data" / "control" / "artifacts" / "task-7.agent.json").exists())

    def test_no_change_yields_empty_diff_and_cleanup_still_happens(self):
        def idle_spawn(argv, cwd, input, **kwargs):
            return subprocess.CompletedProcess(argv, 0, json.dumps({"result": "BLOCKED: nothing to do", "num_turns": 1}), "")
        with mock.patch.object(agent_engine, "claude_executable", lambda: "claude"):
            diff, summary = agent_engine.run_agent(self.root, self.task, model="qwen", base_url="http://127.0.0.1:8081", spawn=idle_spawn)
        self.assertEqual("", diff)
        self.assertIn("BLOCKED", summary["result"])
        self.assertFalse((self.root / "data" / "control" / "worktrees" / "task-7").exists())


if __name__ == "__main__":
    unittest.main()
