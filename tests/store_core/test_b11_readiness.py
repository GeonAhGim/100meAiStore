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
            self.assertFalse(any(result["stages"].values()))
            self.assertFalse(result["stages"]["bounded"])
            complete = StoreControlPlane(repo).demo_readiness(evidence={key: True for key in result["checks"]})
            self.assertTrue(complete["stages"]["bounded"])
        finally:
            repo.close(); temp.cleanup()

    def test_each_missing_false_or_non_boolean_gate_blocks_dependents(self):
        class ReadyStorage:
            def readiness(self):
                return {"ready": True}
        repo = ReadyStorage()
        keys = evaluate_demo_readiness(repo)["checks"]
        complete = dict.fromkeys(keys, True)
        self.assertTrue(evaluate_demo_readiness(repo, evidence=complete)["stages"]["bounded"])
        for key in keys:
            for value in (None, False, "false", "true", 1, [], {}):
                with self.subTest(key=key, value=value):
                    result = evaluate_demo_readiness(repo, evidence=complete | {key: value})
                    self.assertFalse(result["checks"][key])
                    self.assertFalse(result["stages"]["bounded"])
            missing = dict(complete)
            del missing[key]
            self.assertFalse(evaluate_demo_readiness(repo, evidence=missing)["stages"]["bounded"])

    def test_storage_and_evidence_shape_fail_closed(self):
        class UntrustedStorage:
            def readiness(self):
                return {"ready": "false"}
        result = evaluate_demo_readiness(UntrustedStorage(), evidence={"tenant_isolation": True, "immutable_inputs": True})
        self.assertFalse(result["storage_ready"])
        for evidence in (None, [], 1, "false"):
            self.assertFalse(any(evaluate_demo_readiness(UntrustedStorage(), evidence=evidence)["stages"].values()))


if __name__ == "__main__": unittest.main()
