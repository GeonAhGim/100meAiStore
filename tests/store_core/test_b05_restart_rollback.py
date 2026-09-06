from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from packages.store_core import AdapterCapability, AdapterCapabilityManifest, ConflictError, DemoPage, FixtureDemoReadAdapter, SQLiteRepository, StoreControlPlane
from packages.store_core.domain import PurchaseOrderState, ChannelOrderState


class B05RestartRollbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.path = Path(self.temp.name) / "order.sqlite3"
        self.repo = SQLiteRepository(self.path); self.app = StoreControlPlane(self.repo); self.ctx = self.app.bootstrap_tenant("B05", "b05@example.test")
        self.app.register_adapter_manifest(self.ctx, AdapterCapabilityManifest(self.ctx.tenant_id, "demo", "orders", "demo-v1", frozenset({AdapterCapability.ORDERS_READ, AdapterCapability.INBOUND_EVENTS}), frozenset({1}), datetime.now(timezone.utc)))
        row = {"external_order_id": "order-1", "event_id": "event-1", "revision": 1, "currency": "KRW", "total_minor": 500, "lines": [{"sku": "sku-a", "quantity": 1, "unit_minor": 200}, {"sku": "sku-b", "quantity": 1, "unit_minor": 300}]}
        adapter = FixtureDemoReadAdapter([DemoPage((row,), None, False, datetime.now(timezone.utc))], adapter_version="demo-v1")
        result = self.app.poll_demo_connection(self.ctx, "demo", "orders", 0, adapter)
        self.order, _ = self.app.ingest_order(self.ctx, "demo-channel", result.payload_refs[0])

    def tearDown(self): self.repo.close(); self.temp.cleanup()

    def route(self):
        return self.app.propose_routing(self.ctx, self.order.id, {"sku-a": [{"supplier_id": "supplier-a", "unit_cost_minor": 100, "available_quantity": 1}], "sku-b": [{"supplier_id": "supplier-b", "unit_cost_minor": 150, "available_quantity": 1}]})

    def test_split_pos_and_approval_submit_reconcile_survive_restart(self):
        pos = self.route(); self.assertEqual({"supplier-a", "supplier-b"}, {po.supplier_id for po in pos})
        first = self.app.approve_demo_po(self.ctx, pos[0].id, True, "approved")
        self.app.submit_demo_po(self.ctx, first.id)
        self.repo.close(); self.repo = SQLiteRepository(self.path); self.app = StoreControlPlane(self.repo)
        restored = self.app.context_for(self.ctx.tenant_id, self.ctx.user_id)
        orders = self.app.purchase_orders(restored, self.order.id)
        self.assertEqual(PurchaseOrderState.SUBMITTED, next(po.status for po in orders if po.id == first.id))
        self.assertEqual(PurchaseOrderState.APPROVAL_PENDING, next(po.status for po in orders if po.id != first.id))
        ack, replay = self.app.reconcile_demo_po(restored, first.id, {"status": "ACKNOWLEDGED", "provider_reference": "ack-1", "observed_at": datetime.now(timezone.utc)})
        self.assertFalse(replay); self.assertEqual(PurchaseOrderState.ACKNOWLEDGED, ack.status)

    def test_routing_failure_rolls_back_all_order_and_po_writes(self):
        original = self.repo.append_outbox
        self.repo.append_outbox = lambda event: (_ for _ in ()).throw(RuntimeError("simulated crash"))
        with self.assertRaises(RuntimeError): self.route()
        self.repo.append_outbox = original
        self.assertEqual(ChannelOrderState.ACCEPTED, self.app.order(self.ctx, self.order.id).status)
        self.assertEqual((), self.app.purchase_orders(self.ctx, self.order.id))
        self.assertEqual((), self.repo.routing_for(self.ctx.tenant_id, self.order.id))


if __name__ == "__main__": unittest.main()
