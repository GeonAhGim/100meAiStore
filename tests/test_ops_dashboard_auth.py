import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from packages.store_core import ApprovalKind, SQLiteRepository, StoreControlPlane
from smart_store_aios.dashboard import DashboardServer, INDEX_HTML


class OpsDashboardAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "dashboard.sqlite3"
        self.repo = SQLiteRepository(self.path)
        self.app = StoreControlPlane(self.repo)
        self.context = self.app.bootstrap_tenant("Authenticated", "dashboard-auth@example.test")
        self.other = self.app.bootstrap_tenant("Other", "other-dashboard-auth@example.test")
        self.session = self.app.issue_browser_session(
            self.context, identity_assertion_ref="fixture-email-mfa").token
        self.server = DashboardServer(("127.0.0.1", 0), self.path, self.temp.name)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = f"127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.repo.close()
        self.temp.cleanup()

    def request(self, method, path, body=None, *, cookie=True, origin=True):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        headers = {}
        if cookie:
            headers["Cookie"] = f"store_session={self.session}"
        if origin:
            headers["Origin"] = f"http://{self.host}"
        if body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(body)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, dict(response.getheaders()), payload

    def test_dashboard_rejects_anonymous_and_client_identity_override(self):
        status, _, _ = self.request("GET", "/api/dashboard", cookie=False)
        self.assertEqual(401, status)
        status, _, _ = self.request(
            "GET", f"/api/dashboard?tenant_id={self.other.tenant_id}&user_id={self.other.user_id}")
        self.assertEqual(400, status)
        status, headers, payload = self.request("GET", "/api/dashboard")
        self.assertEqual(200, status)
        self.assertEqual("no-store", headers["Cache-Control"])
        value = json.loads(payload)
        self.assertEqual(self.context.tenant_id, value["tenant_id"])
        self.assertNotIn(self.other.tenant_id, payload.decode())

    def test_same_origin_nonce_and_decision_roundtrip_reject_replay(self):
        _, approval = self.app.request_approval(
            self.context, ApprovalKind.PRODUCT, "browser-product", {"sku": "fixture"},
            "browser-product", 1, 1)
        path = f"/api/approvals/{approval.id}"
        status, _, payload = self.request("POST", path + "/nonce", {})
        self.assertEqual(201, status)
        nonce = json.loads(payload)["nonce"]
        decision = {"approve": True, "reason": "browser reviewed", "nonce": nonce}
        status, _, payload = self.request("POST", path + "/decision", decision)
        self.assertEqual(200, status)
        self.assertEqual("approved", json.loads(payload)["state"])
        status, _, _ = self.request("POST", path + "/decision", decision)
        self.assertEqual(401, status)
        status, _, _ = self.request("POST", path + "/nonce", {}, origin=False)
        self.assertEqual(403, status)

    def test_browser_shell_contains_no_raw_identity_or_token_storage(self):
        self.assertNotIn("localStorage", INDEX_HTML)
        self.assertNotIn("tenant_id", INDEX_HTML)
        self.assertNotIn("user_id", INDEX_HTML)
        self.assertNotIn("store_session", INDEX_HTML)


if __name__ == "__main__":
    unittest.main()
