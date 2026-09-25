"""Operational batch scheduler for approval windows and agent runs.

Windows are configured per tenant and execute once per window. Last execution
is persisted to prevent duplicate runs across restarts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from .domain import ApprovalWindowConfig, ApprovalWindowKind, SchedulerCheckpoint


class BatchScheduler:
    """Scheduler for approval windows and agent runs with durable state."""

    def __init__(self, service: Any) -> None:
        self.service = service

    def next_execution_windows(
        self, tenant_id: str, now: datetime
    ) -> list[tuple[ApprovalWindowKind, int]]:
        """Return (kind, hour) tuples for windows that should execute now.

        A window executes if:
        - Current hour matches configured window hour for that kind
        - Last execution was in a different day
        """
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        due_windows = []
        for kind in ApprovalWindowKind:
            config = self.service.repository.get_approval_window_config(tenant_id, kind.value)
            if not config:
                config = self._default_config(tenant_id, kind)

            if now.hour not in config.hours:
                continue

            checkpoint = self.service.repository.get_scheduler_checkpoint(
                tenant_id, kind.value, now.hour
            )
            if checkpoint is None or self._is_new_day(checkpoint.last_executed_at, now):
                due_windows.append((kind, now.hour))

        return due_windows

    def record_execution(
        self, tenant_id: str, kind: ApprovalWindowKind, hour: int, executed_at: datetime
    ) -> None:
        """Record that a window execution completed."""
        if executed_at.tzinfo is None:
            executed_at = executed_at.replace(tzinfo=timezone.utc)

        checkpoint = SchedulerCheckpoint(
            tenant_id=tenant_id,
            kind=kind,
            hour=hour,
            last_executed_at=executed_at,
            version=1,
        )
        self.service.repository.save_scheduler_checkpoint(checkpoint)

    def update_window_config(
        self, tenant_id: str, kind: ApprovalWindowKind, hours: tuple[int, ...]
    ) -> None:
        """Update approval window hours for a tenant."""
        config = ApprovalWindowConfig(
            tenant_id=tenant_id,
            kind=kind,
            hours=hours,
            version=1,
        )
        self.service.repository.save_approval_window_config(config)

    @staticmethod
    def _default_config(
        tenant_id: str, kind: ApprovalWindowKind
    ) -> ApprovalWindowConfig:
        """Return default window configuration for a kind."""
        defaults = {
            ApprovalWindowKind.PRODUCT: (9, 13, 17),
            ApprovalWindowKind.PURCHASE: (9, 12, 15, 18),
            ApprovalWindowKind.AGENT: (9, 13, 17, 21),
        }
        return ApprovalWindowConfig(
            tenant_id=tenant_id,
            kind=kind,
            hours=defaults[kind],
            version=1,
        )

    @staticmethod
    def _is_new_day(last_executed: datetime, now: datetime) -> bool:
        """Check if now is on a different day than last_executed."""
        if last_executed.tzinfo is None:
            last_executed = last_executed.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        return last_executed.date() != now.date()
