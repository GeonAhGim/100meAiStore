from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from packages.store_core import (
    AdapterCapability, AdapterCapabilityManifest, ConflictError, DemoPage,
    FixtureDemoReadAdapter, SQLiteRepository, StoreControlPlane,
)
from packages.store_core.domain import ChannelOrderState, PurchaseOrderState


class OrderRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "orders.sqlite3"
        self.repo = SQLiteRepository(self.path)
        self.app = StoreControlPlane(self.repo)
        self.ctx = self.app.bootstrap_tenant("Orders", "master@example.test")
        self.app.register_adapter_manifest(self.ctx, AdapterCapabilityManifest(
            self.ctx.tenant_id, "demo", "orders", "demo-v1",
            frozenset({AdapterCapability.ORDERS_READ, AdapterCapability.INBOUND_EVENTS}),
            frozenset({1}), datetime.now(timezone.utc)))
        row = {"external_order_id": "external-1", "event_id": "evt-1", "revision": 1,
               "currency": "KRW", "total_minor": 500,
               "lines": [{"sku": "sku-a", "quantity": 1, "unit_minor": 200},
                         {"sku": "sku-b", "quantity": 1, "unit_minor": 300}]}
        result = self.app.poll_demo_connection(self.ctx, "demo", "orders", 0,
            FixtureDemoReadAdapter([DemoPage((row,), None, False, datetime.now(timezone.utc))], adapter_version="demo-v1"))
        self.payload_ref = result.payload_refs[0]

    def tearDown(self):
        self.repo.close(); self.temp.cleanup()

    def test_order01_ingest_is_idempotent_and_durable(self):
        order, replay = self.app.ingest_order(self.ctx, "demo-channel", self.payload_ref)
        self.assertFalse(replay)
        same, replay = self.app.ingest_order(self.ctx, "demo-channel", self.payload_ref)
        self.assertTrue(replay)
        self.assertEqual(order.id, same.id)
        self.assertEqual(ChannelOrderState.ACCEPTED, order.status)
        self.assertEqual(2, len(self.app.order_lines(self.ctx, order.id)))
        self.repo.close()
        self.repo = SQLiteRepository(self.path); self.app = StoreControlPlane(self.repo)
        self.assertEqual(order.id, self.app.order(self.ctx, order.id).id)

    def test_order02_routes_lines_into_separate_approval_pending_pos(self):
        order, _ = self.app.ingest_order(self.ctx, "demo-channel", self.payload_ref)
        pos = self.app.propose_routing(self.ctx, order.id, {
            "sku-a": [{"supplier_id": "supplier-a", "unit_cost_minor": 100, "available_quantity": 1},
                       {"supplier_id": "supplier-b", "unit_cost_minor": 150, "available_quantity": 1}],
            "sku-b": [{"supplier_id": "supplier-b", "unit_cost_minor": 120, "available_quantity": 1}],
        })
        self.assertEqual(2, len(pos))
        self.assertEqual({"supplier-a", "supplier-b"}, {po.supplier_id for po in pos})
        self.assertTrue(all(po.status == PurchaseOrderState.APPROVAL_PENDING for po in pos))
        self.assertEqual(2, len(self.repo.routing_for(self.ctx.tenant_id, order.id)))
        self.assertEqual(2, len(self.app.order(self.ctx, order.id) and self.app.purchase_orders(self.ctx, order.id)))

    def test_order03_route_replay_and_stale_cas_do_not_duplicate_pos(self):
        order, _ = self.app.ingest_order(self.ctx, "demo-channel", self.payload_ref)
        quotes = {"sku-a": [{"supplier_id": "supplier-a", "unit_cost_minor": 100, "available_quantity": 1}],
                  "sku-b": [{"supplier_id": "supplier-a", "unit_cost_minor": 100, "available_quantity": 1}]}
        first = self.app.propose_routing(self.ctx, order.id, quotes)
        with self.assertRaises(ConflictError): self.app.propose_routing(self.ctx, order.id, quotes, expected_order_version=1)
        self.assertEqual(1, len(self.app.purchase_orders(self.ctx, order.id)))
        self.assertEqual(first[0].id, self.app.purchase_orders(self.ctx, order.id)[0].id)

    def test_order04_unavailable_supplier_is_exception_without_po(self):
        order, _ = self.app.ingest_order(self.ctx, "demo-channel", self.payload_ref)
        pos = self.app.propose_routing(self.ctx, order.id, {"sku-a": []})
        self.assertEqual((), pos)
        self.assertEqual(ChannelOrderState.EXCEPTION, self.app.order(self.ctx, order.id).status)
        self.assertEqual((), self.app.purchase_orders(self.ctx, order.id))

    def test_order05_foreign_tenant_order_is_hidden(self):
        first, _ = self.app.ingest_order(self.ctx, "demo-channel", self.payload_ref)
        other = self.app.bootstrap_tenant("Other", "other@example.test")
        with self.assertRaises(Exception): self.app.order(other, first.id)


if __name__ == "__main__": unittest.main()
