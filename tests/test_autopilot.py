import unittest
from unittest.mock import patch

from smart_store_control import autopilot


class AutopilotTests(unittest.TestCase):
    def test_waits_while_worker_is_active(self):
        with patch.object(autopilot, "status", lambda: {"enabled": True, "local_first": True}), patch.object(
            autopilot, "pm_status", lambda: {"tasks": [{"id": 1, "status": "in_progress"}], "attention": 0}
        ), patch.object(autopilot, "recovery_status", lambda: {"status": "idle"}), patch.object(
            autopilot, "integrate_landed", lambda: []
        ):
            self.assertEqual("waiting", autopilot.tick()["action"])

    def test_local_first_starts_recovery_for_ready_work(self):
        with patch.object(autopilot, "status", lambda: {"enabled": True, "local_first": True, "cooldown_seconds": 0}), patch.object(
            autopilot, "pm_status", lambda: {"tasks": [{"id": 1, "status": "ready"}], "attention": 0}
        ), patch.object(autopilot, "recovery_status", lambda: {"status": "idle"}), patch.object(
            autopilot, "start_recovery", lambda: {"status": "diagnosing"}
        ), patch.object(autopilot, "write_json"), patch.object(autopilot, "integrate_landed", lambda: []):
            self.assertEqual("recovery", autopilot.tick()["action"])


if __name__ == "__main__":
    unittest.main()
