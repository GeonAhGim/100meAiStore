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

    def test_installed_wheel_bootstraps_without_source_tree_or_network(self):
        repo = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheelhouse = root / "wheelhouse"
            wheelhouse.mkdir()
            build = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-deps",
                    "--no-build-isolation",
                    str(repo),
                    "--wheel-dir",
                    str(wheelhouse),
                ],
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, build.returncode, build.stderr)
            wheels = list(wheelhouse.glob("*.whl"))
            self.assertEqual(1, len(wheels), build.stdout)

            venv = root / "fresh-venv"
            created = subprocess.run(
                [sys.executable, "-m", "venv", str(venv)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, created.returncode, created.stderr)
            python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            installed = subprocess.run(
                [python, "-m", "pip", "install", "--no-index", "--no-deps", str(wheels[0])],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, installed.returncode, installed.stderr)

            empty_cwd = root / "empty cwd"
            empty_cwd.mkdir()
            config = root / "nested path" / "config.json"
            env = os.environ.copy()
            env.pop("PYTHONPATH", None)
            env["PYTHONNOUSERSITE"] = "1"
            for command in (
                ["init"],
                ["status"],
                ["economics", "--price", "50000", "--cost", "25000", "--shipping", "3000"],
            ):
                result = subprocess.run(
                    [python, "-m", "smart_store_aios.cli", "--config", str(config), *command],
                    cwd=empty_cwd,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(config.exists())
            self.assertTrue((root / "nested path" / "data" / "store.db").exists())
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
