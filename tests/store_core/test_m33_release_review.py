"""M3.3 release review tests: repository evidence, verdict, and blocked gates."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from packages.store_core.release_review import M33ReleaseReview, run_m33_release_review


class M33ReleaseReviewTest(unittest.TestCase):
    """Test M3.3 release review: DEMO approval, LIVE no-go, blocked gates."""

    def setUp(self):
        """Create temporary repository structure."""
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp_dir.name)

        # Create required directories
        (self.root / "docs" / "implementation" / "approvals").mkdir(parents=True, exist_ok=True)
        (self.root / "packages" / "store_core").mkdir(parents=True, exist_ok=True)
        (self.root / "tests").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        """Clean up temporary directory."""
        self.temp_dir.cleanup()

    def _create_approval_file(self, gate: str, mode: str) -> None:
        """Create an approval file (G1-G5) with specified mode."""
        content = f"""# {gate} approval record

- Mode: **{mode}**
- Approval date: 2026-09-22
"""
        (self.root / "docs" / "implementation" / "approvals" / f"{gate}.md").write_text(content, encoding="utf-8")

    def _create_audit_files(self) -> None:
        """Create minimal audit-required files."""
        # development-progress.json (minimal)
        progress = {
            "items": [
                {"id": "P2-10", "status": "completed", "evidence": "docs/implementation/p2-10-demo-discovery-l4.md"}
            ]
        }
        (self.root / "docs" / "implementation" / "development-progress.json").write_text(
            json.dumps(progress, indent=2), encoding="utf-8"
        )

        # p2-10-demo-discovery-l4.md with acceptance table
        (self.root / "docs" / "implementation" / "p2-10-demo-discovery-l4.md").write_text(
            "# P2-10\n\n| ID |\n|---|\n| P2-10 |\n", encoding="utf-8"
        )

        # Create test file that includes P2-10
        (self.root / "tests" / "test_audit.py").write_text(
            "# Test for P2-10\nclass P2_10Test: pass\n", encoding="utf-8"
        )

    def test_all_gates_demo_creates_demo_approved_record(self) -> None:
        """All DEMO gates + evidence present → demo_approved=True, live_approved=False."""
        # Setup: all DEMO gates
        for gate in ("G1", "G2", "G3", "G4", "G5"):
            self._create_approval_file(gate, "DEMO")

        # Setup: audit ready
        self._create_audit_files()
        (self.root / "docs" / "implementation" / "local-restart-operations.md").write_text("# Restart")

        review = run_m33_release_review(self.root)

        self.assertTrue(review.demo_approved, f"DEMO should be approved with all evidence: {review.operations_evidence}")
        self.assertFalse(review.live_approved, "LIVE should never be approved while gates are DEMO/missing")
        self.assertTrue(review.operations, "Operations check should pass with audit ready")
        self.assertTrue(review.security, "Security check should pass with all approval files")
        self.assertTrue(review.recovery, "Recovery check should pass with restart ops file")

    def test_missing_approval_file_blocks_security_check(self) -> None:
        """Missing one approval file → security=False, demo_approved=False."""
        # Setup: only 4 approval files
        for gate in ("G1", "G2", "G3", "G4"):
            self._create_approval_file(gate, "DEMO")
        # G5 is missing

        self._create_audit_files()
        (self.root / "docs" / "implementation" / "local-restart-operations.md").write_text("# Restart")

        review = run_m33_release_review(self.root)

        self.assertFalse(review.demo_approved, "Demo should not be approved with missing approval file")
        self.assertFalse(review.security, "Security check should fail with missing approval files")
        self.assertIn("approval_files=4/5", review.security_evidence)

    def test_missing_restart_operations_blocks_recovery_check(self) -> None:
        """Missing local-restart-operations.md → recovery=False, demo_approved=False."""
        # Setup: all DEMO gates and audit ready
        for gate in ("G1", "G2", "G3", "G4", "G5"):
            self._create_approval_file(gate, "DEMO")
        self._create_audit_files()
        # local-restart-operations.md is NOT created

        review = run_m33_release_review(self.root)

        self.assertFalse(review.demo_approved, "Demo should not be approved without restart operations file")
        self.assertFalse(review.recovery, "Recovery check should fail without restart operations file")
        self.assertIn("restart_ops_exists=False", review.recovery_evidence)

    def test_blocked_gates_enumerated_correctly(self) -> None:
        """Non-LIVE gates (DEMO, missing) → blocked_gates contains them."""
        # Setup: mixed gate states
        self._create_approval_file("G1", "DEMO")
        self._create_approval_file("G2", "DEMO")
        # G3, G4 missing
        self._create_approval_file("G5", "DEMO")

        self._create_audit_files()
        (self.root / "docs" / "implementation" / "local-restart-operations.md").write_text("# Restart")

        review = run_m33_release_review(self.root)

        # blocked_gates should include ALL non-LIVE gates (DEMO and missing)
        self.assertIn("G1", review.blocked_gates, "DEMO gate G1 should be in blocked_gates")
        self.assertIn("G2", review.blocked_gates, "DEMO gate G2 should be in blocked_gates")
        self.assertIn("G3", review.blocked_gates, "Missing gate G3 should be in blocked_gates")
        self.assertIn("G4", review.blocked_gates, "Missing gate G4 should be in blocked_gates")
        self.assertIn("G5", review.blocked_gates, "DEMO gate G5 should be in blocked_gates")

    def test_audit_failure_blocks_operations_check(self) -> None:
        """Broken audit (missing progress.json) → operations=False, demo_approved=False."""
        # Setup: all DEMO gates, but no audit files
        for gate in ("G1", "G2", "G3", "G4", "G5"):
            self._create_approval_file(gate, "DEMO")
        (self.root / "docs" / "implementation" / "local-restart-operations.md").write_text("# Restart")
        # development-progress.json is NOT created

        review = run_m33_release_review(self.root)

        self.assertFalse(review.demo_approved, "Demo should not be approved with audit failure")
        self.assertFalse(review.operations, "Operations check should fail without audit passing")

    def test_release_review_is_immutable_with_digest(self) -> None:
        """M33ReleaseReview is frozen and has valid sha256 digest."""
        for gate in ("G1", "G2", "G3", "G4", "G5"):
            self._create_approval_file(gate, "DEMO")
        self._create_audit_files()
        (self.root / "docs" / "implementation" / "local-restart-operations.md").write_text("# Restart")

        review = run_m33_release_review(self.root)

        # Should be frozen (immutable)
        with self.assertRaises(Exception):
            review.demo_approved = False  # type: ignore

        # Digest should be 64-char hex string
        self.assertEqual(len(review.digest), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in review.digest))

        # Digest should be deterministic
        review2 = run_m33_release_review(self.root)
        self.assertEqual(review.digest, review2.digest)

    def test_actual_repository_shows_demo_approval_live_nogo(self) -> None:
        """Actual test repo: DEMO approval, LIVE no-go, blocked_gates=G1..G5."""
        # Use actual repository structure (real worktree root)
        # This validates against the actual repository
        import sys
        from pathlib import Path as PathlibPath

        # Find actual repo root (navigate up from this test file)
        test_file = PathlibPath(__file__)
        repo_root = test_file.parent.parent.parent  # ../../../ from test file

        if (repo_root / "docs" / "implementation" / "approvals" / "G1.md").exists():
            # We're in the real repo
            review = run_m33_release_review(repo_root)

            # Should show DEMO approval
            self.assertTrue(review.demo_approved, "Real repo should have DEMO approval")

            # Should show LIVE no-go (all gates are DEMO)
            self.assertFalse(review.live_approved, "Real repo should show LIVE no-go")

            # blocked_gates should be G1-G5 (all DEMO)
            self.assertEqual(
                set(review.blocked_gates), {"G1", "G2", "G3", "G4", "G5"},
                "All DEMO gates should be in blocked_gates"
            )


if __name__ == "__main__":
    unittest.main()
