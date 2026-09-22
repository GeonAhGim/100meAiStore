"""M0.5: milestone progress with status, priority, dependencies, exit criteria."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from smart_store_control import server
from smart_store_control import state as state_module


class MilestoneProgressTests(unittest.TestCase):
    """Test that milestone_progress returns enriched data with priority, deps, exit_criteria."""

    def _view(self, milestones):
        return {"milestones": {"milestones": milestones}}

    def test_empty_milestones(self):
        result = server.milestone_progress(self._view([]))
        self.assertEqual(0, result["total"])
        self.assertEqual(0, result["done"])
        self.assertIsNone(result["percent"])
        self.assertEqual([], result["items"])

    def test_done_milestones_count(self):
        items = [
            {"id": "M0.1", "title": "bootstrap", "status": "done", "priority": 100, "depends_on": []},
            {"id": "M0.2", "title": "worker pool", "status": "done", "priority": 90, "depends_on": ["M0.1"]},
            {"id": "M0.3", "title": "review gate", "status": "ready", "priority": 80, "depends_on": ["M0.2"]},
        ]
        result = server.milestone_progress(self._view(items))
        self.assertEqual(3, result["total"])
        self.assertEqual(2, result["done"])
        self.assertEqual(67, result["percent"])

    def test_items_sorted_by_priority_desc(self):
        items = [
            {"id": "M1", "title": "low", "status": "planned", "priority": 10, "depends_on": []},
            {"id": "M2", "title": "high", "status": "planned", "priority": 90, "depends_on": []},
            {"id": "M3", "title": "mid", "status": "planned", "priority": 50, "depends_on": []},
        ]
        result = server.milestone_progress(self._view(items))
        ids = [m["id"] for m in result["items"]]
        self.assertEqual(["M2", "M3", "M1"], ids)

    def test_dependency_satisfaction_labels(self):
        items = [
            {"id": "M0.1", "title": "base", "status": "done", "priority": 100, "depends_on": []},
            {"id": "M0.2", "title": "depends on M0.1", "status": "ready", "priority": 80, "depends_on": ["M0.1"]},
            {"id": "M0.3", "title": "depends on M0.2 (not done)", "status": "planned", "priority": 70, "depends_on": ["M0.2"]},
        ]
        result = server.milestone_progress(self._view(items))
        m02 = next(m for m in result["items"] if m["id"] == "M0.2")
        m03 = next(m for m in result["items"] if m["id"] == "M0.3")
        self.assertEqual(["M0.1"], m02["satisfied_deps"])
        self.assertEqual([], m03["satisfied_deps"])
        self.assertEqual(["M0.1"], m02["depends_on"])
        self.assertEqual(["M0.2"], m03["depends_on"])

    def test_exit_criteria_preserved(self):
        items = [
            {"id": "M1", "title": "test", "status": "ready", "priority": 50,
             "depends_on": [], "exit_criteria": "all tests pass"},
        ]
        result = server.milestone_progress(self._view(items))
        self.assertEqual("all tests pass", result["items"][0]["exit_criteria"])

    def test_groups_still_computed(self):
        items = [
            {"id": "M0.1", "title": "a", "status": "done", "priority": 100, "depends_on": [], "parent": "M0"},
            {"id": "M0.2", "title": "b", "status": "ready", "priority": 90, "depends_on": ["M0.1"], "parent": "M0"},
            {"id": "M1.1", "title": "c", "status": "done", "priority": 80, "depends_on": ["M0.2"], "parent": "M1"},
        ]
        result = server.milestone_progress(self._view(items))
        groups = {g["id"]: g for g in result["groups"]}
        self.assertEqual(2, groups["M0"]["total"])
        self.assertEqual(1, groups["M0"]["done"])
        self.assertEqual(1, groups["M1"]["total"])
        self.assertEqual(1, groups["M1"]["done"])

    def test_missing_fields_use_defaults(self):
        items = [{"id": "M1", "title": "sparse"}]  # no status, priority, depends_on, exit_criteria
        result = server.milestone_progress(self._view(items))
        m = result["items"][0]
        self.assertEqual("planned", m["status"])
        self.assertEqual(50, m["priority"])
        self.assertEqual([], m["depends_on"])
        self.assertEqual([], m["satisfied_deps"])
        self.assertEqual("", m["exit_criteria"])


class MilestoneProgressFromStateTests(unittest.TestCase):
    """Test milestone_progress reads from actual milestones.json via state.snapshot()."""

    def test_full_view_with_milestones_json(self):
        with tempfile.TemporaryDirectory() as folder:
            milestones_path = Path(folder) / "milestones.json"
            milestones_path.write_text(
                json.dumps({
                    "milestones": [
                        {"id": "M0.1", "title": "bootstrap", "status": "done",
                         "priority": 100, "depends_on": [],
                         "exit_criteria": "dashboard loads"},
                        {"id": "M0.5", "title": "progress display", "status": "ready",
                         "priority": 80, "depends_on": ["M0.1"],
                         "exit_criteria": "table shows priority and deps"},
                    ]
                }),
                encoding="utf-8",
            )
            runtime_path = Path(folder) / "runtime.json"
            runtime_path.write_text(
                json.dumps({
                    "aios_priority": True,
                    "handoff_granted": False,
                    "local_llm": {"enabled": False},
                    "slots": {"aios_reserved": 6, "smart_store_reserved": 1, "active": 0},
                }),
                encoding="utf-8",
            )
            with mock.patch.object(state_module, "MILESTONES_PATH", milestones_path), \
                 mock.patch.object(state_module, "RUNTIME_PATH", runtime_path), \
                 mock.patch.object(state_module, "probe_aios", lambda: {"available_slots": 5}):
                view = state_module.snapshot()
                result = server.milestone_progress(view)
            self.assertEqual(2, result["total"])
            self.assertEqual(1, result["done"])
            # M0.1 (priority 100) should come first
            self.assertEqual("M0.1", result["items"][0]["id"])
            self.assertEqual("M0.5", result["items"][1]["id"])
            # M0.5 depends on M0.1 which is done
            m05 = next(m for m in result["items"] if m["id"] == "M0.5")
            self.assertEqual(["M0.1"], m05["satisfied_deps"])


if __name__ == "__main__":
    unittest.main()
