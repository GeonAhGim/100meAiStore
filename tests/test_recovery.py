import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from smart_store_control import pm, recovery


class RecoveryTests(unittest.TestCase):
    def test_stale_task_is_requeued(self):
        with tempfile.TemporaryDirectory() as folder:
            tasks = Path(folder) / "tasks.json"
            tasks.write_text('{"tasks":[{"id":1,"status":"in_progress","started_at":"2020-01-01T00:00:00Z"}]}', encoding="utf-8")
            with patch.object(recovery, "TASKS_PATH", tasks), patch.object(pm, "TASKS_PATH", tasks):
                self.assertEqual([1], recovery._requeue_stale())
            self.assertIn('"ready"', tasks.read_text(encoding="utf-8"))

    def test_start_is_nonblocking_and_singleton(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder) / "pm_run.json"
            with patch.object(recovery, "RUN_PATH", run), patch.object(recovery, "_LOCK", __import__("threading").Lock()), patch.object(recovery.threading, "Thread") as thread:
                result = recovery.start()
                self.assertEqual("diagnosing", result["status"])
                thread.assert_called_once()
                recovery._LOCK.release()


if __name__ == "__main__":
    unittest.main()
