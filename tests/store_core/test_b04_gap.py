from decimal import Decimal
from datetime import datetime, timezone, timedelta
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

    def test_blocked_projection_cannot_be_published_as_local_offer(self):
        temp = tempfile.TemporaryDirectory(); repo = SQLiteRepository(Path(temp.name) / "b04.sqlite3"); app = StoreControlPlane(repo); ctx = app.bootstrap_tenant("B04", "offer@example.test")
        try:
            batch, _ = app.ingest_demo_catalog(ctx, "supplier-1", [{"external_key": "source-1", "sku": "sku-1", "title": "Item", "category": "home", "price_minor": 1000, "currency": "KRW", "attributes": {}}], "catalog-1")
            product_id = repo.connection.execute("SELECT id FROM demo_canonical_products WHERE tenant_id=?", (ctx.tenant_id,)).fetchone()[0]
            app.record_demo_price_projection(ctx, "sku-1", 1000, 950)
            with self.assertRaises(ConflictError): app.project_demo_offer(ctx, product_id, "channel-1")
        finally:
            repo.close(); temp.cleanup()

    def test_stale_inventory_or_price_requires_fresh_reapproval(self):
        temp = tempfile.TemporaryDirectory(); repo = SQLiteRepository(Path(temp.name) / "b04.sqlite3"); now = [datetime(2026, 9, 6, tzinfo=timezone.utc)]; app = StoreControlPlane(repo, lambda: now[0]); ctx = app.bootstrap_tenant("B04", "fresh@example.test")
        try:
            app.ingest_demo_catalog(ctx, "supplier-1", [{"external_key": "source-1", "sku": "sku-1", "title": "Item", "category": "home", "price_minor": 1000, "currency": "KRW", "attributes": {}}], "catalog-1")
            product_id = repo.connection.execute("SELECT id FROM demo_canonical_products WHERE tenant_id=?", (ctx.tenant_id,)).fetchone()[0]
            app.record_demo_inventory(ctx, "sku-1", "supplier-1", 2)
            app.record_demo_price_projection(ctx, "sku-1", 1000, 700)
            app.project_demo_offer(ctx, product_id, "channel-1")
            now[0] += timedelta(days=2)
            with self.assertRaises(ConflictError): app.project_demo_offer(ctx, product_id, "channel-2")
            app.record_demo_inventory(ctx, "sku-1", "supplier-1", 2)
            app.record_demo_price_projection(ctx, "sku-1", 1000, 700)
            app.project_demo_offer(ctx, product_id, "channel-2")
        finally:
            repo.close(); temp.cleanup()


if __name__ == "__main__": unittest.main()
