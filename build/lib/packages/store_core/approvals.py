"""Local DEMO approval inbox and mobile-friendly approval resource contract."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Mapping
from uuid import uuid4

from .domain import APPROVAL_CAPABILITY, ApprovalState, Capability, CommandState, OutboxEvent, OutboxState
from .errors import AuthorizationError, ConflictError

_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}\Z")
_SENSITIVE_KEY = re.compile(
    r"(?i)(api[_-]?key|authorization|access[_-]?token|refresh[_-]?token|password|secret|credential|"
    r"phone|email|address|contact|"
    r"receiver[_-]?(name|phone|email|address)|recipient[_-]?(name|phone|email|address)|"
    r"customer[_-]?(name|phone|email|address))"
)
_SIGNED_URL = re.compile(r"(?i)^https?://.*(?:[?&](?:token|signature|x-amz-signature|x-goog-signature)=)")


class ApprovalExpiryWorker:
    """A scheduler-neutral, tenant-scoped durable expiry worker."""

    def __init__(self, service: Any, tenant_id: str, batch_size: int = 100) -> None:
        self.service = service
        self.tenant_id = tenant_id
        self.batch_size = batch_size

    def run_once(self) -> dict[str, Any]:
        return expire_due_approvals(self.service, self.tenant_id, self.batch_size)


def _safe_preview(value: Any, depth: int = 0) -> tuple[Any, bool]:
    """Redact client-unsafe fields without mutating authoritative command data."""
    if depth > 8:
        return "[REDACTED]", True
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        redacted = False
        for key, item in value.items():
            label = str(key)
            if _SENSITIVE_KEY.search(label):
                result[label] = "[REDACTED]"
                redacted = True
            else:
                safe, changed = _safe_preview(item, depth + 1)
                result[label] = safe
                redacted = redacted or changed
        return result, redacted
    if isinstance(value, (list, tuple)):
        items, redacted = [], False
        for item in value:
            safe, changed = _safe_preview(item, depth + 1)
            items.append(safe)
            redacted = redacted or changed
        return items, redacted
    if isinstance(value, str) and _SIGNED_URL.search(value):
        return "[REDACTED]", True
    return value, False


def _material_preview(command: Any, evidence: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[Any], list[str]]:
    payload = command.payload if isinstance(command.payload, Mapping) else {}
    raw_before = payload.get("before", {})
    raw_after = payload.get("after", payload)
    before, before_redacted = _safe_preview(raw_before)
    after, after_redacted = _safe_preview(raw_after)
    safe_evidence, evidence_redacted = _safe_preview(evidence)
    raw_profit = payload.get("profit", {})
    if not isinstance(raw_profit, Mapping):
        raw_profit = {}
    profit = {
        "projected_profit_minor": raw_profit.get("projected_profit_minor") if type(raw_profit.get("projected_profit_minor")) is int else None,
        "margin_ex_ad": raw_profit.get("margin_ex_ad") if isinstance(raw_profit.get("margin_ex_ad"), str) else None,
        "margin_with_ad": raw_profit.get("margin_with_ad") if isinstance(raw_profit.get("margin_with_ad"), str) else None,
        "currency": raw_profit.get("currency") if isinstance(raw_profit.get("currency"), str) else None,
    }
    badges = []
    if before_redacted or after_redacted or evidence_redacted:
        badges.append("sensitive_fields_redacted")
    if profit["projected_profit_minor"] is None:
        badges.append("profit_evidence_missing")
    return before, after, profit, safe_evidence, badges


def _permissions(service: Any, context: Any) -> list[str]:
    membership = service._membership(context)
    from .domain import ROLE_CAPABILITIES
    available = frozenset().union(*(ROLE_CAPABILITIES[role] for role in membership.roles))
    return sorted(value.value for value in available)


def _can_read(approval: Any, permissions: list[str]) -> bool:
    return (APPROVAL_CAPABILITY[approval.kind].value in permissions
            or Capability.READ_AUDIT.value in permissions)


def _item(service: Any, context: Any, approval: Any, command: Any,
          permissions: list[str], now: datetime) -> dict[str, Any]:
    due = approval.state == ApprovalState.PENDING and now >= approval.expires_at
    pending = approval.state == ApprovalState.PENDING and not due
    effective_state = ApprovalState.EXPIRED if due else approval.state
    decision = "approval_required" if pending else ("allow" if effective_state == ApprovalState.APPROVED else "deny")
    before, after, profit, evidence, risk_badges = _material_preview(command, approval.evidence)
    if due:
        risk_badges.append("approval_expired")
    return {
        "approval_id": approval.id,
        "state": effective_state.value,
        "kind": approval.kind.value,
        "risk_badges": risk_badges,
        "target": {"label": command.target_ref, "ref": command.target_ref},
        "before": before,
        "after": after,
        "profit": profit,
        "evidence": evidence,
        "policy": {"version": "v" + str(service.repo.get_approval_intent(context.tenant_id, command.id).policy_version) if service.repo.get_approval_intent(context.tenant_id, command.id) else "unknown", "decision": decision, "reasons": []},
        "rollback": {"available": False, "description": "DEMO only; no external side effect"},
        "expires_at": approval.expires_at.isoformat(),
        "actions": ["approve", "reject", "ask_question"] if pending and APPROVAL_CAPABILITY[approval.kind].value in permissions else [],
    }


def approval_inbox(service: Any, context: Any) -> dict[str, Any]:
    now = service._clock()
    permissions = _permissions(service, context)
    items = []
    for approval in service.repo.approvals_for(context.tenant_id):
        if not _can_read(approval, permissions) or approval.state != ApprovalState.PENDING:
            continue
        command = service.repo.get_command(context.tenant_id, approval.command_id)
        item = _item(service, context, approval, command, permissions, now)
        if item["state"] == ApprovalState.PENDING.value:
            items.append(item)
    return {"items": items, "next_cursor": None, "as_of": now.isoformat(), "stale": False, "permissions": permissions}


def approval_detail(service: Any, context: Any, approval_id: str) -> dict[str, Any]:
    if not isinstance(approval_id, str) or not _OPAQUE.fullmatch(approval_id):
        raise ConflictError("invalid approval id")
    now = service._clock()
    permissions = _permissions(service, context)
    approval = service.repo.get_approval(context.tenant_id, approval_id)
    if not _can_read(approval, permissions):
        raise AuthorizationError("missing approval read capability")
    command = service.repo.get_command(context.tenant_id, approval.command_id)
    return _item(service, context, approval, command, permissions, now)


def expire_due_approvals(service: Any, tenant_id: str, limit: int = 100) -> dict[str, Any]:
    """Expire one bounded tenant batch atomically without dispatching its outbox."""
    if not isinstance(tenant_id, str) or not _OPAQUE.fullmatch(tenant_id):
        raise ValueError("valid tenant id is required")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer between 1 and 1000")
    now = service._clock()
    expired_ids: list[str] = []
    with service.repo.transaction():
        for approval in service.repo.due_approvals(tenant_id, now, limit):
            if not service.repo.mark_approval_expired(
                    tenant_id, approval.id, approval.command_id, now):
                continue
            command = service.repo.get_command(tenant_id, approval.command_id)
            service._audit(tenant_id, "system:approval-expiry", "approval.expire", approval.id, "blocked", {})
            service.repo.append_outbox(OutboxEvent(
                str(uuid4()), tenant_id, "approval.expired", command.id,
                {"approval_id": approval.id, "command_id": command.id},
                f"approval.expired:{approval.id}", OutboxState.PENDING, now))
            expired_ids.append(approval.id)
    return {"tenant_id": tenant_id, "expired_count": len(expired_ids),
            "approval_ids": expired_ids, "as_of": now.isoformat()}


def decide_approval(service: Any, context: Any, approval_id: str, approve: bool,
                    reason: str, confirmation_nonce: str) -> Any:
    """Retained only as a fail-closed compatibility boundary.

    Browser callers must use ``decide_approval_authenticated`` so the server can
    bind and consume a nonce against the authenticated session and command.
    Internal domain workflows call ``service.decide`` directly.
    """
    raise AuthorizationError("authenticated browser decision required")
