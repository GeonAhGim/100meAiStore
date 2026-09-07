from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from packages.store_core import (ApprovalKind, AuthorizationError, ConflictError,
                                 Role, SQLiteRepository, StoreControlPlane, browser_session_cookie)


class BrowserAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "auth.sqlite3"
        self.now = datetime(2026, 9, 8, tzinfo=timezone.utc)
        self.repo = SQLiteRepository(self.path)
        self.app = StoreControlPlane(self.repo, lambda: self.now)
        self.master = self.app.bootstrap_tenant("Browser auth", "master-auth@example.test")
        self.catalog = self.app.add_member(self.master, "catalog-auth@example.test", [Role.CATALOG_CS])

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def session(self, context=None, assertion="fixture-email-mfa"):
        return self.app.issue_browser_session(
            context or self.master, identity_assertion_ref=assertion).token

    def approval(self, key="browser-product"):
        return self.app.request_approval(
            self.master, ApprovalKind.PRODUCT, key, {"sku": key}, key, 1, 1)[1]

    def test_session_is_hash_only_durable_and_revocation_invalidates_it(self):
        issued = self.app.issue_browser_session(
            self.catalog, identity_assertion_ref="fixture-email-mfa")
        token = issued.token
        self.assertNotIn(token, repr(issued))
        cookie = browser_session_cookie(token, max_age_seconds=28_800)
        for attribute in ("HttpOnly", "SameSite=Strict", "Secure", "Path=/"):
            self.assertIn(attribute, cookie)
        self.assertNotIn(token, self.path.read_bytes().decode("utf-8", errors="ignore"))
        self.repo.close()
        self.repo = SQLiteRepository(self.path)
        self.app = StoreControlPlane(self.repo, lambda: self.now)
        context = self.app.authenticate_browser_session(token)
        self.assertEqual(self.catalog, context)
        self.app.revoke_member(self.master, self.catalog.user_id)
        with self.assertRaises(AuthorizationError):
            self.app.authenticate_browser_session(token)

    def test_session_expiry_and_malformed_tokens_fail_closed(self):
        token = self.session()
        self.now += timedelta(hours=8)
        for candidate in (token, "", "raw-id", "x" * 200):
            with self.assertRaises(AuthorizationError):
                self.app.authenticate_browser_session(candidate)

    def test_nonce_is_session_approval_command_bound_single_use_and_durable(self):
        approval = self.approval()
        session = self.session(self.catalog, "fixture-email-mfa-1")
        other_session = self.session(self.catalog, "fixture-email-mfa-2")
        issued = self.app.issue_approval_confirmation_nonce(session, approval.id)
        nonce = issued.token
        self.assertNotIn(nonce, repr(issued))
        with self.assertRaises(ConflictError):
            self.app.decide_approval_authenticated(other_session, approval.id, True, "wrong session", nonce)
        self.repo.close()
        self.repo = SQLiteRepository(self.path)
        self.app = StoreControlPlane(self.repo, lambda: self.now)
        decided = self.app.decide_approval_authenticated(session, approval.id, True, "reviewed", nonce)
        self.assertEqual("approved", decided.state.value)
        with self.assertRaises(ConflictError):
            self.app.decide_approval_authenticated(session, approval.id, True, "replay", nonce)

    def test_arbitrary_cross_approval_and_expired_nonce_do_not_mutate_approvals(self):
        first, second = self.approval("first"), self.approval("second")
        session = self.session(self.catalog)
        nonce = self.app.issue_approval_confirmation_nonce(session, first.id, timedelta(seconds=30)).token
        for approval_id, candidate in ((second.id, nonce), (first.id, "arbitrary-nonce-value-that-is-long-enough")):
            with self.assertRaises(ConflictError):
                self.app.decide_approval_authenticated(session, approval_id, True, "blocked", candidate)
        self.now += timedelta(seconds=30)
        with self.assertRaises(ConflictError):
            self.app.decide_approval_authenticated(session, first.id, True, "expired", nonce)
        self.assertEqual("pending", self.repo.get_approval(self.master.tenant_id, first.id).state.value)
        self.assertEqual("pending", self.repo.get_approval(self.master.tenant_id, second.id).state.value)


if __name__ == "__main__":
    unittest.main()
