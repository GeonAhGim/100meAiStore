from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from packages.store_core import AdapterCapability, AdapterCapabilityManifest, ConflictError, DemoPage, FixtureDemoReadAdapter, SQLiteRepository, StoreControlPlane
from packages.store_core.domain import SettlementStatus


class Finance01Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.path = Path(self.temp.name) / "finance.sqlite3"
        self.repo = SQLiteRepository(self.path); self.app = StoreControlPlane(self.repo)
        self.ctx = self.app.bootstrap_tenant("Finance", "master@example.test")
        self.app.register_adapter_manifest(self.ctx, AdapterCapabilityManifest(self.ctx.tenant_id, "demo", "orders", "demo-v1", frozenset({AdapterCapability.ORDERS_READ, AdapterCapability.INBOUND_EVENTS}), frozenset({1}), datetime.now(timezone.utc)))
        row = {"external_order_id": "order-1", "event_id": "evt-1", "revision": 1, "currency": "KRW", "total_minor": 500, "lines": [{"sku": "sku", "quantity": 1, "unit_minor": 500}]}
        result = self.app.poll_demo_connection(self.ctx, "demo", "orders", 0, FixtureDemoReadAdapter([DemoPage((row,), None, False, datetime.now(timezone.utc))], adapter_version="demo-v1"))
        self.order, _ = self.app.ingest_order(self.ctx, "demo-channel", result.payload_refs[0])

    def tearDown(self): self.repo.close(); self.temp.cleanup()

    def test_finance01_reconciles_sale_fee_and_refund_and_preserves_realized_separately(self):
        rows = [{"external_order_key": "order-1", "kind": "SALE", "amount_minor": 500, "currency": "KRW", "source_row_ref": "row-sale"},
                {"external_order_key": "order-1", "kind": "FEE", "amount_minor": -50, "currency": "KRW", "source_row_ref": "row-fee"}]
        batch, replay = self.app.import_demo_settlement(self.ctx, "demo-channel", "2026-09", rows, "file-1")
        self.assertFalse(replay); self.assertEqual(SettlementStatus.RECONCILED, batch.status)
        profit = self.app.realized_profits(self.ctx, batch.id)[0]
        self.assertIsNone(profit.projected_minor); self.assertEqual(450, profit.realized_minor)
        same, replay = self.app.import_demo_settlement(self.ctx, "demo-channel", "2026-09", rows, "file-1")
        self.assertTrue(replay); self.assertEqual(batch.id, same.id)

    def test_finance02_missing_or_currency_mismatch_is_exception(self):
        rows = [{"external_order_key": "missing", "kind": "SALE", "amount_minor": 1, "currency": "KRW", "source_row_ref": "row-1"}]
        batch, _ = self.app.import_demo_settlement(self.ctx, "demo-channel", "2026-09", rows, "file-2")
        self.assertEqual(SettlementStatus.EXCEPTION, batch.status)
        bad = [{"external_order_key": "order-1", "kind": "SALE", "amount_minor": 500, "currency": "USD", "source_row_ref": "row-2"}]
        batch, _ = self.app.import_demo_settlement(self.ctx, "demo-channel", "2026-09", bad, "file-3")
        self.assertEqual(SettlementStatus.EXCEPTION, batch.status)

    def test_finance03_duplicate_source_and_changed_idempotency_are_rejected(self):
        row = {"external_order_key": "order-1", "kind": "SALE", "amount_minor": 500, "currency": "KRW", "source_row_ref": "same"}
        self.app.import_demo_settlement(self.ctx, "demo-channel", "2026-09", [row], "file-4")
        with self.assertRaises(ConflictError): self.app.import_demo_settlement(self.ctx, "demo-channel", "2026-09", [row, row], "file-5")
        changed = {**row, "amount_minor": 400}
        with self.assertRaises(ConflictError): self.app.import_demo_settlement(self.ctx, "demo-channel", "2026-09", [changed], "file-4")


if __name__ == "__main__": unittest.main()
