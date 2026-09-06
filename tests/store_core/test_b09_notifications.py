from pathlib import Path
import tempfile
import unittest

from packages.store_core import ConflictError, SQLiteRepository, StoreControlPlane


class B09NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.repo = SQLiteRepository(Path(self.temp.name) / "notify.sqlite3")
        self.app = StoreControlPlane(self.repo); self.ctx = self.app.bootstrap_tenant("Notify", "notify@example.test")

    def tearDown(self): self.repo.close(); self.temp.cleanup()

    def test_fallback_is_local_and_idempotent(self):
        self.app.set_demo_notification_preference(self.ctx, "incident-1", ("app_push", "email", "chatgpt"))
        result = self.app.notify_demo(self.ctx, "incident-1", {"message": "degraded"}, "notice-1", ("app_push", "email"))
        self.assertEqual("DELIVERED", result["state"]); self.assertEqual("chatgpt", result["channel"])
        self.assertEqual(3, len(self.app.notification_deliveries(self.ctx)))
        replay = self.app.notify_demo(self.ctx, "incident-1", {"message": "degraded"}, "notice-1", ("app_push", "email"))
        self.assertTrue(replay["replayed"])

    def test_mute_ack_and_invalid_inputs(self):
        self.app.set_demo_notification_preference(self.ctx, "incident-2", ("app_push", "email"), True)
        result = self.app.notify_demo(self.ctx, "incident-2", {"message": "muted"}, "notice-2")
        self.assertEqual("MUTED", result["state"])
        ack, replay = self.app.acknowledge_demo_incident(self.ctx, "incident-2", "reviewed locally", "ack-1")
        self.assertFalse(replay); same, replay = self.app.acknowledge_demo_incident(self.ctx, "incident-2", "reviewed locally", "ack-1")
        self.assertTrue(replay); self.assertEqual(ack.id, same.id)
        with self.assertRaises(ConflictError): self.app.set_demo_notification_preference(self.ctx, "bad", ("sms",))
        with self.assertRaises(ConflictError): self.app.acknowledge_demo_incident(self.ctx, "incident-2", "", "ack-2")


if __name__ == "__main__": unittest.main()
