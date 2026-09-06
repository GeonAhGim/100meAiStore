from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from packages.store_core import AdapterCapability, AdapterCapabilityManifest, ConflictError, DemoPage, FixtureDemoReadAdapter, SQLiteRepository, StoreControlPlane
from packages.store_core.domain import ClaimStatus


class Claim01Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.path = Path(self.temp.name) / "claim.sqlite3"
        self.repo = SQLiteRepository(self.path); self.app = StoreControlPlane(self.repo)
        self.ctx = self.app.bootstrap_tenant("Claims", "master@example.test")
        self.app.register_adapter_manifest(self.ctx, AdapterCapabilityManifest(self.ctx.tenant_id, "demo", "orders", "demo-v1", frozenset({AdapterCapability.ORDERS_READ, AdapterCapability.INBOUND_EVENTS}), frozenset({1}), datetime.now(timezone.utc)))
        row = {"external_order_id": "order-1", "event_id": "evt-1", "revision": 1, "currency": "KRW", "total_minor": 500, "lines": [{"sku": "sku", "quantity": 1, "unit_minor": 500}]}
        result = self.app.poll_demo_connection(self.ctx, "demo", "orders", 0, FixtureDemoReadAdapter([DemoPage((row,), None, False, datetime.now(timezone.utc))], adapter_version="demo-v1"))
        self.order, _ = self.app.ingest_order(self.ctx, "demo-channel", result.payload_refs[0])

    def tearDown(self): self.repo.close(); self.temp.cleanup()

    def test_claim_intake_and_independent_statuses_are_idempotent(self):
        claim, replay = self.app.open_demo_claim(self.ctx, self.order.id, "return", 400, "claim-1")
        self.assertFalse(replay); self.assertEqual(ClaimStatus.OPEN, claim.consumer_status)
        same, replay = self.app.open_demo_claim(self.ctx, self.order.id, "return", 400, "claim-1")
        self.assertTrue(replay); self.assertEqual(claim.id, same.id)
        claim, replay = self.app.record_demo_claim_status(self.ctx, claim.id, "consumer", "EVIDENCE_PENDING", 1)
        self.assertFalse(replay); self.assertEqual(ClaimStatus.EVIDENCE_PENDING, claim.consumer_status)
        claim, replay = self.app.record_demo_claim_status(self.ctx, claim.id, "channel", "APPROVED", 2)
        self.assertFalse(replay); self.assertEqual(ClaimStatus.APPROVED, claim.channel_status)
        self.assertEqual(ClaimStatus.OPEN, claim.supplier_status)
        same, replay = self.app.record_demo_claim_status(self.ctx, claim.id, "channel", "APPROVED", 999)
        self.assertTrue(replay); self.assertEqual(claim.version, same.version)
        self.assertEqual(2, len(self.app.claim_observations(self.ctx, claim.id)))

    def test_claim_amount_and_cas_fail_closed(self):
        with self.assertRaises(ConflictError): self.app.open_demo_claim(self.ctx, self.order.id, "return", 501, "too-much")
        claim, _ = self.app.open_demo_claim(self.ctx, self.order.id, "return", 100, "claim-2")
        with self.assertRaises(ConflictError): self.app.record_demo_claim_status(self.ctx, claim.id, "consumer", "APPROVED", 99)
        self.assertEqual(1, self.app.claim(self.ctx, claim.id).version)

    def test_claim_restart_and_tenant_isolation(self):
        claim, _ = self.app.open_demo_claim(self.ctx, self.order.id, "damage", 100, "claim-3")
        self.repo.close(); self.repo = SQLiteRepository(self.path); self.app = StoreControlPlane(self.repo)
        self.assertEqual(claim.id, self.app.claim(self.ctx, claim.id).id)
        other = self.app.bootstrap_tenant("Other", "other@example.test")
        with self.assertRaises(Exception): self.app.claim(other, claim.id)


if __name__ == "__main__": unittest.main()
