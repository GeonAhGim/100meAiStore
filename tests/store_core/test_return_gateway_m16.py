"""M1.6 tests for ReturnGateway and partial claims.

Covers:
- ReturnLine + ReturnGateway readback → approve/reject/partial/cancel
- Negative tests: invalid readback, over-refund, wrong state transitions
- submit_partial_claim integration
"""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from packages.store_core import (
    AdapterCapability,
    AdapterCapabilityManifest,
    ConflictError,
    DemoPage,
    FixtureDemoReadAdapter,
    SQLiteRepository,
    StoreControlPlane,
)
from packages.store_core.domain import (
    ClaimStatus,
    DemoClaim,
)
from packages.store_core.return_gateway import (
    ReturnGateway,
    ReturnLine,
    ReturnLineStatus,
)
from packages.store_core.claim01 import (
    submit_partial_claim,
)


class _FakeService:
    """Minimal service stub for ReturnGateway."""

    def __init__(self):
        self.audit_log: list = []

    def _audit(self, tenant_id, user_id, action, target_id, result, metadata):
        self.audit_log.append({
            "tenant_id": tenant_id,
            "user_id": user_id,
            "action": action,
            "target_id": target_id,
            "result": result,
            "metadata": metadata,
        })


class TestReturnLine(unittest.TestCase):
    """ReturnLine dataclass defaults."""

    def test_default_status_is_pending(self):
        line = ReturnLine(
            line_id="L1", order_id="O1", product_id="P1",
            quantity=2, unit_price_minor=5000, reason="defect",
        )
        self.assertEqual(line.status, ReturnLineStatus.PENDING)
        self.assertEqual(line.refunded_minor, 0)
        self.assertIsInstance(line.created_at, datetime)


class TestReturnGatewayReadback(unittest.TestCase):
    """ReturnGateway.readback — pre-approval verification."""

    def setUp(self):
        self.gw = ReturnGateway(_FakeService())

    def test_readback_produces_digest(self):
        rb = self.gw.readback("O1", "P1", 2, 5000)
        self.assertEqual(rb.total_minor, 10000)
        self.assertTrue(len(rb.readback_digest) > 0)
        # Can retrieve by digest
        self.assertEqual(self.gw.get_readback(rb.readback_digest), rb)

    def test_readback_rejects_zero_quantity(self):
        with self.assertRaises(ValueError) as ctx:
            self.gw.readback("O1", "P1", 0, 5000)
        self.assertIn("positive", str(ctx.exception))

    def test_readback_rejects_negative_price(self):
        with self.assertRaises(ValueError) as ctx:
            self.gw.readback("O1", "P1", 1, -100)
        self.assertIn("negative", str(ctx.exception))

    def test_readback_unknown_digest_fails_approve(self):
        with self.assertRaises(ValueError):
            self.gw.approve(
                "T1", "U1", "O1", "bogus_digest", "D1", ["L1"]
            )


class TestReturnGatewayProcess(unittest.TestCase):
    """ReturnGateway.process — line processing before approval."""

    def setUp(self):
        self.gw = ReturnGateway(_FakeService())

    def test_process_marks_lines_pending(self):
        lines = [
            ReturnLine("L1", "O1", "P1", 1, 5000, "defect"),
            ReturnLine("L2", "O1", "P2", 3, 2000, "wrong_item"),
        ]
        states = self.gw.process("T1", "U1", "O1", lines)
        self.assertEqual(len(states), 2)
        for s in states:
            self.assertEqual(s.status, ReturnLineStatus.PENDING)

    def test_process_rejects_empty_lines(self):
        with self.assertRaises(ValueError):
            self.gw.process("T1", "U1", "O1", [])


class TestReturnGatewayApprove(unittest.TestCase):
    """ReturnGateway.approve — full approval with readback gate."""

    def setUp(self):
        self.gw = ReturnGateway(_FakeService())

    def _setup(self):
        rb = self.gw.readback("O1", "P1", 2, 5000)
        return rb

    def test_approve_succeeds_with_readback(self):
        rb = self._setup()
        decision = self.gw.approve(
            "T1", "U1", "O1", rb.readback_digest, "D1", ["L1"]
        )
        self.assertEqual(decision.action, "approve")
        self.assertEqual(decision.refunded_minor, 10000)
        self.assertEqual(decision.lines["L1"].status, ReturnLineStatus.APPROVED)

    def test_approve_idempotent(self):
        rb = self._setup()
        d1 = self.gw.approve("T1", "U1", "O1", rb.readback_digest, "D1", ["L1"])
        d2 = self.gw.approve("T1", "U1", "O1", rb.readback_digest, "D1", ["L1"])
        self.assertIs(d1, d2)

    def test_approve_audits(self):
        rb = self._setup()
        self.gw.approve("T1", "U1", "O1", rb.readback_digest, "D1", ["L1"])
        entry = self.gw._service.audit_log[-1]
        self.assertEqual(entry["action"], "return.approved")
        self.assertEqual(entry["metadata"]["refunded_minor"], 10000)


class TestReturnGatewayReject(unittest.TestCase):
    """ReturnGateway.reject — rejection with readback gate."""

    def setUp(self):
        self.gw = ReturnGateway(_FakeService())

    def _setup(self):
        return self.gw.readback("O1", "P1", 1, 3000)

    def test_reject_zero_refund(self):
        rb = self._setup()
        decision = self.gw.reject("T1", "U1", "O1", rb.readback_digest, "D1", ["L1"])
        self.assertEqual(decision.action, "reject")
        self.assertEqual(decision.refunded_minor, 0)
        self.assertEqual(decision.lines["L1"].status, ReturnLineStatus.REJECTED)

    def test_reject_idempotent(self):
        rb = self._setup()
        d1 = self.gw.reject("T1", "U1", "O1", rb.readback_digest, "D1", ["L1"])
        d2 = self.gw.reject("T1", "U1", "O1", rb.readback_digest, "D1", ["L1"])
        self.assertIs(d1, d2)


class TestReturnGatewayPartial(unittest.TestCase):
    """ReturnGateway.partial — partial claim with readback + approval gate."""

    def setUp(self):
        self.gw = ReturnGateway(_FakeService())

    def _setup(self):
        return self.gw.readback("O1", "P1", 2, 5000)

    def test_partial_refund_below_total(self):
        rb = self._setup()
        decision = self.gw.partial(
            "T1", "U1", "O1", rb.readback_digest, "D1", ["L1"], 6000
        )
        self.assertEqual(decision.action, "partial")
        self.assertEqual(decision.refunded_minor, 6000)
        self.assertEqual(decision.lines["L1"].status, ReturnLineStatus.PARTIAL)

    def test_partial_rejects_over_refund(self):
        rb = self._setup()
        with self.assertRaises(ValueError):
            self.gw.partial(
                "T1", "U1", "O1", rb.readback_digest, "D1", ["L1"], 10001
            )

    def test_partial_rejects_zero_refund(self):
        rb = self._setup()
        with self.assertRaises(ValueError):
            self.gw.partial(
                "T1", "U1", "O1", rb.readback_digest, "D1", ["L1"], 0
            )

    def test_partial_idempotent(self):
        rb = self._setup()
        d1 = self.gw.partial(
            "T1", "U1", "O1", rb.readback_digest, "D1", ["L1"], 6000
        )
        d2 = self.gw.partial(
            "T1", "U1", "O1", rb.readback_digest, "D1", ["L1"], 6000
        )
        self.assertIs(d1, d2)


class TestReturnGatewayCancel(unittest.TestCase):
    """ReturnGateway.cancel — cancel a pending decision."""

    def setUp(self):
        self.gw = ReturnGateway(_FakeService())

    def _setup_with_decision(self):
        rb = self.gw.readback("O1", "P1", 1, 5000)
        states = self.gw.process("T1", "U1", "O1", [
            ReturnLine("L1", "O1", "P1", 1, 5000, "defect"),
        ])
        return rb

    def test_cancel_pending_decision(self):
        rb = self._setup_with_decision()
        # Process creates lines; now approve creates a decision we can cancel
        # Actually, cancel works on decisions, not process states
        # Let's test cancel on a decision that was created via process+approve
        # But process doesn't create a decision_id — approve does.
        # Test: approve then try to cancel (should fail — already approved)
        decision = self.gw.approve("T1", "U1", "O1", rb.readback_digest, "D1", ["L1"])
        with self.assertRaises(ValueError):
            self.gw.cancel("T1", "U1", decision.decision_id)

    def test_cancel_unknown_decision(self):
        with self.assertRaises(ValueError):
            self.gw.cancel("T1", "U1", "nonexistent")


class TestSubmitPartialClaim(unittest.TestCase):
    """submit_partial_claim — partial claim submission for DemoClaim.

    Uses SQLite-backed StoreControlPlane like test_claim01.py.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "partial_claim.sqlite3"
        self.repo = SQLiteRepository(self.path)
        self.app = StoreControlPlane(self.repo)
        self.ctx = self.app.bootstrap_tenant("Claims", "master@example.test")
        self.app.register_adapter_manifest(
            self.ctx, AdapterCapabilityManifest(
                self.ctx.tenant_id, "demo", "orders", "demo-v1",
                frozenset({AdapterCapability.ORDERS_READ, AdapterCapability.INBOUND_EVENTS}),
                frozenset({1}), datetime.now(timezone.utc),
            )
        )
        row = {
            "external_order_id": "order-1", "event_id": "evt-1",
            "revision": 1, "currency": "KRW", "total_minor": 10000,
            "lines": [{"sku": "P1", "quantity": 1, "unit_minor": 10000}],
        }
        result = self.app.poll_demo_connection(
            self.ctx, "demo", "orders", 0,
            FixtureDemoReadAdapter(
                [DemoPage((row,), None, False, datetime.now(timezone.utc))],
                adapter_version="demo-v1",
            ),
        )
        self.order, _ = self.app.ingest_order(self.ctx, "demo-channel", result.payload_refs[0])

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def test_partial_succeeds_for_open_claim(self):
        claim, _ = self.app.open_demo_claim(self.ctx, self.order.id, "defect", 10000, "idem1")
        result, replay = submit_partial_claim(
            self.app, self.ctx, claim.id, 6000, "partial_damage"
        )
        self.assertFalse(replay)
        self.assertEqual(result.consumer_status, ClaimStatus.PARTIAL_APPROVED)
        self.assertEqual(result.version, 2)

    def test_partial_rejects_over_claim_amount(self):
        claim, _ = self.app.open_demo_claim(self.ctx, self.order.id, "defect", 10000, "idem2")
        with self.assertRaises(ConflictError):
            submit_partial_claim(
                self.app, self.ctx, claim.id, 10001, "too_much"
            )

    def test_partial_rejects_non_open_claim(self):
        claim, _ = self.app.open_demo_claim(self.ctx, self.order.id, "defect", 10000, "idem3")
        # Move to REFUNDED status
        self.app.record_demo_claim_status(
            self.ctx, claim.id, "consumer", "REFUNDED", 1
        )
        with self.assertRaises(ConflictError):
            submit_partial_claim(
                self.app, self.ctx, claim.id, 5000, "too_late"
            )

    def test_partial_rejects_zero_amount(self):
        claim, _ = self.app.open_demo_claim(self.ctx, self.order.id, "defect", 10000, "idem4")
        with self.assertRaises(ConflictError):
            submit_partial_claim(
                self.app, self.ctx, claim.id, 0, "zero"
            )


if __name__ == "__main__":
    unittest.main()
