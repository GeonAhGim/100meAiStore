import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from smart_store_aios.config import ProfitPolicy
from smart_store_aios.db import StoreDB
from smart_store_aios.policy import CandidateProduct, evaluate
from smart_store_aios.profit import UnitEconomics, weekly_orders_required


class CoreTests(unittest.TestCase):
    def test_cli_init_bootstraps_from_empty_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(__file__).parents[1])
            result = subprocess.run(
                [sys.executable, "-m", "smart_store_aios.cli", "--config", str(config), "init"],
                cwd=directory, env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(config.exists())
            self.assertTrue((Path(directory) / "data" / "store.db").exists())
            self.assertTrue(json.loads(config.read_text(encoding="utf-8"))["dry_run"])

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
