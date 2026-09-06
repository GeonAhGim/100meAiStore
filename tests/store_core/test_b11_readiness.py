from pathlib import Path
import tempfile
import unittest

from packages.store_core import SQLiteRepository, StoreControlPlane, evaluate_demo_readiness


class B11ReadinessTests(unittest.TestCase):
    def test_evaluator_is_fail_closed_without_external_gates(self):
        temp = tempfile.TemporaryDirectory(); repo = SQLiteRepository(Path(temp.name) / "readiness.sqlite3")
        try:
            result = evaluate_demo_readiness(repo, live_authorized=True)
            self.assertEqual("DEMO", result["mode"])
            self.assertFalse(result["live_authorized"])
            self.assertTrue(result["stages"]["assisted"])
            self.assertFalse(result["stages"]["bounded"])
            complete = StoreControlPlane(repo).demo_readiness(evidence={"external_contract_review": True, "operator_exit": True})
            self.assertTrue(complete["stages"]["bounded"])
        finally:
            repo.close(); temp.cleanup()


if __name__ == "__main__": unittest.main()
