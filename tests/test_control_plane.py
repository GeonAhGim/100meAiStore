import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from smart_store_control import state


class ControlPlaneTests(unittest.TestCase):
    def test_default_snapshot_keeps_smart_store_paused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            milestones = root / "milestones.json"
            runtime = root / "runtime.json"
            milestones.write_text(json.dumps({"milestones": []}), encoding="utf-8")
            runtime.write_text(
                json.dumps(
                    {
                        "aios_priority": True,
                        "handoff_granted": False,
                        "local_llm": {"enabled": False},
                        "slots": {"aios_reserved": 6, "smart_store_reserved": 1, "active": 0},
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(state, "MILESTONES_PATH", milestones), patch.object(
                state, "RUNTIME_PATH", runtime
            ), patch.object(state, "probe_aios", lambda: {"available_slots": 5}):
                view = state.snapshot()
            self.assertTrue(view["aios"]["priority"])
            self.assertFalse(view["runtime"]["handoff_granted"])
            self.assertEqual(0, view["runtime"]["slots"]["active"])

    def test_handoff_is_explicit_and_reversible(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder) / "runtime.json"
            runtime.write_text(
                json.dumps(
                    {
                        "aios_priority": True,
                        "handoff_granted": False,
                        "local_llm": {"enabled": False},
                        "slots": {"aios_reserved": 6, "smart_store_reserved": 1, "active": 0},
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(state, "RUNTIME_PATH", runtime), patch.object(
                state, "probe_aios", lambda: {"available_slots": 5}
            ):
                granted = state.grant_handoff()
                self.assertTrue(granted["handoff_granted"])
                self.assertTrue(granted["local_llm"]["enabled"])
                self.assertEqual(1, granted["slots"]["active"])
                revoked = state.revoke_handoff()
            self.assertFalse(revoked["handoff_granted"])
            self.assertFalse(revoked["local_llm"]["enabled"])
            self.assertEqual(0, revoked["slots"]["active"])


if __name__ == "__main__":
    unittest.main()
