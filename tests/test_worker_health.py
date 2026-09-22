import unittest
from datetime import datetime, timedelta, timezone

from smart_store_control.pm import _health


class WorkerHealthTests(unittest.TestCase):
    def test_recent_heartbeat_is_healthy(self):
        stamp = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
        self.assertEqual("healthy", _health({"status": "in_progress", "heartbeat_at": stamp}))

    def test_old_heartbeat_is_stale(self):
        stamp = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
        self.assertEqual("stale", _health({"status": "in_progress", "heartbeat_at": stamp}))

    def test_blocked_task_is_error(self):
        self.assertEqual("error", _health({"status": "blocked"}))


if __name__ == "__main__":
    unittest.main()
