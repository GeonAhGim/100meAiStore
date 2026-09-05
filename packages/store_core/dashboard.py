"""Read-only, tenant-scoped operations dashboard projection.

The projection deliberately contains no model or network client.  It reads
already durable repository state and local development evidence only; the
browser/API layer must never turn a dashboard refresh into an AI invocation.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .domain import AgentState, InboxState, OutboxState, TenantContext
from .errors import AuthorizationError, NotFoundError


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class DashboardProjection:
    """Build a deterministic dashboard read model from local durable facts."""

    def __init__(self, repository: Any, *, project_root: str | Path | None = None) -> None:
        self.repo = repository
        self.project_root = Path(project_root) if project_root else None

    def snapshot(self, context: TenantContext, *, now: datetime | None = None) -> dict[str, Any]:
        """Return only the active tenant's operational projection.

        Membership validation is intentionally performed before any tenant
        query.  The control plane is passed in by the service facade through
        ``authorize`` so a missing/stale membership fails closed.
        """
        if not context.tenant_id or not context.user_id:
            raise AuthorizationError("dashboard requires tenant context")
        now = now or _utc_now()
        outbox = tuple(self.repo.outbox_for(context.tenant_id))
        inbox = tuple(self.repo.inbox_for(context.tenant_id)) if hasattr(self.repo, "inbox_for") else ()
        audits = tuple(self.repo.audits_for(context.tenant_id))

        readiness: dict[str, Any]
        if hasattr(self.repo, "readiness"):
            try:
                readiness = dict(self.repo.readiness())
            except Exception as exc:  # health is data; a broken DB is not hidden
                readiness = {"ready": False, "error": type(exc).__name__}
        else:
            readiness = {"ready": True, "source": "in_memory_demo"}

        workers = self._workers(outbox, now)
        agents = self._agents(context.tenant_id, now)
        queues = {
            "outbox_pending": sum(e.state == OutboxState.PENDING for e in outbox),
            "outbox_leased": sum(e.state == OutboxState.LEASED for e in outbox),
            "outbox_retry": sum(e.state == OutboxState.RETRY for e in outbox),
            "outbox_dead": sum(e.state == OutboxState.DEAD for e in outbox),
            "inbox_received": sum(e.state == InboxState.RECEIVED for e in inbox),
            # Reconciliation is intentionally explicit until its durable table
            # exists; zero here means no projection source is installed.
            "reconciliation_open": 0,
        }
        approvals = self._approval_count(context.tenant_id)
        blockers = []
        if not readiness.get("ready", False):
            blockers.append("storage_not_ready")
        if queues["outbox_dead"]:
            blockers.append("outbox_dead_letter")
        phase = self._phase_evidence()
        return {
            "generated_at": _iso(now),
            "stale_after_seconds": 90,
            "stale": False,
            "tenant_id": context.tenant_id,
            "phase": phase,
            "recent_commits": self._recent_commits(),
            "tests": self._test_evidence(),
            "workers": workers,
            "agents": agents,
            "agent_status_source": "sqlite_checkpoint" if agents else "no_checkpoint_found",
            "queues": queues,
            "approvals_required": approvals,
            "next_work": "durable inbox → approval intent digest → UNKNOWN reconciliation",
            "blockers": blockers,
            "readiness": readiness,
            "audit": {"event_count": len(audits)},
            "tokens_cost": {"source": "local instrumentation", "models": {}, "total_tokens": 0, "total_cost": 0},
            "controls": {"external_writes_enabled": False, "ai_summary_enabled": False},
        }

    def _agents(self, tenant_id: str, now: datetime) -> list[dict[str, Any]]:
        if not hasattr(self.repo, "agent_status_for"):
            return []
        result = []
        for status in self.repo.agent_status_for(tenant_id):
            heartbeat_age = None
            stale = False
            if status.last_heartbeat_at:
                heartbeat_age = max(0.0, (now - status.last_heartbeat_at).total_seconds())
                stale = heartbeat_age > 30
            state = status.state.value
            if stale and state == AgentState.RUNNING.value:
                state = "stale"
            result.append({
                "agent_id": status.agent_id, "role": status.role, "state": state,
                "observed_state": status.state.value, "stale": stale,
                "heartbeat_age_seconds": heartbeat_age,
                "current_task": status.current_task, "started_at": _iso(status.started_at),
                "last_heartbeat_at": _iso(status.last_heartbeat_at), "ended_at": _iso(status.ended_at),
                "last_message": status.last_message, "last_commit": status.last_commit,
                "test_result": status.test_result, "next_task": status.next_task,
                "blocker": status.blocker, "usage_limited": status.usage_limited,
            })
        return result

    def _phase_evidence(self) -> dict[str, Any]:
        name = "Phase 2 — durable local DEMO control plane"
        if not self.project_root:
            return {"name": name, "completion_percent": None, "completed_acceptance": None,
                    "total_acceptance": None, "evidence": [], "evidence_status": "unavailable"}
        path = self.project_root / ".codex" / "phase-progress.json"
        try:
            evidence = json.loads(path.read_text(encoding="utf-8"))
            completed = int(evidence["completed_acceptance"])
            total = int(evidence["total_acceptance"])
            if total <= 0 or completed < 0 or completed > total:
                raise ValueError("invalid phase progress")
            return {"name": str(evidence.get("phase", name)), "completion_percent": round(completed / total * 100, 1),
                    "completed_acceptance": completed, "total_acceptance": total,
                    "evidence": list(evidence.get("evidence", [])), "evidence_status": "verified_checkpoint"}
        except (OSError, ValueError, TypeError, KeyError):
            return {"name": name, "completion_percent": None, "completed_acceptance": None,
                    "total_acceptance": None, "evidence": [], "evidence_status": "missing_checkpoint"}

    def _approval_count(self, tenant_id: str) -> int:
        # Keep the read model independent of a new approval-list API while
        # retaining tenant predicates through the existing repository method.
        try:
            rows = self.repo.connection.execute(
                "SELECT COUNT(*) FROM approvals WHERE tenant_id=? AND state='pending'", (tenant_id,)
            ).fetchone()
            return int(rows[0]) if rows else 0
        except AttributeError:
            return sum(1 for approval in self.repo.approvals.values() if approval.tenant_id == tenant_id and approval.state.value == "pending")

    @staticmethod
    def _workers(outbox: tuple[Any, ...], now: datetime) -> dict[str, Any]:
        running: dict[str, int] = {}
        failed = 0
        waiting = 0
        for event in outbox:
            if event.state == OutboxState.LEASED and event.lease_owner:
                running[event.lease_owner] = running.get(event.lease_owner, 0) + 1
            elif event.state in {OutboxState.PENDING, OutboxState.RETRY}:
                waiting += 1
            elif event.state == OutboxState.DEAD:
                failed += 1
        return {"running": running, "waiting": waiting, "failed": failed, "heartbeat": "derived_from_leases"}

    def _recent_commits(self) -> list[dict[str, str]]:
        if not self.project_root:
            return []
        try:
            result = subprocess.run(
                ["git", "log", "-5", "--format=%h%x09%s"], cwd=self.project_root,
                capture_output=True, text=True, timeout=2, check=True,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        commits = []
        for line in result.stdout.splitlines():
            short, _, subject = line.partition("\t")
            if short and subject:
                commits.append({"id": short[:12], "subject": subject[:200]})
        return commits

    def _test_evidence(self) -> dict[str, Any]:
        # Test evidence is intentionally supplied by a local CI/checkpoint
        # file.  A missing file is shown as unknown, never fabricated as green.
        if not self.project_root:
            return {"last_result": "unknown", "passed": None, "failed": None, "source": "not_configured"}
        path = self.project_root / ".codex" / "last-test.json"
        try:
            evidence = json.loads(path.read_text(encoding="utf-8"))
            return {
                "last_result": str(evidence.get("result", "unknown")),
                "passed": evidence.get("passed"), "failed": evidence.get("failed"),
                "finished_at": evidence.get("finished_at"), "source": "local_checkpoint",
            }
        except (OSError, ValueError, TypeError):
            return {"last_result": "unknown", "passed": None, "failed": None, "source": "missing_checkpoint"}
