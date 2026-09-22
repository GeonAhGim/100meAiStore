"""Implementer lanes: local (shared llama slots) plus cursor and gemini spare capacity."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from smart_store_control import agent_engine, pm


class LaneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        self.tasks = root / "tasks.json"
        self.pools = root / "pools.json"
        self.runtime = root / "runtime.json"
        self.pools.write_text(json.dumps({"pools": {
            "local-impl": {"size": 2, "engine": "local-http"},
            "cursor-impl": {"size": 1, "engine": "cursor"},
            "gemini-impl": {"size": 1, "engine": "gemini"},
        }}), encoding="utf-8")
        self.runtime.write_text(json.dumps({"handoff_granted": True, "slots": {"operator_floor": 0}}), encoding="utf-8")
        self.tasks.write_text(json.dumps({"tasks": [
            {"id": 1, "role": "local-impl", "status": "ready", "priority": 90},
            {"id": 2, "role": "local-impl", "status": "ready", "priority": 80},
            {"id": 3, "role": "local-impl", "status": "in_progress", "worker": "cursor-impl-1"},
        ]}), encoding="utf-8")
        self.patches = [
            mock.patch.object(pm, "TASKS_PATH", self.tasks), mock.patch.object(pm, "POOLS_PATH", self.pools),
            mock.patch.object(pm, "CONTROL_DIR", root),
            mock.patch.object(pm, "snapshot", lambda: {"aios_live": {"available_slots": 0}}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_lane_names_and_capacity(self):
        self.assertEqual("cursor-impl", pm.lane_of("cursor-impl-1"))
        self.assertEqual("local-impl", pm.lane_of("local-impl-12"))
        self.assertEqual(["local-impl", "cursor-impl", "gemini-impl"], pm.implementer_lanes())
        self.assertEqual(0, pm.effective_capacity("local-impl")["effective"])   # AIOS holds every slot
        self.assertEqual(1, pm.effective_capacity("cursor-impl")["effective"])  # not gated by AIOS
        self.assertEqual(1, pm.effective_capacity("gemini-impl")["effective"])

    def test_claims_are_bounded_per_lane_and_share_the_task_pool(self):
        self.assertIsNone(pm.claim("local-impl-1", "local-impl"))              # no local slot
        self.assertIsNone(pm.claim("cursor-impl-2", "cursor-impl"))            # cursor lane already holds task 3
        claimed = pm.claim("gemini-impl-1", "gemini-impl")
        self.assertEqual(1, claimed["id"])                                     # highest priority ready task
        self.assertEqual("gemini-impl-1", claimed["worker"])
        self.assertIsNone(pm.claim("gemini-impl-1", "gemini-impl"))            # gemini lane now full

    def test_external_engine_commands_read_the_prompt_from_stdin(self):
        with mock.patch.object(agent_engine.shutil, "which", lambda name: f"/bin/{name}"):
            argv, env, kind = agent_engine.engine_command("gemini", "", 45)
            self.assertEqual("/bin/gemini", argv[0])
            self.assertIn("--approval-mode", argv)
            self.assertEqual("text", kind)
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            argv, _, _ = agent_engine.engine_command("cursor", "sonnet-4", 45)
            self.assertEqual(["-p", "--force", "--output-format", "text", "--model", "sonnet-4"], argv[1:])
        with self.assertRaises(RuntimeError):
            agent_engine.engine_command("copilot", "", 1)

    def test_run_agent_uses_the_external_engine_and_captures_its_text(self):
        repo = Path(self.tmp.name) / "repo"
        repo.mkdir()
        for args in (["init", "-q"], ["config", "user.email", "t@example.test"], ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        (repo / "a.py").write_text("A = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "base"], check=True, capture_output=True)
        seen = {}

        def fake_spawn(argv, cwd, input, **kwargs):
            seen["argv"] = argv
            (Path(cwd) / "a.py").write_text("A = 2\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "edited a.py\n", "")
        with mock.patch.object(agent_engine.shutil, "which", lambda name: f"/bin/{name}"):
            diff, summary = agent_engine.run_agent(repo, {"id": 9, "title": "t", "prompt": "p"}, model="", base_url="",
                                                   spawn=fake_spawn, engine="gemini")
        self.assertEqual("/bin/gemini", seen["argv"][0])
        self.assertIn("+A = 2", diff)
        self.assertEqual("gemini", summary["engine"])
        self.assertEqual("edited a.py\n", summary["result"])
        subprocess.run(["git", "-C", str(repo), "worktree", "prune"], capture_output=True)


if __name__ == "__main__":
    unittest.main()
