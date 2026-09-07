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
    r"receiver[_-]?(phone|email|address)|recipient[_-]?(phone|email|address)|customer[_-]?(phone|email|address))"
)


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


def _expire(service: Any, context: Any, approval: Any, command: Any, now: datetime) -> Any:
    if approval.state == ApprovalState.PENDING and now >= approval.expires_at:
        approval.state = ApprovalState.EXPIRED
        command.state = CommandState.EXPIRED
        service.repo.save_approval(approval)
        service.repo.save_command(command)
        service._audit(context.tenant_id, "system:approval-expiry", "approval.expire", approval.id, "blocked", {})
        service.repo.append_outbox(OutboxEvent(str(uuid4()), context.tenant_id, "approval.expired", command.id,
                                               {"approval_id": approval.id, "command_id": command.id},
                                               f"approval.expired:{approval.id}", OutboxState.PENDING, now))
    return approval


def _can_read(approval: Any, permissions: list[str]) -> bool:
    return (APPROVAL_CAPABILITY[approval.kind].value in permissions
            or Capability.READ_AUDIT.value in permissions)


def _item(service: Any, context: Any, approval: Any, command: Any,
          permissions: list[str]) -> dict[str, Any]:
    pending = approval.state == ApprovalState.PENDING
    decision = "approval_required" if pending else ("allow" if approval.state == ApprovalState.APPROVED else "deny")
    before, after, profit, evidence, risk_badges = _material_preview(command, approval.evidence)
    return {
        "approval_id": approval.id,
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
    with service.repo.transaction():
        permissions = _permissions(service, context)
        items = []
        for approval in service.repo.approvals_for(context.tenant_id):
            if not _can_read(approval, permissions):
                continue
            command = service.repo.get_command(context.tenant_id, approval.command_id)
            approval = _expire(service, context, approval, command, now)
            if approval.state == ApprovalState.PENDING:
                items.append(_item(service, context, approval, command, permissions))
        return {"items": items, "next_cursor": None, "as_of": now.isoformat(), "stale": False, "permissions": permissions}


def approval_detail(service: Any, context: Any, approval_id: str) -> dict[str, Any]:
    if not isinstance(approval_id, str) or not _OPAQUE.fullmatch(approval_id):
        raise ConflictError("invalid approval id")
    now = service._clock()
    with service.repo.transaction():
        permissions = _permissions(service, context)
        approval = service.repo.get_approval(context.tenant_id, approval_id)
        if not _can_read(approval, permissions):
            raise AuthorizationError("missing approval read capability")
        command = service.repo.get_command(context.tenant_id, approval.command_id)
        return _item(service, context, _expire(service, context, approval, command, now), command, permissions)


def decide_approval(service: Any, context: Any, approval_id: str, approve: bool,
                    reason: str, confirmation_nonce: str) -> Any:
    """Retained only as a fail-closed compatibility boundary.

    Browser callers must use ``decide_approval_authenticated`` so the server can
    bind and consume a nonce against the authenticated session and command.
    Internal domain workflows call ``service.decide`` directly.
    """
    raise AuthorizationError("authenticated browser decision required")
