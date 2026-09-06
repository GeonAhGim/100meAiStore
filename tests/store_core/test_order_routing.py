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

    def test_order06_approval_revalidates_then_demo_response_reconciles(self):
        order, _ = self.app.ingest_order(self.ctx, "demo-channel", self.payload_ref)
        pos = self.app.propose_routing(self.ctx, order.id, {
            "sku-a": [{"supplier_id": "supplier-a", "unit_cost_minor": 100, "available_quantity": 1}],
            "sku-b": [{"supplier_id": "supplier-a", "unit_cost_minor": 150, "available_quantity": 1}],
        })
        approved = self.app.approve_demo_po(self.ctx, pos[0].id, True, "fixture approval")
        self.assertEqual(PurchaseOrderState.APPROVED, approved.status)
        submitted = self.app.submit_demo_po(self.ctx, approved.id)
        self.assertEqual(PurchaseOrderState.SUBMITTED, submitted.status)
        response = {"status": "ACKNOWLEDGED", "provider_reference": "ack-1",
                    "observed_at": datetime.now(timezone.utc)}
        acknowledged, replay = self.app.reconcile_demo_po(self.ctx, submitted.id, response)
        self.assertFalse(replay)
        self.assertEqual(PurchaseOrderState.ACKNOWLEDGED, acknowledged.status)
        same, replay = self.app.reconcile_demo_po(self.ctx, submitted.id, response)
        self.assertTrue(replay)
        self.assertEqual(acknowledged.version, same.version)
        with self.assertRaises(ConflictError):
            self.app.reconcile_demo_po(self.ctx, submitted.id, {**response, "provider_reference": "ack-2"})

    def test_order07_changed_order_blocks_approval_without_mutation(self):
        order, _ = self.app.ingest_order(self.ctx, "demo-channel", self.payload_ref)
        pos = self.app.propose_routing(self.ctx, order.id, {
            "sku-a": [{"supplier_id": "supplier-a", "unit_cost_minor": 100, "available_quantity": 1}],
            "sku-b": [{"supplier_id": "supplier-a", "unit_cost_minor": 150, "available_quantity": 1}],
        })
        current = self.app.order(self.ctx, order.id)
        current.status = ChannelOrderState.CANCELLED
        current.version += 1
        self.repo.update_channel_order(current, current.version - 1)
        with self.assertRaises(ConflictError): self.app.approve_demo_po(self.ctx, pos[0].id, True, "stale")
        self.assertEqual(PurchaseOrderState.APPROVAL_PENDING, self.app.purchase_orders(self.ctx, order.id)[0].status)

    def routed_order(self):
        order, _ = self.app.ingest_order(self.ctx, "demo-channel", self.payload_ref)
        pos = self.app.propose_routing(self.ctx, order.id, {
            "sku-a": [{"supplier_id": "supplier-a", "unit_cost_minor": 100, "available_quantity": 1}],
            "sku-b": [{"supplier_id": "supplier-a", "unit_cost_minor": 150, "available_quantity": 1}],
        })
        return order, pos[0]

    def test_order08_pending_cancel_is_cas_and_cancels_pending_po(self):
        order, po = self.routed_order()
        cancelled, replay = self.app.request_demo_cancel(self.ctx, order.id, "customer request", 2)
        self.assertFalse(replay)
        self.assertEqual(ChannelOrderState.CANCELLED, cancelled.status)
        self.assertEqual(PurchaseOrderState.CANCELLED, self.app.purchase_orders(self.ctx, order.id)[0].status)
        same, replay = self.app.request_demo_cancel(self.ctx, order.id, "replay", 999)
        self.assertTrue(replay); self.assertEqual(cancelled.id, same.id)

    def test_order09_submitted_cancel_keeps_evidence_and_requests_compensation(self):
        order, po = self.routed_order()
        self.app.approve_demo_po(self.ctx, po.id, True, "approve")
        self.app.submit_demo_po(self.ctx, po.id)
        self.app.request_demo_cancel(self.ctx, order.id, "after submit", 2)
        self.assertEqual(PurchaseOrderState.CANCEL_REQUESTED, self.app.purchase_orders(self.ctx, order.id)[0].status)
        self.assertTrue(any(event.topic == "purchase_order.cancel_requested" for event in self.repo.outbox_for(self.ctx.tenant_id)))

    def test_order10_tracking_is_line_level_and_corrected_status_is_append_only(self):
        order, _ = self.routed_order()
        line = self.app.order_lines(self.ctx, order.id)[0]
        first, replay = self.app.ingest_demo_tracking(self.ctx, line.id, "track-1", "IN_TRANSIT")
        self.assertFalse(replay); self.assertEqual("IN_TRANSIT", first.tracking_status)
        same, replay = self.app.ingest_demo_tracking(self.ctx, line.id, "track-1", "IN_TRANSIT")
        self.assertTrue(replay); self.assertEqual(first.version, same.version)
        corrected, replay = self.app.ingest_demo_tracking(self.ctx, line.id, "track-1", "DELIVERED")
        self.assertFalse(replay); self.assertEqual("DELIVERED", corrected.tracking_status)
        self.assertEqual(2, len(self.app.tracking_for(self.ctx, line.id)))


if __name__ == "__main__": unittest.main()
