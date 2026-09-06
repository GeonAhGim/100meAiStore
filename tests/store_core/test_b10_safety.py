from pathlib import Path
import tempfile
import unittest

from packages.store_core import ConflictError, SQLiteRepository, StoreControlPlane


class B10SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.path = Path(self.temp.name) / "safety.sqlite3"
        self.repo = SQLiteRepository(self.path); self.app = StoreControlPlane(self.repo)
        self.ctx = self.app.bootstrap_tenant("Safety", "safety@example.test")

    def tearDown(self): self.repo.close(); self.temp.cleanup()

    def test_stop_scope_blocks_gateway_and_explicit_resume_recovers(self):
        self.app.set_demo_stop(self.ctx, "connection", "channel-1", True, "incident")
        blocked = self.app.submit_demo_tool(self.ctx, actor_type="workflow", actor_id="workflow-1", tool="reconcile", target_type="channel", target_id="channel-1", input_value={}, idempotency_key="stop-1", requested_policy_version=1)
        self.assertEqual("blocked", blocked["state"]); self.assertEqual("stop_active", blocked["next_action"])
        self.app.set_demo_stop(self.ctx, "connection", "channel-1", False, "recovered")
        accepted = self.app.submit_demo_tool(self.ctx, actor_type="workflow", actor_id="workflow-1", tool="reconcile", target_type="channel", target_id="channel-1", input_value={}, idempotency_key="stop-2", requested_policy_version=1)
        self.assertEqual("accepted", accepted["state"])
        with self.assertRaises(ConflictError): self.app.set_demo_stop(self.ctx, "global", "wrong", True, "bad")

    def test_new_path_backup_reopens_with_integrity_and_manifest(self):
        backup = Path(self.temp.name) / "backup.sqlite3"
        manifest = self.app.backup_demo_sqlite(str(backup), self.ctx.tenant_id)
        self.assertTrue(backup.exists()); self.assertEqual(17, manifest.schema_version)
        restored = SQLiteRepository(backup)
        self.assertTrue(restored.readiness()["ready"]); self.assertEqual(self.ctx.tenant_id, restored.get_membership(self.ctx.tenant_id, self.ctx.user_id).tenant_id)
        restored.close()
        with self.assertRaises(ConflictError): self.app.backup_demo_sqlite(str(backup), self.ctx.tenant_id)


if __name__ == "__main__": unittest.main()
