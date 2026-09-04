import tempfile
import unittest
from pathlib import Path

from smart_store_aios.config import ProfitPolicy
from smart_store_aios.db import StoreDB
from smart_store_aios.policy import CandidateProduct, evaluate
from smart_store_aios.profit import UnitEconomics, weekly_orders_required


class CoreTests(unittest.TestCase):
    def test_economics_includes_variable_reserves(self):
        policy = ProfitPolicy()
        economics = UnitEconomics(50_000, 25_000, 3_000)
        self.assertEqual(economics.contribution(policy), 11_500)
        self.assertEqual(weekly_orders_required(policy, 11_500), 435)

    def test_policy_blocks_incomplete_product(self):
        product = CandidateProduct(
            supplier_sku="x", name="test", economics=UnitEconomics(10_000, 9_000), stock=0
        )
        decision = evaluate(product, ProfitPolicy())
        self.assertFalse(decision.approved)
        self.assertGreaterEqual(len(decision.reasons), 4)

    def test_job_is_claimed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StoreDB(Path(directory) / "test.db")
            db.initialize()
            job_id = db.enqueue("catalog.scan", {"source": "demo"})
            claimed = db.claim("worker-a", 60)
            self.assertEqual(claimed["id"], job_id)
            self.assertIsNone(db.claim("worker-b", 60))


if __name__ == "__main__":
    unittest.main()
