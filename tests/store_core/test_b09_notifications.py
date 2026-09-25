from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from packages.store_core import ConflictError, SQLiteRepository, StoreControlPlane


class B09NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.repo = SQLiteRepository(Path(self.temp.name) / "notify.sqlite3")
        self.app = StoreControlPlane(self.repo); self.ctx = self.app.bootstrap_tenant("Notify", "notify@example.test")

    def tearDown(self): self.repo.close(); self.temp.cleanup()

    def test_fallback_is_local_and_idempotent(self):  # B09-01, B09-02
        self.app.set_demo_notification_preference(self.ctx, "incident-1", ("app_push", "email", "chatgpt"))
        result = self.app.notify_demo(self.ctx, "incident-1", {"message": "degraded"}, "notice-1", ("app_push", "email"))
        self.assertEqual("DELIVERED", result["state"]); self.assertEqual("chatgpt", result["channel"])
        self.assertEqual(3, len(self.app.notification_deliveries(self.ctx)))
        replay = self.app.notify_demo(self.ctx, "incident-1", {"message": "degraded"}, "notice-1", ("app_push", "email"))
        self.assertTrue(replay["replayed"])

    def test_mute_ack_and_invalid_inputs(self):  # B09-02
        self.app.set_demo_notification_preference(self.ctx, "incident-2", ("app_push", "email"), True)
        result = self.app.notify_demo(self.ctx, "incident-2", {"message": "muted"}, "notice-2")
        self.assertEqual("MUTED", result["state"])
        ack, replay = self.app.acknowledge_demo_incident(self.ctx, "incident-2", "reviewed locally", "ack-1")
        self.assertFalse(replay); same, replay = self.app.acknowledge_demo_incident(self.ctx, "incident-2", "reviewed locally", "ack-1")
        self.assertTrue(replay); self.assertEqual(ack.id, same.id)
        with self.assertRaises(ConflictError): self.app.set_demo_notification_preference(self.ctx, "bad", ("sms",))
        with self.assertRaises(ConflictError): self.app.acknowledge_demo_incident(self.ctx, "incident-2", "", "ack-2")

    def test_m54_urgent_sends_immediately_to_app_push(self):  # M5.4-01
        result = self.app.notify_urgent_demo(self.ctx, "urgent-1", "account_suspension_security",
                                             {"reason": "suspicious activity"}, "urgent-notice-1")
        self.assertEqual("DELIVERED", result["state"])
        self.assertEqual("app_push", result["channel"])
        self.assertEqual(1, len(result["deliveries"]))
        self.assertFalse(result["replayed"])

    def test_m54_urgent_escalates_after_5min_if_unacknowledged(self):  # M5.4-02
        now = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        self.app._clock = lambda: now

        result = self.app.notify_urgent_demo(self.ctx, "urgent-2", "mass_stockout",
                                             {"sku": "SKU123", "remaining": 0}, "urgent-notice-2")
        self.assertEqual("app_push", result["channel"])

        deliveries = list(self.app.notification_deliveries(self.ctx))
        self.assertEqual(1, len(deliveries))
        self.assertEqual("app_push", deliveries[0].channel)

        later = now + timedelta(minutes=6)
        self.app._clock = lambda: later
        escalation = self.app.check_and_escalate_incidents(self.ctx.tenant_id, later)
        self.assertEqual(1, escalation["escalated_count"])

        deliveries = list(self.app.notification_deliveries(self.ctx))
        self.assertEqual(2, len(deliveries))
        email_delivery = [d for d in deliveries if d.channel == "email"][0]
        self.assertEqual("DELIVERED", email_delivery.state)
        self.assertEqual(2, email_delivery.attempt)

    def test_m54_escalation_blocked_if_acknowledged_before_5min(self):  # M5.4-03
        now = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        self.app._clock = lambda: now

        result = self.app.notify_urgent_demo(self.ctx, "urgent-3", "po_deadline",
                                             {"supplier": "S1", "deadline": "2026-01-16"}, "urgent-notice-3")
        self.assertEqual("app_push", result["channel"])

        ack, _ = self.app.acknowledge_demo_incident(self.ctx, "urgent-3", "reviewed", "ack-3")
        self.assertIsNotNone(ack)

        later = now + timedelta(minutes=6)
        self.app._clock = lambda: later
        escalation = self.app.check_and_escalate_incidents(self.ctx.tenant_id, later)
        self.assertEqual(0, escalation["escalated_count"])

        deliveries = list(self.app.notification_deliveries(self.ctx))
        self.assertEqual(1, len(deliveries))
        self.assertEqual("app_push", deliveries[0].channel)

    def test_m54_invalid_category_rejected(self):  # M5.4-04
        with self.assertRaises(ConflictError):
            self.app.notify_urgent_demo(self.ctx, "urgent-4", "invalid_category",
                                        {"message": "test"}, "urgent-notice-4")

    def test_m54_dedup_by_incident_key(self):  # M5.4-05
        result1 = self.app.notify_urgent_demo(self.ctx, "urgent-5", "emergency_stop",
                                              {"reason": "manual"}, "urgent-notice-5a")
        result2 = self.app.notify_urgent_demo(self.ctx, "urgent-5", "emergency_stop",
                                              {"reason": "manual"}, "urgent-notice-5b")

        self.assertTrue(result2["replayed"])
        deliveries = list(self.app.notification_deliveries(self.ctx))
        app_push_count = sum(1 for d in deliveries if d.channel == "app_push" and d.notification_key == "urgent-5")
        self.assertEqual(1, app_push_count)


if __name__ == "__main__": unittest.main()
