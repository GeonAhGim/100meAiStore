from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from packages.store_core import (
    ApprovalKind,
    ApprovalState,
    AuthorizationError,
    ConflictError,
    Role,
    StoreControlPlane,
    TenantBoundaryError,
)


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


class ControlPlaneTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = MutableClock()
        self.app = StoreControlPlane(clock=self.clock)
        self.master = self.app.bootstrap_tenant("Demo Store", "master@example.test")

    def test_membership_limit_and_stale_session_revocation(self) -> None:
        funds = self.app.add_member(self.master, "funds@example.test", [Role.FUNDS])
        self.app.add_member(self.master, "catalog@example.test", [Role.CATALOG_CS])
        with self.assertRaises(ConflictError):
            self.app.add_member(self.master, "fourth@example.test", [Role.AUDITOR])

        self.app.change_member_roles(self.master, funds.user_id, [Role.AUDITOR])
        with self.assertRaises(AuthorizationError):
            self.app.require(funds, self._purchase_capability())

    @staticmethod
    def _purchase_capability():
        from packages.store_core import Capability

        return Capability.APPROVE_PURCHASE

    def test_purchase_approval_authorization_and_24_hour_expiry(self) -> None:
        catalog = self.app.add_member(self.master, "catalog@example.test", [Role.CATALOG_CS])
        command, approval = self.app.create_command(
            self.master,
            ApprovalKind.PURCHASE,
            "po:demo-1",
            {"amount_minor": 30_000, "currency": "KRW"},
            "purchase:demo-1:v1",
        )
        self.assertEqual(timedelta(hours=24), approval.expires_at - approval.requested_at)
        with self.assertRaises(AuthorizationError):
            self.app.decide(catalog, command.id, True, "not a financial approver")

        with self.assertRaises(AuthorizationError):
            self.app.create_command(
                catalog, ApprovalKind.PURCHASE, "po:unauthorized", {}, "unauthorized:v1"
            )

        self.clock.now += timedelta(hours=24)
        with self.assertRaisesRegex(ConflictError, "expired"):
            self.app.decide(self.master, command.id, True, "too late")
        self.assertEqual(ApprovalState.EXPIRED, approval.state)

    def test_idempotent_creation_rejects_key_collision(self) -> None:
        args = (
            self.master,
            ApprovalKind.PRODUCT,
            "product:demo-1",
            {"name": "Synthetic demo item"},
            "product:demo-1:v1",
        )
        first_command, first_approval = self.app.create_command(*args)
        second_command, second_approval = self.app.create_command(*args)
        self.assertIs(first_command, second_command)
        self.assertIs(first_approval, second_approval)

        with self.assertRaisesRegex(ConflictError, "different command"):
            self.app.create_command(
                self.master,
                ApprovalKind.PRODUCT,
                "product:demo-1",
                {"name": "Materially changed"},
                "product:demo-1:v1",
            )

    def test_material_change_supersedes_even_approved_command(self) -> None:
        command, old_approval = self.app.create_command(
            self.master,
            ApprovalKind.PRODUCT,
            "product:demo-2",
            {"price_minor": 10_000},
            "product:demo-2:v1",
        )
        self.app.decide(self.master, command.id, True, "approved initial facts")
        replacement, new_approval = self.app.supersede(
            self.master, command.id, {"price_minor": 12_000}, "product:demo-2:v2"
        )
        self.assertEqual(ApprovalState.SUPERSEDED, old_approval.state)
        self.assertEqual(ApprovalState.PENDING, new_approval.state)
        self.assertEqual(command.id, replacement.supersedes_id)

    def test_cross_tenant_command_access_is_denied(self) -> None:
        command, _ = self.app.create_command(
            self.master, ApprovalKind.PRODUCT, "product:a", {"safe": True}, "a:v1"
        )
        other = self.app.bootstrap_tenant("Other", "other@example.test")
        with self.assertRaises(TenantBoundaryError):
            self.app.decide(other, command.id, True, "cross tenant attempt")
        self.assertEqual("command.cross_tenant_access", self.app.audit_log(other)[-1].action)

    def test_audit_is_hash_chained_and_exposed_immutably(self) -> None:
        self.app.create_command(
            self.master, ApprovalKind.PRODUCT, "product:audit", {"safe": True}, "audit:v1"
        )
        events = self.app.audit_log(self.master)
        self.assertIsInstance(events, tuple)
        self.assertGreaterEqual(len(events), 2)
        self.assertIsNone(events[0].prev_hash)
        self.assertEqual(events[0].event_hash, events[1].prev_hash)
        self.assertTrue(self.app.verify_audit_chain(self.master.tenant_id))


if __name__ == "__main__":
    unittest.main()
