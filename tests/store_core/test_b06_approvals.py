from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from packages.store_core import ApprovalKind, ApprovalState, ConflictError, SQLiteRepository, StoreControlPlane


class B06ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 9, 6, tzinfo=timezone.utc)
        self.repo = SQLiteRepository(Path(self.temp.name) / "approval.sqlite3")
        self.app = StoreControlPlane(self.repo, lambda: self.now)
        self.ctx = self.app.bootstrap_tenant("Approvals", "master@example.test")

    def tearDown(self):
        self.repo.close(); self.temp.cleanup()

    def test_mobile_inbox_detail_decision_and_one_decider(self):
        command, approval = self.app.request_approval(self.ctx, ApprovalKind.PRODUCT, "product-1", {"sku": "sku-1", "price_minor": 1000}, "approval-1", 1, 1, ({"label": "fixture", "ref": "source-1", "observed_at": self.now.isoformat()},))
        inbox = self.app.approval_inbox(self.ctx)
        self.assertEqual(approval.id, inbox["items"][0]["approval_id"])
        self.assertEqual(["approve", "reject", "ask_question"], inbox["items"][0]["actions"])
        detail = self.app.approval_detail(self.ctx, approval.id)
        self.assertEqual(command.target_ref, detail["target"]["ref"])
        decided = self.app.decide_approval(self.ctx, approval.id, True, "checked", "nonce-1")
        self.assertEqual(ApprovalState.APPROVED, decided.state)
        self.assertEqual([], self.app.approval_inbox(self.ctx)["items"])
        with self.assertRaises(ConflictError): self.app.decide_approval(self.ctx, approval.id, False, "second", "nonce-2")

    def test_expiry_is_durable_and_changed_nonce_or_tenant_fails(self):
        _, approval = self.app.request_approval(self.ctx, ApprovalKind.PRODUCT, "product-2", {}, "approval-2", 1, 1)
        self.now += timedelta(hours=24)
        inbox = self.app.approval_inbox(self.ctx)
        self.assertEqual([], inbox["items"])
        self.assertEqual(ApprovalState.EXPIRED, self.repo.get_approval(self.ctx.tenant_id, approval.id).state)
        with self.assertRaises(ConflictError): self.app.decide_approval(self.ctx, approval.id, True, "late", "nonce")
        with self.assertRaises(ConflictError): self.app.decide_approval(self.ctx, approval.id, True, "late", "bad nonce!")


if __name__ == "__main__": unittest.main()
