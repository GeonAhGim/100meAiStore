from datetime import datetime, timezone
from pathlib import Path
import tempfile
import threading
import unittest
import sqlite3

from packages.store_core import (
    AdapterCapability, AdapterCapabilityManifest, ConflictError, DemoPage,
    FixtureDemoReadAdapter, SQLiteRepository, StoreControlPlane,
)
from packages.store_core.errors import NotFoundError


class AdapterIngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.temp.name) / "adapter.sqlite3")
        self.app = StoreControlPlane(self.repo)
        self.ctx = self.app.bootstrap_tenant("DEMO", "master@example.test")
        self.app.register_adapter_manifest(self.ctx, AdapterCapabilityManifest(
            self.ctx.tenant_id, "demo", "orders", "demo-v1",
            frozenset({AdapterCapability.ORDERS_READ, AdapterCapability.INBOUND_EVENTS}),
            frozenset({1}), datetime.now(timezone.utc),
        ))

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    @staticmethod
    def row(event="e1", revision=1, total=300):
        return {"external_order_id": "order-1", "event_id": event, "revision": revision,
                "currency": "KRW", "total_minor": total,
                "lines": [{"sku": "sku-1", "quantity": 3, "unit_minor": 100}]}

    def adapter(self, *pages):
        return FixtureDemoReadAdapter(pages, provider="demo", adapter_version="demo-v1")

    def test_ad01_two_pages_are_durable_and_advance_cursor(self):
        first = DemoPage((self.row(),), "p1", True, datetime.now(timezone.utc))
        second = DemoPage((self.row("e2"),), "p2", False, datetime.now(timezone.utc))
        fixture = self.adapter(first, second)
        one = self.app.poll_demo_connection(self.ctx, "demo", "orders", 0, fixture)
        two = self.app.poll_demo_connection(self.ctx, "demo", "orders", 1, fixture)
        self.assertEqual("p1", one.checkpoint.cursor)
        self.assertEqual("p2", two.checkpoint.cursor)
        self.assertEqual(2, len(self.repo.normalized_payloads_for(self.ctx.tenant_id)))
        self.assertEqual(2, len(self.app.inbox_for(self.ctx)))

    def test_ad02_replayed_page_has_stable_receipt_and_no_extra_event(self):
        page = DemoPage((self.row(),), "p1", False, datetime.now(timezone.utc))
        first = self.app.poll_demo_connection(self.ctx, "demo", "orders", 0, self.adapter(page))
        prior_id = first.inbox_ids[0]
        replay = self.app.poll_demo_connection(self.ctx, "demo", "orders", 1, self.adapter(page, page))
        self.assertEqual(1, replay.replayed_count)
        self.assertEqual(prior_id, replay.inbox_ids[0])
        self.assertEqual(1, len(self.app.inbox_for(self.ctx)))

    def test_ad03_malformed_page_rolls_back_payloads_receipts_and_cursor(self):
        malformed = self.row()
        malformed["unexpected"] = True
        page = DemoPage((malformed,), "p1", True, datetime.now(timezone.utc))
        with self.assertRaises(ConflictError):
            self.app.poll_demo_connection(self.ctx, "demo", "orders", 0, self.adapter(page))
        self.assertEqual((), self.repo.normalized_payloads_for(self.ctx.tenant_id))
        self.assertEqual((), self.app.inbox_for(self.ctx))
        self.assertIsNone(self.repo.get_poll_checkpoint(self.ctx.tenant_id, "demo", "orders"))

    def test_ad06_unsupported_manifest_fails_before_fixture_read(self):
        self.app.register_adapter_manifest(self.ctx, AdapterCapabilityManifest(
            self.ctx.tenant_id, "demo", "orders", "demo-v1",
            frozenset({AdapterCapability.INBOUND_EVENTS}), frozenset({1}), datetime.now(timezone.utc)))
        fixture = self.adapter(DemoPage((), None, False, datetime.now(timezone.utc)))
        with self.assertRaises(ConflictError):
            self.app.poll_demo_connection(self.ctx, "demo", "orders", 0, fixture)
        self.assertEqual([], fixture.calls)

    def test_ad08_retryable_read_and_empty_terminal_page_leave_no_cursor_loss(self):
        fixture = self.adapter(DemoPage((), None, False, datetime.now(timezone.utc)))
        fixture.fail_once = True
        with self.assertRaises(Exception):
            self.app.poll_demo_connection(self.ctx, "demo", "orders", 0, fixture)
        result = self.app.poll_demo_connection(self.ctx, "demo", "orders", 0, fixture)
        self.assertFalse(result.has_more)
        self.assertEqual(1, result.checkpoint.version)
        self.assertIsNotNone(result.checkpoint.last_success_at)

    def test_ad07_foreign_payload_reference_is_not_disclosed(self):
        page = DemoPage((self.row(),), None, False, datetime.now(timezone.utc))
        result = self.app.poll_demo_connection(self.ctx, "demo", "orders", 0, self.adapter(page))
        other = self.app.bootstrap_tenant("OTHER", "other@example.test")
        with self.assertRaises(NotFoundError):
            self.app.get_normalized_payload(other, result.payload_refs[0])

    def test_ad04_restart_replays_committed_page_idempotently(self):
        page = DemoPage((self.row(),), "p1", False, datetime.now(timezone.utc))
        first = self.app.poll_demo_connection(self.ctx, "demo", "orders", 0, self.adapter(page))
        self.repo.close()
        self.repo = SQLiteRepository(Path(self.temp.name) / "adapter.sqlite3")
        self.app = StoreControlPlane(self.repo)
        replay = self.app.poll_demo_connection(self.ctx, "demo", "orders", 1, self.adapter(page, page))
        self.assertEqual(first.inbox_ids, replay.inbox_ids)
        self.assertEqual(1, len(self.app.inbox_for(self.ctx)))

    def test_ad05_concurrent_checkpoint_contenders_have_one_winner(self):
        path = Path(self.temp.name) / "adapter.sqlite3"
        barrier = threading.Barrier(2)
        outcomes = []

        def contender():
            repo = SQLiteRepository(path)
            try:
                app = StoreControlPlane(repo)
                ctx = app.context_for(self.ctx.tenant_id, self.ctx.user_id)
                adapter = self.adapter(DemoPage((self.row(),), None, False, datetime.now(timezone.utc)))
                barrier.wait()
                try:
                    outcomes.append(app.poll_demo_connection(ctx, "demo", "orders", 0, adapter))
                except ConflictError:
                    outcomes.append(None)
            finally:
                repo.close()

        threads = [threading.Thread(target=contender) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertEqual(1, sum(value is not None for value in outcomes))
        self.assertEqual(1, len(self.app.inbox_for(self.ctx)))

    def test_ad09_revisions_are_immutable_and_modified_event_is_rejected(self):
        first = self.app.poll_demo_connection(self.ctx, "demo", "orders", 0,
            self.adapter(DemoPage((self.row("e2", 2),), "p1", False, datetime.now(timezone.utc))))
        second = self.app.poll_demo_connection(self.ctx, "demo", "orders", 1,
            self.adapter(DemoPage((self.row("e1", 1),), "p2", False, datetime.now(timezone.utc)),
                          DemoPage((self.row("e1", 1),), "p2", False, datetime.now(timezone.utc))))
        self.assertNotEqual(first.payload_refs[0], second.payload_refs[0])
        modified = self.row("e1", 1, 303)
        modified["lines"][0]["unit_minor"] = 101
        with self.assertRaises(ConflictError):
            self.app.poll_demo_connection(self.ctx, "demo", "orders", 2,
                self.adapter(DemoPage((), "p1", False, datetime.now(timezone.utc)),
                             DemoPage((), "p2", False, datetime.now(timezone.utc)),
                             DemoPage((modified,), "p3", False, datetime.now(timezone.utc))))
        self.assertEqual(2, len(self.app.normalized_payloads_for(self.ctx)))

    def test_ad10_migration_and_immutable_payload_triggers(self):
        self.assertEqual(16, self.repo.readiness()["schema_version"])
        result = self.app.poll_demo_connection(self.ctx, "demo", "orders", 0,
            self.adapter(DemoPage((self.row(),), None, False, datetime.now(timezone.utc))))
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.connection.execute("DELETE FROM normalized_inbound_payloads WHERE tenant_id=?", (self.ctx.tenant_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.connection.execute("UPDATE normalized_inbound_payloads SET payload_json='{}' WHERE tenant_id=? AND immutable_ref=?",
                                        (self.ctx.tenant_id, result.payload_refs[0]))


if __name__ == "__main__":
    unittest.main()
