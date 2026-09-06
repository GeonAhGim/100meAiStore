import json
import unittest
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

from packages.store_core.channel_contracts import (
    COUPANG_STATES, classify_naver_read_error, coupang_day_page_plan,
)


class OfflineChannelContractTest(unittest.TestCase):
    def setUp(self):
        self.fixtures = json.loads((Path(__file__).parents[1] / "fixtures" /
                                    "channel_contracts.json").read_text(encoding="utf-8"))
        self.args = self.fixtures["coupang_kr_page"].copy()
        self.args["start"] = date.fromisoformat(self.args["start"])
        self.args["end"] = date.fromisoformat(self.args["end"])

    def test_encoded_page_cursor_roundtrip_and_fixed_offline_scope(self):
        with patch("socket.socket", side_effect=AssertionError("network prohibited")):
            plan = coupang_day_page_plan(**self.args)
        parsed = parse_qs(plan.encoded_query)
        self.assertEqual(["2026-08-01+09:00"], parsed["createdAtFrom"])
        self.assertIn("%2B09%3A00", plan.encoded_query)
        self.assertEqual([self.args["next_token"]], parsed["nextToken"])
        self.assertEqual("/v2/providers/openapi/apis/api/v5/vendors/fixture-vendor/ordersheets", plan.path)
        self.assertEqual(("GET", "OFFLINE_CONTRACT", False),
                         (plan.method, plan.mode, plan.live_authorized))
        self.assertNotIn(self.args["next_token"], repr(plan))
        self.assertNotIn("fixture-tenant", repr(plan))
        self.assertNotIn("headers", asdict(plan))

    def test_first_page_and_all_known_states(self):
        for status in COUPANG_STATES:
            plan = coupang_day_page_plan(**{**self.args, "status": status, "next_token": None})
            self.assertNotIn("nextToken", dict(plan.query))
            self.assertNotIn("searchType", dict(plan.query))

    def test_window_page_state_and_reference_boundaries_fail_closed(self):
        changes = [
            {"end": date(2026, 9, 1)}, {"end": date(2026, 7, 31)},
            {"start": datetime(2026, 8, 1)}, {"max_per_page": 51},
            {"max_per_page": 0}, {"max_per_page": True}, {"status": "UNKNOWN"},
            {"vendor_id": "../other"}, {"tenant_ref": ""},
            {"connection_ref": "other/connection"}, {"next_token": ""},
            {"next_token": "cursor\r\nInjected"}, {"next_token": "x" * 4097},
            {"next_token": 123},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                coupang_day_page_plan(**{**self.args, **change})
        plan = coupang_day_page_plan(**{**self.args, "end": self.args["start"], "max_per_page": 1})
        self.assertEqual("1", dict(plan.query)["maxPerPage"])

    def test_documented_error_pairs_do_not_blindly_retry_or_expose_body(self):
        for fixture in self.fixtures["naver_errors"]:
            body = {**fixture["body"], "message": "sensitive-fixture-message"}
            with patch("socket.socket", side_effect=AssertionError("network prohibited")):
                decision = classify_naver_read_error(fixture["http_status"], body)
            self.assertEqual(fixture["expected"], decision.action)
            self.assertFalse(decision.automatic_retry)
            self.assertNotIn("sensitive-fixture-message", repr(decision))

    def test_malformed_and_mismatched_errors_require_review(self):
        for body in (None, [], "message", {"code": []}, {"code": "GW.RATE_LIMIT"}):
            self.assertEqual("MANUAL_REVIEW", classify_naver_read_error(401, body).action)
        for status in (True, 200, 600, "401"):
            with self.assertRaises(ValueError):
                classify_naver_read_error(status, {"code": "GW.AUTHN"})


if __name__ == "__main__":
    unittest.main()
