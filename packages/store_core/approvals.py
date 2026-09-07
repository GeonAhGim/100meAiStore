"""Local DEMO approval inbox and mobile-friendly approval resource contract."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from uuid import uuid4

from .domain import APPROVAL_CAPABILITY, ApprovalState, Capability, CommandState, OutboxEvent, OutboxState
from .errors import AuthorizationError, ConflictError

_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}\Z")


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
    return {
        "approval_id": approval.id,
        "kind": approval.kind.value,
        "risk_badges": [],
        "target": {"label": command.target_ref, "ref": command.target_ref},
        "before": {},
        "after": dict(command.payload),
        "profit": {"projected_profit_minor": None, "margin_ex_ad": None, "margin_with_ad": None, "currency": command.payload.get("currency") if isinstance(command.payload, dict) else None},
        "evidence": [dict(item) for item in approval.evidence],
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
    if not isinstance(approval_id, str) or not _OPAQUE.fullmatch(approval_id) or type(approve) is not bool:
        raise ConflictError("invalid approval decision")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise ConflictError("approval decision reason is required")
    if not isinstance(confirmation_nonce, str) or not _OPAQUE.fullmatch(confirmation_nonce):
        raise ConflictError("confirmation nonce is required")
    with service.repo.transaction():
        service._membership(context)
        approval = service.repo.get_approval(context.tenant_id, approval_id)
        service.require(context, APPROVAL_CAPABILITY[approval.kind])
    # Core decide re-checks membership, intent and single-decider state in its
    # own transaction. Let it commit expiry evidence before raising to callers.
    return service.decide(context, approval.command_id, approve, reason)
