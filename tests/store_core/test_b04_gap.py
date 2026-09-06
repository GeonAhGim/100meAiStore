from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from packages.store_core import ConflictError, SQLiteRepository, StoreControlPlane


class B04GapTests(unittest.TestCase):
    def test_inventory_and_price_evidence_survive_restart(self):
        temp = tempfile.TemporaryDirectory(); path = Path(temp.name) / "b04.sqlite3"
        repo = SQLiteRepository(path); app = StoreControlPlane(repo); ctx = app.bootstrap_tenant("B04", "b04@example.test")
        try:
            app.record_demo_inventory(ctx, "sku-1", "supplier-1", 3)
            projection = app.record_demo_price_projection(ctx, "sku-1", 1000, 900, fee_rate=Decimal("0.05"))
            self.assertEqual("BLOCKED", projection.status)
            repo.close(); repo = SQLiteRepository(path); app = StoreControlPlane(repo); restored = app.context_for(ctx.tenant_id, ctx.user_id)
            self.assertEqual(1, len(app.inventory_snapshots(restored, "sku-1")))
            self.assertEqual("BLOCKED", app.price_projections(restored, "sku-1")[0].status)
        finally:
            repo.close(); temp.cleanup()

    def test_invalid_inventory_fails_before_persisting(self):
        temp = tempfile.TemporaryDirectory(); repo = SQLiteRepository(Path(temp.name) / "b04.sqlite3"); app = StoreControlPlane(repo); ctx = app.bootstrap_tenant("B04", "invalid@example.test")
        try:
            with self.assertRaises(ConflictError): app.record_demo_inventory(ctx, "sku-1", "supplier-1", -1)
            self.assertEqual(0, len(app.inventory_snapshots(ctx)))
        finally:
            repo.close(); temp.cleanup()


if __name__ == "__main__": unittest.main()
