import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from smart_store_control import pm


class PMPoolTests(unittest.TestCase):
    @staticmethod
    def _read(path, default):
        if path == pm.CONTROL_DIR / "runtime.json":
            return {"handoff_granted": True}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def test_aios_priority_caps_effective_capacity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tasks = root / "tasks.json"
            pools = root / "pools.json"
            tasks.write_text(json.dumps({"version": 1, "next_id": 2, "tasks": [{"id": 1, "role": "local-impl", "status": "ready", "priority": 1}]}), encoding="utf-8")
            pools.write_text(json.dumps({"pools": {"local-impl": {"size": 4, "max_size": 4}}}), encoding="utf-8")
            with patch.object(pm, "TASKS_PATH", tasks), patch.object(pm, "POOLS_PATH", pools), patch.object(
                pm, "snapshot", lambda: {"aios_live": {"available_slots": 2}}
            ), patch.object(pm, "read_json", self._read):
                self.assertEqual(2, pm.effective_capacity()["effective"])

    def test_claim_is_priority_ordered_and_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tasks = root / "tasks.json"
            pools = root / "pools.json"
            tasks.write_text(json.dumps({"version": 1, "next_id": 3, "tasks": [{"id": 1, "role": "local-impl", "status": "ready", "priority": 10}, {"id": 2, "role": "local-impl", "status": "ready", "priority": 20}]}), encoding="utf-8")
            pools.write_text(json.dumps({"pools": {"local-impl": {"size": 1, "max_size": 1}}}), encoding="utf-8")
            with patch.object(pm, "TASKS_PATH", tasks), patch.object(pm, "POOLS_PATH", pools), patch.object(
                pm, "snapshot", lambda: {"aios_live": {"available_slots": 1}}
            ), patch.object(pm, "read_json", self._read):
                task = pm.claim("local-impl-1")
                self.assertEqual(2, task["id"])
                self.assertIsNone(pm.claim("local-impl-2"))


if __name__ == "__main__":
    unittest.main()
