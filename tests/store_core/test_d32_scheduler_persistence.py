"""Integration tests for scheduler checkpoint and window config SQLite persistence."""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from packages.store_core.sqlite_repository import SQLiteRepository
from packages.store_core.domain import (
    ApprovalWindowKind,
    ApprovalWindowConfig,
    SchedulerCheckpoint,
    Tenant,
)
from packages.store_core.errors import ConflictError


class SchedulerPersistenceTests(unittest.TestCase):
    """Test SQLite persistence of scheduler state."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.repo = SQLiteRepository(str(self.db_path))
        self.tenant_id = "tenant-001"
        self.repo.add_tenant(Tenant(self.tenant_id, "Test Tenant", datetime.now(timezone.utc)))

    def tearDown(self) -> None:
        self.repo.close()
        self.temp_dir.cleanup()

    def test_save_and_retrieve_approval_window_config(self) -> None:
        """Approval window config persists and loads correctly."""
        config = ApprovalWindowConfig(
            tenant_id=self.tenant_id,
            kind=ApprovalWindowKind.PRODUCT,
            hours=(9, 13, 17),
            version=1,
        )

        self.repo.save_approval_window_config(config)
        retrieved = self.repo.get_approval_window_config(
            self.tenant_id, ApprovalWindowKind.PRODUCT.value
        )

        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.tenant_id, self.tenant_id)
        self.assertEqual(retrieved.kind, ApprovalWindowKind.PRODUCT)
        self.assertEqual(retrieved.hours, (9, 13, 17))
        self.assertEqual(retrieved.version, 1)

    def test_save_and_retrieve_scheduler_checkpoint(self) -> None:
        """Scheduler checkpoint persists and loads correctly."""
        now = datetime(2026, 9, 25, 9, 30, 0, tzinfo=timezone.utc)
        checkpoint = SchedulerCheckpoint(
            tenant_id=self.tenant_id,
            kind=ApprovalWindowKind.PRODUCT,
            hour=9,
            last_executed_at=now,
            version=1,
        )

        self.repo.save_scheduler_checkpoint(checkpoint)
        retrieved = self.repo.get_scheduler_checkpoint(
            self.tenant_id, ApprovalWindowKind.PRODUCT.value, 9
        )

        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.tenant_id, self.tenant_id)
        self.assertEqual(retrieved.kind, ApprovalWindowKind.PRODUCT)
        self.assertEqual(retrieved.hour, 9)
        self.assertEqual(retrieved.last_executed_at, now)
        self.assertEqual(retrieved.version, 1)

    def test_checkpoint_not_found_returns_none(self) -> None:
        """Missing checkpoint returns None."""
        retrieved = self.repo.get_scheduler_checkpoint(
            self.tenant_id, ApprovalWindowKind.PRODUCT.value, 9
        )
        self.assertIsNone(retrieved)

    def test_window_config_not_found_returns_none(self) -> None:
        """Missing window config returns None."""
        retrieved = self.repo.get_approval_window_config(
            self.tenant_id, ApprovalWindowKind.PRODUCT.value
        )
        self.assertIsNone(retrieved)

    def test_persistence_across_connections(self) -> None:
        """Data saved in one connection is readable in another."""
        config = ApprovalWindowConfig(
            tenant_id=self.tenant_id,
            kind=ApprovalWindowKind.PURCHASE,
            hours=(9, 12, 15, 18),
            version=1,
        )
        checkpoint = SchedulerCheckpoint(
            tenant_id=self.tenant_id,
            kind=ApprovalWindowKind.PURCHASE,
            hour=12,
            last_executed_at=datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc),
            version=1,
        )

        self.repo.save_approval_window_config(config)
        self.repo.save_scheduler_checkpoint(checkpoint)
        self.repo.close()

        repo2 = SQLiteRepository(str(self.db_path))
        try:
            retrieved_config = repo2.get_approval_window_config(
                self.tenant_id, ApprovalWindowKind.PURCHASE.value
            )
            retrieved_checkpoint = repo2.get_scheduler_checkpoint(
                self.tenant_id, ApprovalWindowKind.PURCHASE.value, 12
            )

            self.assertIsNotNone(retrieved_config)
            self.assertEqual(retrieved_config.hours, (9, 12, 15, 18))
            self.assertIsNotNone(retrieved_checkpoint)
            self.assertEqual(retrieved_checkpoint.hour, 12)
        finally:
            repo2.close()

    def test_checkpoint_version_conflict_on_concurrent_write(self) -> None:
        """Second write with stale version fails with ConflictError."""
        now = datetime(2026, 9, 25, 9, 30, 0, tzinfo=timezone.utc)
        checkpoint1 = SchedulerCheckpoint(
            tenant_id=self.tenant_id,
            kind=ApprovalWindowKind.AGENT,
            hour=9,
            last_executed_at=now,
            version=1,
        )

        self.repo.save_scheduler_checkpoint(checkpoint1)

        later = now + timedelta(minutes=5)
        checkpoint2 = SchedulerCheckpoint(
            tenant_id=self.tenant_id,
            kind=ApprovalWindowKind.AGENT,
            hour=9,
            last_executed_at=later,
            version=2,
        )

        self.repo.save_scheduler_checkpoint(checkpoint2)

        stale = SchedulerCheckpoint(
            tenant_id=self.tenant_id,
            kind=ApprovalWindowKind.AGENT,
            hour=9,
            last_executed_at=later,
            version=2,
        )

        with self.assertRaises(ConflictError) as ctx:
            self.repo.save_scheduler_checkpoint(stale)
        self.assertIn("version conflict", str(ctx.exception))

    def test_update_approval_window_config(self) -> None:
        """Updating window config overwrites previous version."""
        config1 = ApprovalWindowConfig(
            tenant_id=self.tenant_id,
            kind=ApprovalWindowKind.PRODUCT,
            hours=(9, 13, 17),
            version=1,
        )
        self.repo.save_approval_window_config(config1)

        config2 = ApprovalWindowConfig(
            tenant_id=self.tenant_id,
            kind=ApprovalWindowKind.PRODUCT,
            hours=(8, 14, 20),
            version=2,
        )
        self.repo.save_approval_window_config(config2)

        retrieved = self.repo.get_approval_window_config(
            self.tenant_id, ApprovalWindowKind.PRODUCT.value
        )
        self.assertEqual(retrieved.hours, (8, 14, 20))
        self.assertEqual(retrieved.version, 2)

    def test_multiple_window_kinds_in_same_tenant(self) -> None:
        """Multiple window kinds for same tenant are stored separately."""
        product_config = ApprovalWindowConfig(
            tenant_id=self.tenant_id,
            kind=ApprovalWindowKind.PRODUCT,
            hours=(9, 13, 17),
            version=1,
        )
        purchase_config = ApprovalWindowConfig(
            tenant_id=self.tenant_id,
            kind=ApprovalWindowKind.PURCHASE,
            hours=(9, 12, 15, 18),
            version=1,
        )

        self.repo.save_approval_window_config(product_config)
        self.repo.save_approval_window_config(purchase_config)

        product = self.repo.get_approval_window_config(
            self.tenant_id, ApprovalWindowKind.PRODUCT.value
        )
        purchase = self.repo.get_approval_window_config(
            self.tenant_id, ApprovalWindowKind.PURCHASE.value
        )

        self.assertIsNotNone(product)
        self.assertIsNotNone(purchase)
        self.assertEqual(product.hours, (9, 13, 17))
        self.assertEqual(purchase.hours, (9, 12, 15, 18))

    def test_checkpoint_for_different_hours(self) -> None:
        """Checkpoints for different hours are stored separately."""
        now = datetime(2026, 9, 25, 9, 30, 0, tzinfo=timezone.utc)
        checkpoint_9 = SchedulerCheckpoint(
            tenant_id=self.tenant_id,
            kind=ApprovalWindowKind.PRODUCT,
            hour=9,
            last_executed_at=now,
            version=1,
        )
        checkpoint_13 = SchedulerCheckpoint(
            tenant_id=self.tenant_id,
            kind=ApprovalWindowKind.PRODUCT,
            hour=13,
            last_executed_at=now + timedelta(hours=4),
            version=1,
        )

        self.repo.save_scheduler_checkpoint(checkpoint_9)
        self.repo.save_scheduler_checkpoint(checkpoint_13)

        retrieved_9 = self.repo.get_scheduler_checkpoint(
            self.tenant_id, ApprovalWindowKind.PRODUCT.value, 9
        )
        retrieved_13 = self.repo.get_scheduler_checkpoint(
            self.tenant_id, ApprovalWindowKind.PRODUCT.value, 13
        )

        self.assertIsNotNone(retrieved_9)
        self.assertIsNotNone(retrieved_13)
        self.assertEqual(retrieved_9.hour, 9)
        self.assertEqual(retrieved_13.hour, 13)

    def test_schema_migration_created_tables(self) -> None:
        """Schema migration 23 creates required tables."""
        cursor = self.repo.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('approval_window_configs', 'scheduler_checkpoints')"
        )
        tables = {row['name'] for row in cursor}
        self.assertIn('approval_window_configs', tables)
        self.assertIn('scheduler_checkpoints', tables)

    def test_checkpoint_timezone_aware(self) -> None:
        """Stored checkpoint preserves timezone information."""
        now = datetime(2026, 9, 25, 14, 30, 45, 123456, tzinfo=timezone.utc)
        checkpoint = SchedulerCheckpoint(
            tenant_id=self.tenant_id,
            kind=ApprovalWindowKind.AGENT,
            hour=14,
            last_executed_at=now,
            version=1,
        )

        self.repo.save_scheduler_checkpoint(checkpoint)
        retrieved = self.repo.get_scheduler_checkpoint(
            self.tenant_id, ApprovalWindowKind.AGENT.value, 14
        )

        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.last_executed_at, now)
        self.assertIsNotNone(retrieved.last_executed_at.tzinfo)

    def test_concurrent_writes_to_different_checkpoints_succeed(self) -> None:
        """Concurrent writes to different checkpoints both succeed."""
        now = datetime(2026, 9, 25, 9, 30, 0, tzinfo=timezone.utc)
        checkpoint_product = SchedulerCheckpoint(
            tenant_id=self.tenant_id,
            kind=ApprovalWindowKind.PRODUCT,
            hour=9,
            last_executed_at=now,
            version=1,
        )
        checkpoint_purchase = SchedulerCheckpoint(
            tenant_id=self.tenant_id,
            kind=ApprovalWindowKind.PURCHASE,
            hour=9,
            last_executed_at=now,
            version=1,
        )

        self.repo.save_scheduler_checkpoint(checkpoint_product)
        self.repo.save_scheduler_checkpoint(checkpoint_purchase)

        retrieved_product = self.repo.get_scheduler_checkpoint(
            self.tenant_id, ApprovalWindowKind.PRODUCT.value, 9
        )
        retrieved_purchase = self.repo.get_scheduler_checkpoint(
            self.tenant_id, ApprovalWindowKind.PURCHASE.value, 9
        )

        self.assertIsNotNone(retrieved_product)
        self.assertIsNotNone(retrieved_purchase)


if __name__ == "__main__":
    unittest.main()
