"""Tests for operational batch scheduler."""

import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock

from packages.store_core.batch_scheduler import BatchScheduler
from packages.store_core.domain import (
    ApprovalWindowKind,
    ApprovalWindowConfig,
    SchedulerCheckpoint,
)


class TestBatchScheduler(unittest.TestCase):
    """Test approval window scheduling and state persistence."""

    def setUp(self) -> None:
        self.mock_service = Mock()
        self.mock_repository = Mock()
        self.mock_service.repository = self.mock_repository
        self.scheduler = BatchScheduler(self.mock_service)
        self.tenant_id = "tenant-001"

    def test_product_approval_default_hours(self) -> None:
        """Product approval windows are at 09, 13, 17 by default."""
        now = datetime(2026, 9, 25, 9, 30, 0, tzinfo=timezone.utc)
        self.mock_repository.get_approval_window_config.return_value = None
        self.mock_repository.get_scheduler_checkpoint.return_value = None

        windows = self.scheduler.next_execution_windows(self.tenant_id, now)

        product_windows = [w for w in windows if w[0] == ApprovalWindowKind.PRODUCT]
        self.assertEqual(len(product_windows), 1)
        self.assertEqual(product_windows[0], (ApprovalWindowKind.PRODUCT, 9))

    def test_purchase_approval_default_hours(self) -> None:
        """Purchase approval windows are at 09, 12, 15, 18 by default."""
        now = datetime(2026, 9, 25, 12, 30, 0, tzinfo=timezone.utc)
        self.mock_repository.get_approval_window_config.return_value = None
        self.mock_repository.get_scheduler_checkpoint.return_value = None

        windows = self.scheduler.next_execution_windows(self.tenant_id, now)

        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0], (ApprovalWindowKind.PURCHASE, 12))

    def test_agent_default_hours(self) -> None:
        """Agent windows are at 09, 13, 17, 21 by default."""
        now = datetime(2026, 9, 25, 21, 30, 0, tzinfo=timezone.utc)
        self.mock_repository.get_approval_window_config.return_value = None
        self.mock_repository.get_scheduler_checkpoint.return_value = None

        windows = self.scheduler.next_execution_windows(self.tenant_id, now)

        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0], (ApprovalWindowKind.AGENT, 21))

    def test_no_execution_outside_window(self) -> None:
        """No windows execute if current hour is not in configuration."""
        now = datetime(2026, 9, 25, 10, 30, 0, tzinfo=timezone.utc)
        self.mock_repository.get_approval_window_config.return_value = None
        self.mock_repository.get_scheduler_checkpoint.return_value = None

        windows = self.scheduler.next_execution_windows(self.tenant_id, now)

        self.assertEqual(len(windows), 0)

    def test_duplicate_execution_prevention(self) -> None:
        """Window does not execute twice in the same day."""
        now = datetime(2026, 9, 25, 9, 30, 0, tzinfo=timezone.utc)
        prev_execution = datetime(2026, 9, 25, 9, 0, 0, tzinfo=timezone.utc)
        checkpoint = SchedulerCheckpoint(
            tenant_id=self.tenant_id,
            kind=ApprovalWindowKind.PRODUCT,
            hour=9,
            last_executed_at=prev_execution,
            version=1,
        )

        self.mock_repository.get_approval_window_config.return_value = None
        self.mock_repository.get_scheduler_checkpoint.return_value = checkpoint

        windows = self.scheduler.next_execution_windows(self.tenant_id, now)

        self.assertEqual(len(windows), 0)

    def test_execution_after_midnight(self) -> None:
        """Window executes again on the next day at the same hour."""
        now = datetime(2026, 9, 26, 9, 30, 0, tzinfo=timezone.utc)
        prev_execution = datetime(2026, 9, 25, 9, 0, 0, tzinfo=timezone.utc)
        product_checkpoint = SchedulerCheckpoint(
            tenant_id=self.tenant_id,
            kind=ApprovalWindowKind.PRODUCT,
            hour=9,
            last_executed_at=prev_execution,
            version=1,
        )

        self.mock_repository.get_approval_window_config.return_value = None

        def get_checkpoint_side_effect(
            tenant_id: str, kind: str, hour: int
        ) -> SchedulerCheckpoint | None:
            if kind == ApprovalWindowKind.PRODUCT.value and hour == 9:
                return product_checkpoint
            return None

        self.mock_repository.get_scheduler_checkpoint.side_effect = (
            get_checkpoint_side_effect
        )

        windows = self.scheduler.next_execution_windows(self.tenant_id, now)

        product_windows = [w for w in windows if w[0] == ApprovalWindowKind.PRODUCT]
        self.assertEqual(len(product_windows), 1)
        self.assertEqual(product_windows[0], (ApprovalWindowKind.PRODUCT, 9))

    def test_custom_window_configuration(self) -> None:
        """Custom window hours are respected instead of defaults."""
        now = datetime(2026, 9, 25, 10, 30, 0, tzinfo=timezone.utc)
        product_config = ApprovalWindowConfig(
            tenant_id=self.tenant_id,
            kind=ApprovalWindowKind.PRODUCT,
            hours=(10, 14, 18),
            version=1,
        )

        def get_config_side_effect(tenant_id: str, kind: str) -> ApprovalWindowConfig | None:
            if kind == ApprovalWindowKind.PRODUCT.value:
                return product_config
            return None

        self.mock_repository.get_approval_window_config.side_effect = get_config_side_effect
        self.mock_repository.get_scheduler_checkpoint.return_value = None

        windows = self.scheduler.next_execution_windows(self.tenant_id, now)

        product_windows = [w for w in windows if w[0] == ApprovalWindowKind.PRODUCT]
        self.assertEqual(len(product_windows), 1)
        self.assertEqual(product_windows[0], (ApprovalWindowKind.PRODUCT, 10))

    def test_record_execution(self) -> None:
        """Recording execution persists checkpoint."""
        now = datetime(2026, 9, 25, 9, 30, 0, tzinfo=timezone.utc)

        self.scheduler.record_execution(
            self.tenant_id, ApprovalWindowKind.PRODUCT, 9, now
        )

        self.mock_repository.save_scheduler_checkpoint.assert_called_once()
        saved_checkpoint = self.mock_repository.save_scheduler_checkpoint.call_args[0][0]
        self.assertEqual(saved_checkpoint.tenant_id, self.tenant_id)
        self.assertEqual(saved_checkpoint.kind, ApprovalWindowKind.PRODUCT)
        self.assertEqual(saved_checkpoint.hour, 9)
        self.assertEqual(saved_checkpoint.last_executed_at, now)

    def test_update_window_config(self) -> None:
        """Updating configuration persists new hours."""
        new_hours = (8, 14, 19)

        self.scheduler.update_window_config(
            self.tenant_id, ApprovalWindowKind.PRODUCT, new_hours
        )

        self.mock_repository.save_approval_window_config.assert_called_once()
        saved_config = self.mock_repository.save_approval_window_config.call_args[0][0]
        self.assertEqual(saved_config.tenant_id, self.tenant_id)
        self.assertEqual(saved_config.kind, ApprovalWindowKind.PRODUCT)
        self.assertEqual(saved_config.hours, new_hours)

    def test_multiple_windows_in_one_run(self) -> None:
        """Multiple window kinds can be due at the same time."""
        now = datetime(2026, 9, 25, 9, 30, 0, tzinfo=timezone.utc)
        self.mock_repository.get_approval_window_config.return_value = None
        self.mock_repository.get_scheduler_checkpoint.return_value = None

        windows = self.scheduler.next_execution_windows(self.tenant_id, now)

        due_kinds = {w[0] for w in windows}
        self.assertIn(ApprovalWindowKind.PRODUCT, due_kinds)
        self.assertIn(ApprovalWindowKind.PURCHASE, due_kinds)
        self.assertIn(ApprovalWindowKind.AGENT, due_kinds)

    def test_naive_datetime_becomes_utc(self) -> None:
        """Naive datetimes are treated as UTC."""
        now = datetime(2026, 9, 25, 9, 30, 0)
        self.mock_repository.get_approval_window_config.return_value = None
        self.mock_repository.get_scheduler_checkpoint.return_value = None

        windows = self.scheduler.next_execution_windows(self.tenant_id, now)

        self.assertEqual(len(windows), 3)

    def test_narrow_window_at_hour_boundary(self) -> None:
        """Execution happens within the hour."""
        at_start = datetime(2026, 9, 25, 9, 0, 1, tzinfo=timezone.utc)
        at_end = datetime(2026, 9, 25, 9, 59, 59, tzinfo=timezone.utc)

        self.mock_repository.get_approval_window_config.return_value = None
        self.mock_repository.get_scheduler_checkpoint.return_value = None

        windows_start = self.scheduler.next_execution_windows(self.tenant_id, at_start)
        windows_end = self.scheduler.next_execution_windows(self.tenant_id, at_end)

        self.assertGreater(len(windows_start), 0)
        self.assertGreater(len(windows_end), 0)

    def test_no_execution_just_before_hour(self) -> None:
        """No execution at :59 of previous hour."""
        now = datetime(2026, 9, 25, 8, 59, 59, tzinfo=timezone.utc)
        self.mock_repository.get_approval_window_config.return_value = None
        self.mock_repository.get_scheduler_checkpoint.return_value = None

        windows = self.scheduler.next_execution_windows(self.tenant_id, now)

        self.assertEqual(len(windows), 0)

    def test_idempotency_within_hour(self) -> None:
        """Multiple calls within the same hour return same result."""
        now1 = datetime(2026, 9, 25, 9, 10, 0, tzinfo=timezone.utc)
        now2 = datetime(2026, 9, 25, 9, 50, 0, tzinfo=timezone.utc)

        self.mock_repository.get_approval_window_config.return_value = None
        self.mock_repository.get_scheduler_checkpoint.return_value = None

        windows1 = self.scheduler.next_execution_windows(self.tenant_id, now1)

        self.mock_repository.get_scheduler_checkpoint.return_value = SchedulerCheckpoint(
            tenant_id=self.tenant_id,
            kind=ApprovalWindowKind.PRODUCT,
            hour=9,
            last_executed_at=now1,
            version=1,
        )
        windows2 = self.scheduler.next_execution_windows(self.tenant_id, now2)

        self.assertEqual(len(windows1), 3)
        self.assertEqual(len(windows2), 0)

    def test_timezone_aware_checkpoint_comparison(self) -> None:
        """Checkpoint comparison works across different UTC offset representations."""
        now_utc = datetime(2026, 9, 25, 9, 30, 0, tzinfo=timezone.utc)
        prev_execution = datetime(2026, 9, 25, 9, 0, 0, tzinfo=timezone.utc)
        checkpoint = SchedulerCheckpoint(
            tenant_id=self.tenant_id,
            kind=ApprovalWindowKind.PRODUCT,
            hour=9,
            last_executed_at=prev_execution,
            version=1,
        )

        self.mock_repository.get_approval_window_config.return_value = None
        self.mock_repository.get_scheduler_checkpoint.return_value = checkpoint

        windows = self.scheduler.next_execution_windows(self.tenant_id, now_utc)

        self.assertEqual(len(windows), 0)


if __name__ == "__main__":
    unittest.main()
