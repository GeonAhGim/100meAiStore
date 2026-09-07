from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from packages.store_core import ApprovalKind, ConflictError, Role, SQLiteRepository, StoreControlPlane


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
        baseline = len(self.repo.tool_commands_for(self.ctx.tenant_id))
        for index, value in enumerate((
            {"clientSecret": "raw"}, {"nested": {"access-token": "raw"}},
            {"credential": "raw"}, {"passwordValue": "raw"},
        )):
            with self.subTest(value=value), self.assertRaises(ConflictError):
                self.app.submit_demo_tool(
                    self.ctx, actor_type="agent", actor_id="agent-1", tool="update_price",
                    target_type="offer", target_id="offer-1", input_value=value,
                    idempotency_key=f"secret-variant-{index}", requested_policy_version=1)
        self.assertEqual(baseline, len(self.repo.tool_commands_for(self.ctx.tenant_id)))
        opaque = self.app.submit_demo_tool(
            self.ctx, actor_type="workflow", actor_id="workflow-1", tool="reconcile",
            target_type="channel", target_id="channel-1",
            input_value={"secret_ref": "secret-ref:fixture-vault-entry"},
            idempotency_key="opaque-secret-ref", requested_policy_version=1)
        self.assertEqual("accepted", opaque["state"])
        self.assertFalse(any(row.topic == "tool.command" for row in self.repo.outbox_for(self.ctx.tenant_id)))

    def test_agent_run_budget_stops_without_charge(self):
        run = self.app.record_demo_agent_run(self.ctx, agent_id="agent-1", goal="inspect", policy_version=1, model="economy", prompt_version="p1", input_value={"sku": "sku-1"}, decision={"state": "proposed"}, confidence="high", tool_calls=1, estimated_cost_minor=6, idempotency_key="run-1")
        self.assertEqual("RECORDED", run.outcome)
        blocked = self.app.record_demo_agent_run(self.ctx, agent_id="agent-1", goal="inspect-2", policy_version=1, model="economy", prompt_version="p1", input_value={"sku": "sku-2"}, decision={"state": "proposed"}, confidence="high", tool_calls=1, estimated_cost_minor=6, idempotency_key="run-2")
        self.assertEqual("BLOCKED_BUDGET", blocked.outcome)
        self.assertEqual(1, len(self.app.budget_entries(self.ctx)))

    def approved(self, kind, target, payload, key="gateway-approval"):
        command, approval = self.app.request_approval(self.ctx, kind, target, payload, key, 1, 1)
        self.app.decide(self.ctx, command.id, True, "gateway reviewed")
        return approval

    def test_mutating_tool_binds_exact_approval_intent_and_is_single_use(self):
        approval = self.approved(ApprovalKind.PRODUCT, "offer:offer-1", {"price_minor": 100})
        accepted = self.app.submit_demo_tool(
            self.ctx, actor_type="agent", actor_id="agent-1", tool="update_price",
            target_type="offer", target_id="offer-1", input_value={"price_minor": 100},
            idempotency_key="bound-1", requested_policy_version=1, approval_id=approval.id)
        self.assertEqual("accepted", accepted["state"])
        replay = self.app.submit_demo_tool(
            self.ctx, actor_type="agent", actor_id="agent-1", tool="update_price",
            target_type="offer", target_id="offer-1", input_value={"price_minor": 100},
            idempotency_key="bound-1", requested_policy_version=1, approval_id=approval.id)
        self.assertEqual(accepted["command_id"], replay["command_id"])
        with self.assertRaises(ConflictError):
            self.app.submit_demo_tool(
                self.ctx, actor_type="workflow", actor_id="other", tool="update_price",
                target_type="offer", target_id="offer-1", input_value={"price_minor": 100},
                idempotency_key="bound-1", requested_policy_version=1, approval_id=approval.id)
        second = self.app.submit_demo_tool(
            self.ctx, actor_type="agent", actor_id="agent-1", tool="update_price",
            target_type="offer", target_id="offer-1", input_value={"price_minor": 100},
            idempotency_key="bound-2", requested_policy_version=1, approval_id=approval.id)
        self.assertEqual(("blocked", "approval_already_used"), (second["state"], second["next_action"]))
        self.assertEqual(1, sum(row.topic == "tool.command" for row in self.repo.outbox_for(self.ctx.tenant_id)))
        row = next(row for row in self.repo.tool_commands_for(self.ctx.tenant_id) if row.id == accepted["command_id"])
        self.assertEqual(approval.command_id, row.approval_command_id)
        self.assertEqual(64, len(row.intent_digest))
        self.repo.close()
        self.repo = SQLiteRepository(Path(self.temp.name) / "gateway.sqlite3")
        self.app = StoreControlPlane(self.repo, lambda: datetime(2026, 9, 6, tzinfo=timezone.utc))
        restored = next(row for row in self.repo.tool_commands_for(self.ctx.tenant_id) if row.id == accepted["command_id"])
        self.assertEqual((approval.command_id, row.intent_digest), (restored.approval_command_id, restored.intent_digest))

    def test_unrelated_or_stale_approval_never_authorizes_tool(self):
        cases = [
            (ApprovalKind.PURCHASE, "offer:offer-1", {"price_minor": 100}, "update_price", "offer", "offer-1", {"price_minor": 100}, 1),
            (ApprovalKind.PRODUCT, "offer:other", {"price_minor": 100}, "update_price", "offer", "offer-1", {"price_minor": 100}, 1),
            (ApprovalKind.PRODUCT, "offer:offer-1", {"price_minor": 100}, "update_price", "offer", "offer-1", {"price_minor": 110}, 1),
            (ApprovalKind.PRODUCT, "offer:offer-1", {"price_minor": 100}, "update_price", "offer", "offer-1", {"price_minor": 100}, 2),
        ]
        for index, (kind, target, approved_input, tool, target_type, target_id, actual, policy) in enumerate(cases):
            with self.subTest(index=index):
                approval = self.approved(kind, target, approved_input, f"mismatch-{index}")
                result = self.app.submit_demo_tool(
                    self.ctx, actor_type="agent", actor_id="agent-1", tool=tool,
                    target_type=target_type, target_id=target_id, input_value=actual,
                    idempotency_key=f"mismatch-tool-{index}", requested_policy_version=policy,
                    approval_id=approval.id)
                self.assertEqual("blocked", result["state"])
        catalog = self.app.add_member(self.ctx, "gateway-catalog@example.test", [Role.CATALOG_CS])
        command, approval = self.app.request_approval(
            self.ctx, ApprovalKind.PRODUCT, "offer:revoked", {"price_minor": 100},
            "revoked-approval", 1, 1)
        self.app.decide(catalog, command.id, True, "catalog reviewed")
        self.app.revoke_member(self.ctx, catalog.user_id)
        result = self.app.submit_demo_tool(
            self.ctx, actor_type="agent", actor_id="agent-1", tool="update_price",
            target_type="offer", target_id="revoked", input_value={"price_minor": 100},
            idempotency_key="revoked-tool", requested_policy_version=1, approval_id=approval.id)
        self.assertEqual("blocked", result["state"])
        foreign = self.app.bootstrap_tenant("Foreign gateway", "foreign-gateway@example.test")
        crossed = self.app.submit_demo_tool(
            foreign, actor_type="agent", actor_id="agent-1", tool="update_price",
            target_type="offer", target_id="revoked", input_value={"price_minor": 100},
            idempotency_key="crossed-tool", requested_policy_version=1, approval_id=approval.id)
        self.assertEqual("blocked", crossed["state"])


if __name__ == "__main__": unittest.main()
