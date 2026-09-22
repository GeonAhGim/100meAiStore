"""P2-13 offline audit: traceability gaps, restart check, fail-closed readiness."""
import json
import tempfile
import unittest
from pathlib import Path

from packages.store_core.final_audit import (acceptance_ids, readiness, restart_check, run_final_audit,
                                             trace_items)

ROOT = Path(__file__).parents[2]


class FinalAuditTests(unittest.TestCase):
    def test_traceability_maps_items_to_evidence_and_tests_and_reports_gaps(self):
        """P2-13-FINAL-AUDIT-01"""
        traces = {t.item_id: t for t in trace_items(ROOT)}
        self.assertIn("P2-10", traces)
        self.assertTrue(traces["P2-10"].traced, traces["P2-10"].gaps)
        self.assertEqual(("P2-10-DEMO-DISCOVERY-01", "P2-10-DEMO-DISCOVERY-02", "P2-10-DEMO-DISCOVERY-03"),
                         traces["P2-10"].acceptance_ids)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs" / "implementation").mkdir(parents=True)
            (root / "tests").mkdir()
            (root / "docs" / "implementation" / "x.md").write_text(
                "| ID | Acceptance criterion |\n|---|---|\n| X-01 | a |\n| X-02 | b |\n", encoding="utf-8")
            (root / "tests" / "test_x.py").write_text("# X-01 only\n", encoding="utf-8")
            (root / "docs" / "implementation" / "development-progress.json").write_text(json.dumps({"items": [
                {"id": "A", "status": "completed", "evidence": "docs/implementation/x.md"},
                {"id": "B", "status": "completed", "evidence": "docs/implementation/missing.md"},
                {"id": "C", "status": "pending", "evidence": "docs/implementation/x.md"},
            ]}), encoding="utf-8")
            by_id = {t.item_id: t for t in trace_items(root)}
            self.assertEqual(("untested_ids", "status_overclaims"), by_id["A"].gaps)
            self.assertEqual(("X-02",), by_id["A"].untested_ids)
            self.assertEqual(("evidence_missing", "status_overclaims"), by_id["B"].gaps)
            self.assertEqual(("untested_ids",), by_id["C"].gaps)  # pending may be incomplete without overclaim
            self.assertEqual(("X-01", "X-02"), acceptance_ids(root / "docs" / "implementation" / "x.md"))

    def test_bootstrap_and_restart_leave_no_leased_events(self):
        """P2-13-FINAL-AUDIT-02"""
        check = restart_check()
        self.assertTrue(check.passed, check.detail)
        self.assertEqual(0, check.leased)
        self.assertGreaterEqual(check.events, 0)

    def test_readiness_is_fail_closed_and_live_is_not_approved(self):
        """P2-13-FINAL-AUDIT-03"""
        ready = readiness(ROOT / "docs" / "implementation" / "approvals")
        self.assertFalse(ready.live_approved)
        self.assertEqual(("G1", "G2", "G3", "G4", "G5"), ready.not_live)
        self.assertIn("LIVE is not approved", ready.statement)
        self.assertIn("G1=DEMO", ready.statement)
        with tempfile.TemporaryDirectory() as empty:
            missing = readiness(Path(empty))
            self.assertFalse(missing.live_approved)
            self.assertIn("G5=missing", missing.statement)
        report = run_final_audit(ROOT)
        self.assertIsNone(report.error)
        self.assertFalse(report.readiness.live_approved)
        self.assertEqual(64, len(report.digest))
        broken = run_final_audit(Path("Z:/does/not/exist"))
        self.assertFalse(broken.ready)
        self.assertIsNotNone(broken.error)


if __name__ == "__main__":
    unittest.main()
