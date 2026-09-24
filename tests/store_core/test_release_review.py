from datetime import datetime, timedelta, timezone
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packages.store_core.channel_order_contracts import ContractQuarantine
from packages.store_core.release_review import check_no_secrets, masked_fixture_metadata, plan_fixture_retention


class ReleaseReviewTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 6, tzinfo=timezone.utc)
        self.row = {"record_ref": "fixture1", "data_class": "finance", "created_at": "2026-09-01T00:00:00Z",
                    "legal_hold": False, "private_payload": "SYNTHETIC PRIVATE ACCOUNT"}
        self.policy = {"finance": {"retention_days": 5, "fixture_approval_digest": "a" * 64}}

    def test_missing_or_unapproved_policy_never_proposes_deletion(self):
        # P2-07-01: Data flow inventory documents intended consumers, sensitive values, and retention decisions
        # P2-07-02: Dry-run deletion never invokes filesystem/database deletion; missing/invalid policy returns REVIEW_POLICY; legal hold always returns RETAIN
        # P2-07-03: Masked export contains only allowlisted metadata; deletion_authorized is always false
        for policies in ({}, {"finance": {"retention_days": 1}},
                         {"finance": {"retention_days": True, "fixture_approval_digest": "a" * 64}},
                         {"finance": {"retention_days": 1, "fixture_approval_digest": ""}}):
            result = plan_fixture_retention([self.row], policies, as_of=self.now)[0]
            self.assertEqual("REVIEW_POLICY", result.action)
            self.assertFalse(result.deletion_authorized)

    def test_legal_hold_overrides_age_and_missing_policy(self):
        held = {**self.row, "legal_hold": True}
        for policies in ({}, self.policy):
            result = plan_fixture_retention([held], policies, as_of=self.now)[0]
            self.assertEqual(("RETAIN", "legal_hold", False), (result.action, result.reason, result.deletion_authorized))

    def test_boundary_is_review_only_and_calls_no_delete_or_network(self):
        with patch("os.remove", side_effect=AssertionError("deletion prohibited")), \
             patch("shutil.rmtree", side_effect=AssertionError("deletion prohibited")), \
             patch("socket.socket", side_effect=AssertionError("network prohibited")):
            before = plan_fixture_retention([self.row], self.policy, as_of=self.now - timedelta(seconds=1))[0]
            boundary = plan_fixture_retention([self.row], self.policy, as_of=self.now)[0]
        self.assertEqual("RETAIN", before.action)
        self.assertEqual("ELIGIBLE_FOR_REVIEW", boundary.action)
        self.assertFalse(boundary.deletion_authorized)

    def test_masked_export_drops_unrecognized_sensitive_fields(self):
        result = masked_fixture_metadata([self.row])
        self.assertNotIn("PRIVATE", repr(result))
        self.assertNotIn("private_payload", result[0])
        self.assertIn("private_payload", self.row)

    def test_unknown_class_future_naive_duplicate_and_invalid_hold_fail_closed(self):
        for change in ({"data_class": "unknown"}, {"created_at": "2027-01-01T00:00:00Z"},
                       {"created_at": "2026-09-01T00:00:00"}, {"legal_hold": "false"}):
            with self.assertRaises(ContractQuarantine):
                plan_fixture_retention([{**self.row, **change}], self.policy, as_of=self.now)
        with self.assertRaises(ContractQuarantine): masked_fixture_metadata([self.row, self.row])


class CheckNoSecretsTest(unittest.TestCase):
    """Test check_no_secrets: content-based detection, git tracking, .gitignore handling."""

    def setUp(self):
        """Create temporary directory structure for testing."""
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp_dir.name)
        (self.root / "packages").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        """Clean up temporary directory."""
        self.temp_dir.cleanup()

    def test_config_json_without_secrets_passes(self):
        """config.json with no secrets should pass (not just fail on filename)."""
        config = {"debug": True, "port": 8000, "name": "demo"}
        (self.root / "config.json").write_text(json.dumps(config))

        passed, evidence = check_no_secrets(self.root)
        self.assertTrue(passed, f"config.json without secrets should pass: {evidence}")

    def test_actual_secret_pattern_fails(self):
        """File with actual credential pattern should fail."""
        py_content = 'api_key = "sk_live_' + 'a' * 40 + '"'
        (self.root / "packages" / "config.py").write_text(py_content)

        passed, evidence = check_no_secrets(self.root)
        self.assertFalse(passed, "Should fail when credential pattern found in tracked file")
        self.assertIn("credential patterns", evidence)

    def test_demo_fixture_with_credentials_passes(self):
        """DEMO fixture files with credential patterns should be skipped."""
        fixture_content = """DEMO fixture data
password = "test_password_123"
api_key = "fake_key_for_testing"
"""
        (self.root / "packages" / "fixtures.py").write_text(fixture_content)

        passed, evidence = check_no_secrets(self.root)
        self.assertTrue(passed, "DEMO fixture with credential patterns should be skipped")

    def test_private_key_header_fails(self):
        """File with private key header should fail."""
        key_content = """-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEA1234567890...
-----END RSA PRIVATE KEY-----"""
        (self.root / "packages" / "key.pem").write_text(key_content)

        passed, evidence = check_no_secrets(self.root)
        self.assertFalse(passed, "Should fail when private key header found")

    def test_no_secrets_found_returns_scanned_count(self):
        """Passing check should report scanned file count."""
        (self.root / "packages" / "app.py").write_text("def main(): pass")
        (self.root / "packages" / "utils.py").write_text("def helper(): pass")

        passed, evidence = check_no_secrets(self.root)
        self.assertTrue(passed)
        self.assertIn("scanned", evidence)
        self.assertIn("2", evidence)


if __name__ == "__main__":
    unittest.main()
