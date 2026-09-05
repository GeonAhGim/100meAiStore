from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from packages.store_core import SQLiteRepository, StoreControlPlane
from packages.store_core.dashboard import DashboardProjection
from packages.store_core.domain import AgentState, AgentStatusSnapshot


class DashboardProjectionTest(unittest.TestCase):
    def test_projection_is_tenant_scoped_and_marks_stale_heartbeat(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite3"
            repo = SQLiteRepository(path)
            try:
                app = StoreControlPlane(repo)
                first = app.bootstrap_tenant("First", "first-dashboard@example.test")
                second = app.bootstrap_tenant("Second", "second-dashboard@example.test")
                now = datetime(2026, 9, 5, tzinfo=timezone.utc)
                repo.save_agent_status(AgentStatusSnapshot(
                    first.tenant_id, "codex_pm_luna", "pm", AgentState.RUNNING,
                    "dashboard", now - timedelta(minutes=2), now - timedelta(minutes=1), None,
                    "last message", "1c933ca", "25 passed", "durable inbox", None, False, now - timedelta(minutes=1),
                ))
                snapshot = app.dashboard_snapshot(first, project_root=temp)
                self.assertEqual(first.tenant_id, snapshot["tenant_id"])
                self.assertEqual("stale", snapshot["agents"][0]["state"])
                self.assertTrue(snapshot["agents"][0]["stale"])
                self.assertEqual("unknown", snapshot["tests"]["last_result"])
                self.assertIsNone(snapshot["phase"]["completion_percent"])
                self.assertEqual(0, snapshot["tokens_cost"]["total_tokens"])
                with self.assertRaises(Exception):
                    app.dashboard_snapshot(type(first)(first.tenant_id, "missing", first.membership_version), project_root=temp)
                self.assertNotIn(second.tenant_id, json.dumps(snapshot))
            finally:
                repo.close()

    def test_phase_and_test_checkpoint_are_never_fabricated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".codex").mkdir()
            (root / ".codex" / "phase-progress.json").write_text(json.dumps({
                "phase": "Phase 2", "completed_acceptance": 25, "total_acceptance": 49,
                "evidence": ["phase2-test-plan.md"]
            }), encoding="utf-8")
            (root / ".codex" / "last-test.json").write_text(json.dumps({
                "result": "passed", "passed": 25, "failed": 0, "finished_at": "2026-09-05T00:00:00+00:00"
            }), encoding="utf-8")
            repo = SQLiteRepository(root / "state.sqlite3")
            try:
                app = StoreControlPlane(repo)
                context = app.bootstrap_tenant("Evidence", "evidence-dashboard@example.test")
                snapshot = app.dashboard_snapshot(context, project_root=root)
                self.assertEqual(51.0, snapshot["phase"]["completion_percent"])
                self.assertEqual("passed", snapshot["tests"]["last_result"])
                self.assertEqual(25, snapshot["tests"]["passed"])
            finally:
                repo.close()


if __name__ == "__main__":
    unittest.main()
