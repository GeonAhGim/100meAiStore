"""P2-11 DEMO Shadow: per-line comparison, freshness, exceptions and fail-closed gates."""
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from packages.store_core.channel_order_contracts import parse_coupang_day_page
from packages.store_core.demo_shadow import ShadowBlocked, run_demo_shadow

ROOT = Path(__file__).parents[2]
APPROVALS = ROOT / "docs" / "implementation" / "approvals"
NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def _page():
    body = json.loads((ROOT / "tests" / "fixtures" / "channel_orders.json").read_text(encoding="utf-8"))["coupang"]
    return parse_coupang_day_page(body)


class DemoShadowTests(unittest.TestCase):
    def test_compares_per_line_and_classifies_drift(self):
        """P2-11-DEMO-SHADOW-01"""
        page = _page()
        same = run_demo_shadow(approvals_dir=APPROVALS, operation=page.snapshots, channel=page,
                               observed_at=NOW - timedelta(minutes=5), now=NOW, polling_window_seconds=600)
        self.assertEqual("continue", same.decision)
        self.assertTrue(all(line.match and line.verified for line in same.lines))
        self.assertEqual((), same.exceptions)
        self.assertEqual(64, len(same.digest))

        drifted = replace(page.snapshots[0], status="DEPARTURE", remaining_quantity=1)
        report = run_demo_shadow(approvals_dir=APPROVALS, operation=(drifted,), channel=page,
                                 observed_at=NOW - timedelta(minutes=5), now=NOW, polling_window_seconds=600)
        self.assertEqual(("status_drift", "quantity_drift"), report.lines[0].classes)
        self.assertEqual("hold", report.decision)
        self.assertEqual({"status_drift", "quantity_drift"}, {e.kind for e in report.exceptions})
        self.assertNotIn("SYNTHETIC PRIVATE", report.canonical_payload())

        extra = replace(page.snapshots[0], line_id="9007199254740995:002")
        missing = run_demo_shadow(approvals_dir=APPROVALS, operation=page.snapshots + (extra,), channel=page,
                                  observed_at=NOW - timedelta(minutes=5), now=NOW, polling_window_seconds=600)
        self.assertEqual("exit", missing.decision)
        self.assertIn("missing_in_channel", [e.kind for e in missing.exceptions])

    def test_stale_page_is_reported_not_accepted(self):
        """P2-11-DEMO-SHADOW-02"""
        page = _page()
        report = run_demo_shadow(approvals_dir=APPROVALS, operation=page.snapshots, channel=page,
                                 observed_at=NOW - timedelta(hours=2), now=NOW, polling_window_seconds=600)
        self.assertTrue(report.stale)
        self.assertEqual(7200, report.age_seconds)
        self.assertEqual("exit", report.decision)
        self.assertEqual("stale_page", report.exceptions[0].kind)
        self.assertFalse(any(line.verified for line in report.lines))  # nothing verified from a stale page
        with self.assertRaises(ValueError):
            run_demo_shadow(approvals_dir=APPROVALS, operation=page.snapshots, channel=page,
                            observed_at=NOW, now=NOW, polling_window_seconds=10)  # window below the bound

    def test_exceptions_rollback_evidence_and_fail_closed_gates(self):
        """P2-11-DEMO-SHADOW-03"""
        page = _page()
        report = run_demo_shadow(approvals_dir=APPROVALS, operation=page.snapshots, channel=page,
                                 observed_at=NOW, now=NOW, polling_window_seconds=600)
        self.assertEqual("none_required", report.rollback)
        self.assertEqual(1, report.pages_compared)
        self.assertEqual(page.source_digest, report.channel_digest)
        self.assertEqual((("G1", "DEMO"), ("G3", "DEMO"), ("G5", "DEMO")), report.gates)
        with tempfile.TemporaryDirectory() as empty:
            (Path(empty) / "G1.md").write_text("- 모드: **DEMO**\n", encoding="utf-8")
            with self.assertRaises(ShadowBlocked) as ctx:
                run_demo_shadow(approvals_dir=Path(empty), operation=page.snapshots, channel=page,
                                observed_at=NOW, now=NOW, polling_window_seconds=600)
            self.assertIn("G3, G5", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
