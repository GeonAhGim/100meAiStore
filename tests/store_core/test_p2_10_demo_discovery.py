"""P2-10 DEMO Discovery: coverage, ID roundtrip, permission report, fail-closed gates."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from packages.store_core.channel_order_contracts import parse_coupang_day_page
from packages.store_core.demo_discovery import (DiscoveryBlocked, approval_modes, roundtrip_losses,
                                                run_demo_discovery)
from packages.store_core.supplier_file_contracts import HEADERS

ROOT = Path(__file__).parents[2]
APPROVALS = ROOT / "docs" / "implementation" / "approvals"
CSV = (','.join(HEADERS) + '\n001,000123,fixture-model,fixture-brand,fixture-spec,blue,one,10,500,KRW,'
       '2026-09-07T00:00:00+00:00\n').encode()


def _orders():
    return json.loads((ROOT / "tests" / "fixtures" / "channel_orders.json").read_text(encoding="utf-8"))


class DemoDiscoveryTests(unittest.TestCase):
    def test_coverage_per_channel_and_supplier(self):
        """P2-10-DEMO-DISCOVERY-01: every source reports rows, present and absent required fields."""
        report = run_demo_discovery(approvals_dir=APPROVALS, channel_orders=_orders(), supplier_csv=CSV,
                                    requested_naver_ids=("fixture-product-order-1",))
        self.assertEqual("DEMO", report.mode)
        self.assertEqual({"naver": 1, "coupang": 1, "supplier": 1}, {c.source: c.rows for c in report.coverage})
        self.assertTrue(all(c.covered for c in report.coverage), [c.absent for c in report.coverage])
        self.assertTrue(report.ready)
        self.assertEqual(64, len(report.digest))
        # no private fixture values leak into the report
        self.assertNotIn("SYNTHETIC PRIVATE", report.canonical_payload())

    def test_external_ids_roundtrip_without_loss(self):
        """P2-10-DEMO-DISCOVERY-02: numeric IDs above 2^53 and composite line IDs survive."""
        report = run_demo_discovery(approvals_dir=APPROVALS, channel_orders=_orders(), supplier_csv=CSV,
                                    requested_naver_ids=("fixture-product-order-1",))
        self.assertEqual((), report.field_loss)
        self.assertEqual(3 + 4, report.external_ids)
        page = parse_coupang_day_page(_orders()["coupang"])
        snapshot = page.snapshots[0]
        self.assertEqual("9007199254740993", snapshot.order_id)  # > 2^53, kept as string
        broken = replace(snapshot, product_id="")
        self.assertIn("coupang.product", roundtrip_losses(broken))

    def test_permission_gaps_and_fail_closed_without_demo_record(self):
        """P2-10-DEMO-DISCOVERY-03: unauthorized fields are named, and a missing or LIVE gate blocks the run."""
        report = run_demo_discovery(approvals_dir=APPROVALS, channel_orders=_orders(), supplier_csv=CSV,
                                    requested_naver_ids=("fixture-product-order-1",))
        self.assertEqual(("naver.ordererName", "coupang.receiver.name", "coupang.receiver.addr1"), report.permission_gaps)
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual({}, approval_modes(Path(empty)))
            with self.assertRaises(DiscoveryBlocked) as ctx:
                run_demo_discovery(approvals_dir=Path(empty), channel_orders=_orders(), supplier_csv=CSV,
                                   requested_naver_ids=("fixture-product-order-1",))
            self.assertIn("G1, G2, G3", str(ctx.exception))
            # a record whose mode is not DEMO does not unlock a DEMO run
            for gate in ("G1", "G2", "G3"):
                (Path(empty) / f"{gate}.md").write_text("# x\n\n- 모드: **LIVE**\n", encoding="utf-8")
            with self.assertRaises(DiscoveryBlocked):
                run_demo_discovery(approvals_dir=Path(empty), channel_orders=_orders(), supplier_csv=CSV,
                                   requested_naver_ids=("fixture-product-order-1",))


if __name__ == "__main__":
    unittest.main()
