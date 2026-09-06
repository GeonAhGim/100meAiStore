from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from packages.store_core import AdapterCapability, AdapterCapabilityManifest, DemoPage, FixtureDemoReadAdapter, SQLiteRepository, StoreControlPlane
from packages.store_core.domain import SettlementStatus


class B07SplitRestartRollbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.path = Path(self.temp.name) / "finance.sqlite3"
        self.repo = SQLiteRepository(self.path); self.app = StoreControlPlane(self.repo); self.ctx = self.app.bootstrap_tenant("B07", "split@example.test")
        self.app.register_adapter_manifest(self.ctx, AdapterCapabilityManifest(self.ctx.tenant_id, "demo", "orders", "demo-v1", frozenset({AdapterCapability.ORDERS_READ, AdapterCapability.INBOUND_EVENTS}), frozenset({1}), datetime.now(timezone.utc)))
        row = {"external_order_id": "order-1", "event_id": "event-1", "revision": 1, "currency": "KRW", "total_minor": 500, "lines": [{"sku": "sku-a", "quantity": 1, "unit_minor": 200}, {"sku": "sku-b", "quantity": 1, "unit_minor": 300}]}
        result = self.app.poll_demo_connection(self.ctx, "demo", "orders", 0, FixtureDemoReadAdapter([DemoPage((row,), None, False, datetime.now(timezone.utc))], adapter_version="demo-v1"))
        self.order, _ = self.app.ingest_order(self.ctx, "demo-channel", result.payload_refs[0])

    def tearDown(self): self.repo.close(); self.temp.cleanup()

    def test_split_po_costs_and_settlement_survive_restart(self):
        claim, _ = self.app.open_demo_claim(self.ctx, self.order.id, "delivery", 50, "claim-1")
        self.app.propose_routing(self.ctx, self.order.id, {"sku-a": [{"supplier_id": "supplier-a", "unit_cost_minor": 100, "available_quantity": 1}], "sku-b": [{"supplier_id": "supplier-b", "unit_cost_minor": 150, "available_quantity": 1}]})
        rows = [{"external_order_key": "order-1", "kind": "SALE", "amount_minor": 500, "currency": "KRW", "source_row_ref": "sale-1"}]
        batch, _ = self.app.import_demo_settlement(self.ctx, "demo-channel", "2026-09", rows, "batch-1")
        profit = self.app.realized_profits(self.ctx, batch.id)[0]
        self.assertEqual(250, profit.projected_minor); self.assertEqual(500, profit.realized_minor); self.assertEqual(SettlementStatus.RECONCILED, batch.status)
        self.repo.close(); self.repo = SQLiteRepository(self.path); self.app = StoreControlPlane(self.repo)
        restored = self.app.context_for(self.ctx.tenant_id, self.ctx.user_id)
        self.assertEqual(batch.id, self.app.settlement_batch(restored, batch.id).id)
        self.assertEqual(250, self.app.realized_profits(restored, batch.id)[0].projected_minor)
        self.assertEqual("OPEN", self.app.claim(restored, claim.id).consumer_status.value)

    def test_settlement_outbox_failure_rolls_back_batch_lines_and_profit(self):
        original = self.repo.append_outbox; self.repo.append_outbox = lambda event: (_ for _ in ()).throw(RuntimeError("crash"))
        rows = [{"external_order_key": "order-1", "kind": "SALE", "amount_minor": 500, "currency": "KRW", "source_row_ref": "sale-2"}]
        with self.assertRaises(RuntimeError): self.app.import_demo_settlement(self.ctx, "demo-channel", "2026-09", rows, "batch-2")
        self.repo.append_outbox = original
        self.assertEqual((), self.repo.budget_entries_for(self.ctx.tenant_id))
        self.assertEqual(0, self.repo.connection.execute("SELECT count(*) FROM demo_settlement_batches WHERE tenant_id=?", (self.ctx.tenant_id,)).fetchone()[0])


if __name__ == "__main__": unittest.main()
