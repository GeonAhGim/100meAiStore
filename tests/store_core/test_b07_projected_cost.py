from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from packages.store_core import AdapterCapability, AdapterCapabilityManifest, DemoPage, FixtureDemoReadAdapter, SQLiteRepository, StoreControlPlane


class B07ProjectedCostTests(unittest.TestCase):
    def test_realized_reconciliation_keeps_projected_cost_separate(self):
        temp = tempfile.TemporaryDirectory(); repo = SQLiteRepository(Path(temp.name) / "finance.sqlite3"); app = StoreControlPlane(repo)
        ctx = app.bootstrap_tenant("B07", "b07@example.test")
        try:
            app.register_adapter_manifest(ctx, AdapterCapabilityManifest(ctx.tenant_id, "demo", "orders", "demo-v1", frozenset({AdapterCapability.ORDERS_READ, AdapterCapability.INBOUND_EVENTS}), frozenset({1}), datetime.now(timezone.utc)))
            row = {"external_order_id": "order-1", "event_id": "event-1", "revision": 1, "currency": "KRW", "total_minor": 500, "lines": [{"sku": "sku-1", "quantity": 1, "unit_minor": 500}]}
            result = app.poll_demo_connection(ctx, "demo", "orders", 0, FixtureDemoReadAdapter([DemoPage((row,), None, False, datetime.now(timezone.utc))], adapter_version="demo-v1"))
            order, _ = app.ingest_order(ctx, "demo-channel", result.payload_refs[0])
            app.propose_routing(ctx, order.id, {"sku-1": [{"supplier_id": "supplier-1", "unit_cost_minor": 200, "available_quantity": 1}]})
            batch, _ = app.import_demo_settlement(ctx, "demo-channel", "2026-09", [{"external_order_key": "order-1", "kind": "SALE", "amount_minor": 500, "currency": "KRW", "source_row_ref": "sale-1"}], "settlement-1")
            profit = app.realized_profits(ctx, batch.id)[0]
            self.assertEqual(300, profit.projected_minor); self.assertEqual(500, profit.realized_minor); self.assertNotEqual(profit.projected_minor, profit.realized_minor)
        finally:
            repo.close(); temp.cleanup()


if __name__ == "__main__": unittest.main()
