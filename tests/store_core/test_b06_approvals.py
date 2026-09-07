from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from packages.store_core import (ApprovalKind, ApprovalState, AuthorizationError, ConflictError,
                                 Role, SQLiteRepository, StoreControlPlane)
from packages.store_core.errors import NotFoundError


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

    def test_three_user_inbox_and_decision_are_scoped_by_approval_kind(self):
        funds = self.app.add_member(self.ctx, "funds@example.test", [Role.FUNDS])
        catalog = self.app.add_member(self.ctx, "catalog@example.test", [Role.CATALOG_CS])
        _, purchase = self.app.request_approval(self.ctx, ApprovalKind.PURCHASE, "po-1", {"amount_minor": 1000}, "po-1", 1, 1)
        _, product = self.app.request_approval(self.ctx, ApprovalKind.PRODUCT, "product-1", {"sku": "fixture"}, "product-1", 1, 1)
        self.assertEqual({purchase.id, product.id}, {item["approval_id"] for item in self.app.approval_inbox(self.ctx)["items"]})
        self.assertEqual([purchase.id], [item["approval_id"] for item in self.app.approval_inbox(funds)["items"]])
        self.assertEqual([product.id], [item["approval_id"] for item in self.app.approval_inbox(catalog)["items"]])
        for context, approval in ((funds, product), (catalog, purchase)):
            with self.assertRaises(AuthorizationError):
                self.app.approval_detail(context, approval.id)
            with self.assertRaises(AuthorizationError):
                self.app.decide_approval(context, approval.id, True, "wrong role", "nonce")
        self.assertEqual(funds.user_id, self.app.decide_approval(funds, purchase.id, True, "checked", "nonce-funds").decided_by)
        self.assertEqual(catalog.user_id, self.app.decide_approval(catalog, product.id, True, "checked", "nonce-catalog").decided_by)
        self.repo.close()
        self.repo = SQLiteRepository(Path(self.temp.name) / "approval.sqlite3")
        self.app = StoreControlPlane(self.repo, lambda: self.now)
        self.assertEqual(funds.user_id, self.repo.get_approval(funds.tenant_id, purchase.id).decided_by)
        self.assertTrue(self.app.verify_audit_chain(self.ctx.tenant_id))

    def test_auditor_can_read_but_cannot_decide_and_revoked_sessions_stop(self):
        auditor = self.app.add_member(self.ctx, "auditor@example.test", [Role.AUDITOR])
        _, approval = self.app.request_approval(self.ctx, ApprovalKind.PRODUCT, "product", {}, "auditor-product", 1, 1)
        self.assertEqual([], self.app.approval_detail(auditor, approval.id)["actions"])
        self.assertEqual([], self.app.approval_inbox(auditor)["items"][0]["actions"])
        with self.assertRaises(AuthorizationError):
            self.app.decide_approval(auditor, approval.id, True, "not allowed", "nonce")
        self.app.revoke_member(self.ctx, auditor.user_id)
        for read in (lambda: self.app.approval_inbox(auditor), lambda: self.app.approval_detail(auditor, approval.id)):
            with self.assertRaises(AuthorizationError):
                read()
        foreign = self.app.bootstrap_tenant("foreign", "foreign@example.test")
        with self.assertRaises(NotFoundError):
            self.app.approval_detail(foreign, approval.id)
        with self.assertRaises(NotFoundError):
            self.app.decide_approval(foreign, approval.id, True, "foreign", "nonce")

    def test_expired_direct_decision_commits_expiry_before_raising(self):
        command, approval = self.app.request_approval(self.ctx, ApprovalKind.PRODUCT, "expired", {}, "expired-direct", 1, 1)
        self.now += timedelta(hours=24)
        with self.assertRaises(ConflictError):
            self.app.decide_approval(self.ctx, approval.id, True, "too late", "nonce")
        self.assertEqual(ApprovalState.EXPIRED, self.repo.get_approval(self.ctx.tenant_id, approval.id).state)
        self.assertEqual(1, sum(event.topic == "approval.expired" and event.aggregate_ref == command.id
                                for event in self.repo.outbox_for(self.ctx.tenant_id)))
        self.assertEqual(1, sum(event.action == "approval.expire" and event.target_ref == approval.id
                                for event in self.repo.audits_for(self.ctx.tenant_id)))

    def test_core_decision_rejects_non_boolean_and_missing_reason(self):
        command, approval = self.app.request_approval(self.ctx, ApprovalKind.PRODUCT, 'strict-decision', {}, 'strict-decision', 1, 1)
        baseline = len(self.repo.outbox_for(self.ctx.tenant_id)), len(self.repo.audits_for(self.ctx.tenant_id))
        for approve, reason in [(value, 'review') for value in ('false', 'true', 0, 1, None, [], {})] + [(True, value) for value in ('', ' ', None, 1, 'x' * 1001)]:
            with self.subTest(approve=approve, reason_type=type(reason).__name__), self.assertRaises(ConflictError):
                self.app.decide(self.ctx, command.id, approve, reason)
            self.assertEqual(ApprovalState.PENDING, self.repo.get_approval(self.ctx.tenant_id, approval.id).state)
            self.assertEqual(baseline, (len(self.repo.outbox_for(self.ctx.tenant_id)), len(self.repo.audits_for(self.ctx.tenant_id))))
        self.assertEqual(ApprovalState.REJECTED, self.app.decide(self.ctx, command.id, False, 'explicit rejection').state)

    def test_material_preview_redacts_secrets_and_contacts_without_changing_intent(self):
        auditor = self.app.add_member(self.ctx, "preview-auditor@example.test", [Role.AUDITOR])
        catalog = self.app.add_member(self.ctx, "preview-catalog@example.test", [Role.CATALOG_CS])
        payload = {
            "before": {"price_minor": 1200},
            "after": {"price_minor": 1000, "api_key": "PRIVATE-API-KEY", "receiver_phone": "010-1111-2222"},
            "profit": {"projected_profit_minor": 200, "margin_ex_ad": "0.20", "margin_with_ad": "0.15", "currency": "KRW"},
            "authorization": "PRIVATE-AUTH",
        }
        evidence = ({"label": "quote", "ref": "source-1", "customer_email": "private@example.test",
                     "observed_at": self.now.isoformat()},)
        command, approval = self.app.request_approval(
            self.ctx, ApprovalKind.PRODUCT, "product-preview", payload, "preview-1", 1, 1, evidence)
        preview = self.app.approval_detail(auditor, approval.id)
        catalog_preview = self.app.approval_detail(catalog, approval.id)
        self.assertEqual({key: value for key, value in preview.items() if key != "actions"},
                         {key: value for key, value in catalog_preview.items() if key != "actions"})
        self.assertEqual([], preview["actions"])
        self.assertEqual(["approve", "reject", "ask_question"], catalog_preview["actions"])
        rendered = repr(preview)
        for private in ("PRIVATE-API-KEY", "010-1111-2222", "PRIVATE-AUTH", "private@example.test"):
            self.assertNotIn(private, rendered)
        self.assertEqual({"price_minor": 1200}, preview["before"])
        self.assertEqual(1000, preview["after"]["price_minor"])
        self.assertEqual(200, preview["profit"]["projected_profit_minor"])
        self.assertIn("sensitive_fields_redacted", preview["risk_badges"])
        self.app.decide_approval(catalog, approval.id, True, "safe preview checked", "preview-nonce")
        self.assertEqual(payload, self.repo.get_command(self.ctx.tenant_id, command.id).payload)
        self.repo.close()
        self.repo = SQLiteRepository(Path(self.temp.name) / "approval.sqlite3")
        self.app = StoreControlPlane(self.repo, lambda: self.now)
        self.assertEqual(payload, self.repo.get_command(self.ctx.tenant_id, command.id).payload)


if __name__ == "__main__": unittest.main()
