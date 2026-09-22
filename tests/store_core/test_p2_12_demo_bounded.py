"""P2-12 DEMO bounded release: scoped approval, caps, audit, halt/restore, fail-closed gates."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from packages.store_core.demo_bounded import (BoundedBlocked, BoundedGate, BoundedPolicy, ProposedWrite,
                                              ScopedApproval, payload_digest)

ROOT = Path(__file__).parents[2]
APPROVALS = ROOT / "docs" / "implementation" / "approvals"
T0 = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
POLICY = BoundedPolicy(1, "update_price", max_amount_minor=0, max_skus=50, max_writes_per_day=2, max_age_seconds=86400)
PAYLOAD = {"price_minor": 12000}


def _approval(digest=None, at=T0, target="offer:offer-1", capability="update_price"):
    return ScopedApproval(capability, target, digest or payload_digest(PAYLOAD), at, "operator")


def _write(at=T0 + timedelta(minutes=1), amount=0, skus=1, payload=PAYLOAD, target="offer:offer-1"):
    return ProposedWrite("update_price", target, payload, amount, skus, at)


class DemoBoundedTests(unittest.TestCase):
    def test_admits_only_with_matching_scope_digest_and_caps(self):
        """P2-12-DEMO-BOUNDED-01"""
        gate = BoundedGate(APPROVALS, POLICY)
        self.assertEqual("admit", gate.decide(_write(), _approval()).decision)
        cases = {
            "scope_mismatch": (_write(), _approval(target="offer:other")),
            "digest_mismatch": (_write(payload={"price_minor": 13000}), _approval()),
            "approval_expired": (_write(at=T0 + timedelta(days=2)), _approval()),
            "amount_cap": (_write(amount=1), _approval()),
            "sku_cap": (_write(skus=51), _approval()),
        }
        for reason, (write, approval) in cases.items():
            record = gate.decide(write, approval)
            self.assertEqual("reject", record.decision, reason)
            self.assertIn(reason, record.reasons)
        self.assertEqual("scope_mismatch", gate.decide(_write(), None).reasons[0])
        # daily cap: second admit ok, third rejected
        self.assertEqual("admit", gate.decide(_write(), _approval()).decision)
        self.assertEqual(("daily_cap",), gate.decide(_write(), _approval()).reasons)

    def test_every_decision_is_audited_and_halt_rejects_until_restore(self):
        """P2-12-DEMO-BOUNDED-02"""
        gate = BoundedGate(APPROVALS, POLICY)
        gate.decide(_write(), _approval())
        gate.decide(_write(skus=99), _approval())
        self.assertEqual([1, 2], [r.sequence for r in gate.ledger])
        self.assertEqual(["admit", "reject"], [r.decision for r in gate.ledger])
        self.assertTrue(all(r.payload_digest and "price_minor" not in json.dumps(r.__dict__) for r in gate.ledger))
        gate.halt(T0 + timedelta(minutes=2), "operator")
        halted = gate.decide(_write(), _approval())
        self.assertEqual(("halted",), halted.reasons)
        gate.restore(T0 + timedelta(minutes=3), "operator")
        self.assertEqual("admit", gate.decide(_write(), _approval()).decision)
        self.assertEqual(["decision", "decision", "halt", "decision", "restore", "decision"], [r.kind for r in gate.ledger])

    def test_stop_restore_evidence_and_fail_closed_gates(self):
        """P2-12-DEMO-BOUNDED-03"""
        gate = BoundedGate(APPROVALS, POLICY)
        gate.decide(_write(), _approval())
        gate.halt(T0, "operator")
        gate.restore(T0, "operator")
        evidence = json.loads(gate.evidence())
        self.assertEqual({"admit": 1, "reject": 0, "halt": 1, "restore": 1}, evidence["counts"])
        self.assertFalse(evidence["halted"])
        self.assertEqual(64, len(evidence["ledger_digest"]))
        self.assertEqual([["G4", "DEMO"], ["G5", "DEMO"]], evidence["gates"])
        with tempfile.TemporaryDirectory() as empty:
            (Path(empty) / "G4.md").write_text("- 모드: **DEMO**\n", encoding="utf-8")
            with self.assertRaises(BoundedBlocked) as ctx:
                BoundedGate(Path(empty), POLICY)
            self.assertIn("G5", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
