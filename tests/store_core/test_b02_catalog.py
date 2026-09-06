from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from packages.store_core import ConflictError, SQLiteRepository, StoreControlPlane


class B02CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.temp.name) / "catalog.sqlite3")
        self.app = StoreControlPlane(self.repo)
        self.ctx = self.app.bootstrap_tenant("Catalog", "catalog@example.test")
        self.rows = [{"external_key": "supplier-1", "sku": "sku-1", "title": "Demo item",
                      "category": "home", "price_minor": 1200, "currency": "KRW", "attributes": {"color": "blue"}}]

    def tearDown(self):
        self.repo.close(); self.temp.cleanup()

    def test_source_canonical_lineage_and_local_offer_are_durable(self):
        batch, replay = self.app.ingest_demo_catalog(self.ctx, "supplier", self.rows, "catalog-1")
        self.assertFalse(replay)
        snapshot = self.app.catalog_snapshots(self.ctx, batch.id)[0]
        product_id = self.repo.connection.execute("SELECT id FROM demo_canonical_products WHERE tenant_id=?", (self.ctx.tenant_id,)).fetchone()[0]
        product = self.app.canonical_product(self.ctx, product_id)
        lineage = self.app.product_lineage(self.ctx, product.id)
        self.assertEqual(snapshot.id, lineage[0].source_snapshot_id)
        offer, replay = self.app.project_demo_offer(self.ctx, product.id, "demo-channel")
        self.assertFalse(replay); self.assertEqual(snapshot.id, offer.source_snapshot_id)
        self.repo.close(); self.repo = SQLiteRepository(Path(self.temp.name) / "catalog.sqlite3")
        self.app = StoreControlPlane(self.repo)
        restored = self.app.context_for(self.ctx.tenant_id, self.ctx.user_id)
        self.assertEqual(product.id, self.repo.get_canonical_product(restored.tenant_id, product.id).id)
        self.assertEqual(1, len(self.app.channel_offers(restored, product.id)))

    def test_idempotency_and_strict_rejection_happen_before_writes(self):
        batch, _ = self.app.ingest_demo_catalog(self.ctx, "supplier", self.rows, "catalog-1")
        same, replay = self.app.ingest_demo_catalog(self.ctx, "supplier", self.rows, "catalog-1")
        self.assertTrue(replay); self.assertEqual(batch.id, same.id)
        with self.assertRaises(ConflictError): self.app.ingest_demo_catalog(self.ctx, "supplier", [{**self.rows[0], "price_minor": 1300}], "catalog-1")
        before = self.repo.connection.execute("SELECT count(*) FROM demo_catalog_imports WHERE tenant_id=?", (self.ctx.tenant_id,)).fetchone()[0]
        with self.assertRaises(ConflictError): self.app.ingest_demo_catalog(self.ctx, "supplier", [{**self.rows[0], "email": "pii"}], "bad")
        after = self.repo.connection.execute("SELECT count(*) FROM demo_catalog_imports WHERE tenant_id=?", (self.ctx.tenant_id,)).fetchone()[0]
        self.assertEqual(before, after)


if __name__ == "__main__": unittest.main()
