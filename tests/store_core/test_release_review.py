from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from packages.store_core.channel_order_contracts import ContractQuarantine
from packages.store_core.release_review import masked_fixture_metadata, plan_fixture_retention


class ReleaseReviewTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 6, tzinfo=timezone.utc)
        self.row = {"record_ref": "fixture1", "data_class": "finance", "created_at": "2026-09-01T00:00:00Z",
                    "legal_hold": False, "private_payload": "SYNTHETIC PRIVATE ACCOUNT"}
        self.policy = {"finance": {"retention_days": 5, "fixture_approval_digest": "a" * 64}}

    def test_missing_or_unapproved_policy_never_proposes_deletion(self):
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


if __name__ == "__main__":
    unittest.main()
