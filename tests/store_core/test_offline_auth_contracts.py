import base64
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from packages.store_core.channel_contracts import coupang_day_page_plan
from packages.store_core.channel_order_contracts import ContractQuarantine
from packages.store_core.offline_auth_contracts import (
    coupang_fixture_signature, naver_public_fixture_signature, naver_canonical_fixture_signature,
    offline_auth_source_boundary, plan_naver_fixture_retry,
)


class OfflineAuthContractTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 6, 10, 0, tzinfo=timezone(timedelta(hours=9)))
        self.plan = coupang_day_page_plan(tenant_ref="fixture", connection_ref="fixture",
                                          vendor_id="fixture", start=date(2026, 9, 1),
                                          end=date(2026, 9, 1), status="INSTRUCT", next_token="x+/=")
        self.args = dict(method="GET", http_status=429, body={"code": "GW.RATE_LIMIT"},
                         headers={"GNCP-GW-RateLimit-Replenish-Rate": "2", "GNCP-GW-RateLimit-Remaining": "0"},
                         attempts_used=1, fixture_refresh_used=False, now=self.now,
                         deadline=self.now + timedelta(seconds=30))

    def test_naver_public_vector_input_and_base64_contract(self):
        expected = "JDJhJDEwJGFiY2RlZmdoaWprbG1ub3BxcnN0dXVCVldZSk42T0VPdEx1OFY0cDQxa2IuTnpVaUEzbmsy"
        def fixture_hash(password, salt):
            self.assertEqual(b"aaaabbbbcccc_1643961623299", password)
            self.assertEqual(b"$2a$10$abcdefghijklmnopqrstuv", salt)
            return base64.b64decode(expected)
        result = naver_public_fixture_signature(fixture_hash)
        self.assertEqual(expected, result.digest)
        self.assertFalse(result.usable_for_live_auth)
        self.assertNotIn(expected, repr(result))
        with self.assertRaises(ContractQuarantine): naver_public_fixture_signature(lambda *_: b"invalid")
        def unsupported_hash(*_):
            raise ValueError("SYNTHETIC PRIVATE BACKEND MESSAGE")
        with self.assertRaisesRegex(ContractQuarantine, "^public_fixture_bcrypt_incompatible$"):
            naver_public_fixture_signature(unsupported_hash)

    def test_coupang_utc_and_exact_encoded_query_are_deterministic(self):
        with patch("socket.socket", side_effect=AssertionError("network prohibited")):
            local = coupang_fixture_signature(self.plan, at=self.now)
            utc = coupang_fixture_signature(self.plan, at=self.now.astimezone(timezone.utc))
        self.assertEqual(local, utc)
        self.assertEqual("260906T010000Z", local.signed_at)
        self.assertEqual(64, len(local.digest))
        self.assertNotIn(local.digest, repr(local))
        changed_plan = coupang_day_page_plan(tenant_ref="fixture", connection_ref="fixture",
                                              vendor_id="fixture", start=date(2026, 9, 1),
                                              end=date(2026, 9, 1), status="INSTRUCT", next_token="x / =")
        self.assertNotEqual(local.digest, coupang_fixture_signature(changed_plan, at=self.now).digest)
        with self.assertRaises(ContractQuarantine): coupang_fixture_signature(self.plan, at=datetime(2026, 9, 6))

    def test_canonical_synthetic_bcrypt_vector_is_separate_from_public_sample(self):
        expected = "JDJiJDA0JGFiY2RlZmdoaWprbG1ub3BxcnN0dXVyRFhyaU5lRWFjUXg2eU9ZYk50RVIxZHlrbWtyblJL"
        def fixture_hash(password, salt):
            self.assertEqual(b"offline-fixture_1643961623299", password)
            self.assertEqual(b"$2b$04$abcdefghijklmnopqrstuu", salt)
            return base64.b64decode(expected)
        self.assertEqual(expected, naver_canonical_fixture_signature(fixture_hash).digest)

    def test_rate_retry_budget_and_deadline(self):
        first = plan_naver_fixture_retry(**self.args)
        self.assertEqual(("RETRY_FIXTURE_AFTER_DELAY", 2, 1), (first.action, first.next_attempt, first.delay_seconds))
        self.assertFalse(first.executes_network)
        for overrides in ({"attempts_used": 3}, {"method": "POST"}, {"deadline": self.now},
                          {"deadline": self.now + timedelta(seconds=1)}):
            self.assertEqual("STOP", plan_naver_fixture_retry(**{**self.args, **overrides}).action)

    def test_refresh_once_and_mismatched_status_fail_closed(self):
        args = {**self.args, "http_status": 401, "body": {"code": "GW.AUTHN"}}
        self.assertEqual("REFRESH_FIXTURE_ONCE", plan_naver_fixture_retry(**args).action)
        self.assertEqual("STOP", plan_naver_fixture_retry(**{**args, "fixture_refresh_used": True}).action)
        self.assertEqual("MANUAL_REVIEW", plan_naver_fixture_retry(**{**args, "http_status": 403}).action)

    def test_quota_round_and_missing_or_malformed_headers_require_review(self):
        quota = {"GNCP-GW-Quota-Period": "ROUND", "GNCP-GW-Quota-Limit": "10", "GNCP-GW-Quota-Remaining": "0"}
        args = {**self.args, "body": {"code": "GW.QUOTA_LIMIT"}, "headers": quota}
        self.assertEqual("MANUAL_REVIEW", plan_naver_fixture_retry(**args).action)
        quota["GNCP-GW-Quota-Period"] = "SECONDS"
        self.assertEqual("RETRY_FIXTURE_AFTER_DELAY", plan_naver_fixture_retry(**args).action)
        for headers in ({}, {"GNCP-GW-RateLimit-Replenish-Rate": "nan"},
                        {**self.args["headers"], "gncp-gw-ratelimit-remaining": "9"}):
            self.assertEqual("MANUAL_REVIEW", plan_naver_fixture_retry(**{**self.args, "headers": headers}).action)

    def test_raw_header_values_and_messages_are_not_diagnostics(self):
        result = plan_naver_fixture_retry(**{**self.args, "headers": {"Authorization": "SYNTHETIC PRIVATE"},
                                           "body": {"code": "OTHER", "message": "SYNTHETIC PRIVATE"}})
        self.assertNotIn("PRIVATE", repr(result))
        for overrides in ({"attempts_used": True}, {"max_attempts": 100}, {"now": datetime(2026, 9, 6)}):
            with self.assertRaises(ContractQuarantine): plan_naver_fixture_retry(**{**self.args, **overrides})

    def test_unresolved_official_examples_explicitly_block_live_transport(self):
        boundary = offline_auth_source_boundary()
        self.assertFalse(boundary.live_transport_authorized)
        self.assertFalse(boundary.naver_public_bcrypt_compatible)
        self.assertIsNone(boundary.coupang_requested_by_header)
        self.assertEqual(
            ("naver_public_fixture_bcrypt_incompatible", "coupang_requested_by_header_unresolved"),
            boundary.unresolved_reasons,
        )


if __name__ == "__main__":
    unittest.main()
