import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from smart_store_control import pm_cycle


class PMCycleTests(unittest.TestCase):
    def test_cycle_is_blocked_without_effective_slot(self):
        with tempfile.TemporaryDirectory() as folder:
            run_path = Path(folder) / "pm_run.json"
            with patch.object(pm_cycle, "RUN_PATH", run_path), patch.object(
                pm_cycle, "effective_capacity", lambda role: {"effective": 0}
            ):
                result = pm_cycle.start()
            self.assertEqual("blocked", result["status"])

    def test_cycle_does_not_overlap_running_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            run_path = Path(folder) / "pm_run.json"
            with patch.object(pm_cycle, "RUN_PATH", run_path), patch.object(
                pm_cycle, "effective_capacity", lambda role: {"effective": 1}
            ), patch.object(
                pm_cycle, "pm_status", lambda: {"tasks": [{"status": "in_progress"}]}
            ):
                result = pm_cycle.start()
            self.assertEqual("blocked", result["status"])


if __name__ == "__main__":
    unittest.main()
