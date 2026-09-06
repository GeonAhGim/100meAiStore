from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from packages.store_core import ApprovalKind, ConflictError, SQLiteRepository, StoreControlPlane


class B08GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.repo = SQLiteRepository(Path(self.temp.name) / "gateway.sqlite3")
        self.app = StoreControlPlane(self.repo, lambda: datetime(2026, 9, 6, tzinfo=timezone.utc))
        self.ctx = self.app.bootstrap_tenant("Gateway", "gateway@example.test")
        self.app.set_demo_budget_policy(self.ctx, daily_limit_minor=10, monthly_limit_minor=20, generation_limit=2, agent_run_limit=2, max_tokens=1000, max_tool_calls=3, model_tier="economy")

    def tearDown(self): self.repo.close(); self.temp.cleanup()

    def test_opaque_byok_and_typed_tool_gate(self):
        ref = self.app.configure_demo_byok(self.ctx, "openai", "secret-ref:demo", "UNVERIFIED")
        self.assertEqual("secret-ref:demo", ref.secret_ref)
        blocked = self.app.submit_demo_tool(self.ctx, actor_type="agent", actor_id="agent-1", tool="update_price", target_type="offer", target_id="offer-1", input_value={"price_minor": 100}, idempotency_key="tool-1", requested_policy_version=1)
        self.assertEqual("approval_required", blocked["state"])
        with self.assertRaises(ConflictError): self.app.configure_demo_byok(self.ctx, "openai", "sk-raw-secret-value")
        with self.assertRaises(ConflictError): self.app.submit_demo_tool(self.ctx, actor_type="agent", actor_id="agent-1", tool="update_price", target_type="offer", target_id="offer-1", input_value={"api_key": "raw"}, idempotency_key="tool-2", requested_policy_version=1)

    def test_agent_run_budget_stops_without_charge(self):
        run = self.app.record_demo_agent_run(self.ctx, agent_id="agent-1", goal="inspect", policy_version=1, model="economy", prompt_version="p1", input_value={"sku": "sku-1"}, decision={"state": "proposed"}, confidence="high", tool_calls=1, estimated_cost_minor=6, idempotency_key="run-1")
        self.assertEqual("RECORDED", run.outcome)
        blocked = self.app.record_demo_agent_run(self.ctx, agent_id="agent-1", goal="inspect-2", policy_version=1, model="economy", prompt_version="p1", input_value={"sku": "sku-2"}, decision={"state": "proposed"}, confidence="high", tool_calls=1, estimated_cost_minor=6, idempotency_key="run-2")
        self.assertEqual("BLOCKED_BUDGET", blocked.outcome)
        self.assertEqual(1, len(self.app.budget_entries(self.ctx)))


if __name__ == "__main__": unittest.main()
