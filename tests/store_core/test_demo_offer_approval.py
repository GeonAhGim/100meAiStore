"""M1 slice: DEMO catalog scan -> gated channel offer -> PRODUCT approval -> fail-closed verification."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from packages.store_core import ConflictError, SQLiteRepository, StoreControlPlane
from packages.store_core.domain import ApprovalKind, ApprovalState

ROW = {"external_key": "source-1", "sku": "sku-1", "title": "Item", "category": "home",
       "price_minor": 1000, "currency": "KRW", "attributes": {}}


class DemoOfferApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "offer.sqlite3"
        self.now = [datetime(2026, 9, 23, tzinfo=timezone.utc)]
        self.repo = SQLiteRepository(self.path)
        self.app = StoreControlPlane(self.repo, lambda: self.now[0])
        self.ctx = self.app.bootstrap_tenant("Offers", "offers@example.test")
        self.app.ingest_demo_catalog(self.ctx, "supplier-1", [ROW], "catalog-1")
        self.product_id = self.repo.connection.execute(
            "SELECT id FROM demo_canonical_products WHERE tenant_id=?", (self.ctx.tenant_id,)).fetchone()[0]

    def tearDown(self):
        self.repo.close(); self.temp.cleanup()

    def evidence(self, quantity=3, supply_cost=700, selling_price=1000):
        self.now[0] += timedelta(seconds=1)  # "latest evidence" is ordered by time; never tie
        self.app.record_demo_inventory(self.ctx, "sku-1", "supplier-1", quantity)
        return self.app.record_demo_price_projection(self.ctx, "sku-1", selling_price, supply_cost)

    def offer(self, channel="channel-1", price=None):
        return self.app.project_demo_offer(self.ctx, self.product_id, channel, price)[0]

    def approved(self):
        self.evidence()
        offer = self.offer()
        command, _ = self.app.request_demo_offer_approval(self.ctx, offer.id, "offer-approval-1")
        self.app.decide(self.ctx, command.id, True, "reviewed DEMO offer")
        return offer, command

    def test_catalog_scan_reaches_an_approval_bound_to_the_exact_offer(self):
        self.evidence()
        offer = self.offer()
        command, approval = self.app.request_demo_offer_approval(self.ctx, offer.id, "offer-approval-1")
        self.assertEqual((ApprovalKind.PRODUCT, f"offer:{offer.id}"), (command.kind, command.target_ref))
        self.assertEqual(ApprovalState.PENDING, approval.state)
        self.assertEqual({"offer_id": offer.id, "sku": "sku-1", "price_minor": 1000, "mode": "DEMO",
                          "external_write_authorized": False},
                         {k: command.payload[k] for k in ("offer_id", "sku", "price_minor", "mode", "external_write_authorized")})
        self.assertEqual("DEMO_ONLY", approval.evidence[0]["risk"])
        same, _ = self.app.request_demo_offer_approval(self.ctx, offer.id, "offer-approval-1")
        self.assertEqual(command.id, same.id)                                         # idempotent replay
        with self.assertRaises(ConflictError):                                        # approval-first: nothing before a decision
            self.app.verify_approved_demo_offer(self.ctx, offer.id, command.id)
        self.app.decide(self.ctx, command.id, True, "reviewed DEMO offer")
        self.repo.close()
        self.repo = SQLiteRepository(self.path); self.app = StoreControlPlane(self.repo, lambda: self.now[0])
        verified = self.app.verify_approved_demo_offer(self.ctx, offer.id, command.id)
        self.assertEqual((offer.id, command.id, False), (verified["offer_id"], verified["command_id"], verified["external_write_authorized"]))
        self.assertEqual(self.ctx.user_id, verified["approved_by"])
        self.assertTrue(self.app.verify_audit_chain(self.ctx.tenant_id))

    def test_approval_is_refused_without_passing_evidence(self):
        offer = self.offer()                                                          # projection alone is lenient
        with self.assertRaises(ConflictError):
            self.app.request_demo_offer_approval(self.ctx, offer.id, "no-evidence")
        self.evidence(quantity=0)
        with self.assertRaises(ConflictError):                                        # nothing in stock
            self.app.request_demo_offer_approval(self.ctx, offer.id, "no-stock")
        self.evidence(supply_cost=990)
        with self.assertRaises(ConflictError):                                        # margin now below threshold
            self.app.request_demo_offer_approval(self.ctx, offer.id, "blocked-margin")
        self.evidence()
        repriced = self.offer("channel-2", price=1200)
        with self.assertRaises(ConflictError):                                        # margin was projected for 1000, not 1200
            self.app.request_demo_offer_approval(self.ctx, repriced.id, "other-price")
        self.assertEqual(0, self.repo.connection.execute(
            "SELECT count(*) FROM commands WHERE tenant_id=? AND kind='product'", (self.ctx.tenant_id,)).fetchone()[0])

    def test_an_approval_stops_verifying_when_its_evidence_goes_bad(self):
        offer, command = self.approved()
        self.evidence(supply_cost=990)
        with self.assertRaises(ConflictError):
            self.app.verify_approved_demo_offer(self.ctx, offer.id, command.id)
        self.evidence()
        self.assertEqual(offer.id, self.app.verify_approved_demo_offer(self.ctx, offer.id, command.id)["offer_id"])
        self.now[0] += timedelta(days=2)
        with self.assertRaises(ConflictError):                                        # stale evidence and an expired approval
            self.app.verify_approved_demo_offer(self.ctx, offer.id, command.id)

    def test_verification_fails_closed_on_rejection_or_a_foreign_approval(self):
        offer, command = self.approved()
        other = self.offer("channel-2")
        with self.assertRaises(ConflictError):                                        # approval belongs to another offer
            self.app.verify_approved_demo_offer(self.ctx, other.id, command.id)
        rejected, _ = self.app.request_demo_offer_approval(self.ctx, other.id, "offer-approval-2")
        self.app.decide(self.ctx, rejected.id, False, "not this channel")
        with self.assertRaises(ConflictError):
            self.app.verify_approved_demo_offer(self.ctx, other.id, rejected.id)


if __name__ == "__main__": unittest.main()
