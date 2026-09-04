from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

from .domain import (
    APPROVAL_CAPABILITY,
    ROLE_CAPABILITIES,
    Approval,
    ApprovalKind,
    ApprovalState,
    AuditEvent,
    Capability,
    Command,
    CommandState,
    Membership,
    Role,
    Tenant,
    TenantContext,
    User,
)
from .errors import AuthorizationError, ConflictError
from .repository import InMemoryRepository


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class StoreControlPlane:
    """Safe local control-plane slice; it performs no external side effects."""

    def __init__(
        self,
        repository: InMemoryRepository | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repo = repository or InMemoryRepository()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def bootstrap_tenant(self, legal_name: str, master_email: str) -> TenantContext:
        now = self._clock()
        tenant = Tenant(str(uuid4()), legal_name, now)
        user = User(str(uuid4()), master_email.strip().lower(), now)
        self.repo.add_tenant(tenant)
        self.repo.add_user(user)
        membership = Membership(tenant.id, user.id, frozenset({Role.MASTER}))
        self.repo.memberships[(tenant.id, user.id)] = membership
        self._audit(tenant.id, user.id, "tenant.bootstrap", tenant.id, "succeeded", {})
        return TenantContext(tenant.id, user.id, membership.version)

    def context_for(self, tenant_id: str, user_id: str) -> TenantContext:
        membership = self.repo.get_membership(tenant_id, user_id)
        if not membership.active:
            raise AuthorizationError("membership revoked")
        return TenantContext(tenant_id, user_id, membership.version)

    def _membership(self, context: TenantContext) -> Membership:
        membership = self.repo.get_membership(context.tenant_id, context.user_id)
        if not membership.active or membership.version != context.membership_version:
            raise AuthorizationError("session is stale or revoked")
        return membership

    def require(self, context: TenantContext, capability: Capability) -> None:
        membership = self._membership(context)
        available = frozenset().union(*(ROLE_CAPABILITIES[role] for role in membership.roles))
        if capability not in available:
            raise AuthorizationError(f"missing capability: {capability}")

    def add_member(self, context: TenantContext, email: str, roles: Sequence[Role]) -> TenantContext:
        self.require(context, Capability.TENANT_ADMIN)
        role_set = frozenset(roles)
        if not role_set or Role.MASTER in role_set:
            raise ConflictError("delegated member requires non-master role(s)")
        active_count = sum(1 for m in self.repo.tenant_memberships(context.tenant_id) if m.active)
        if active_count >= 3:
            raise ConflictError("a tenant supports one master plus two active members")
        normalized = email.strip().lower()
        user = next((u for u in self.repo.users.values() if u.email == normalized), None)
        if user is None:
            user = User(str(uuid4()), normalized, self._clock())
            self.repo.add_user(user)
        key = (context.tenant_id, user.id)
        prior = self.repo.memberships.get(key)
        version = prior.version + 1 if prior else 1
        membership = Membership(context.tenant_id, user.id, role_set, True, version)
        self.repo.memberships[key] = membership
        self._audit(context.tenant_id, context.user_id, "membership.add", user.id, "succeeded", {"roles": sorted(role_set)})
        return TenantContext(context.tenant_id, user.id, version)

    def change_member_roles(self, context: TenantContext, user_id: str, roles: Sequence[Role]) -> None:
        self.require(context, Capability.TENANT_ADMIN)
        membership = self.repo.get_membership(context.tenant_id, user_id)
        if Role.MASTER in membership.roles:
            raise ConflictError("master role cannot be delegated or changed")
        role_set = frozenset(roles)
        if not role_set or Role.MASTER in role_set:
            raise ConflictError("invalid delegated roles")
        membership.roles = role_set
        membership.version += 1
        self._audit(context.tenant_id, context.user_id, "membership.roles_changed", user_id, "succeeded", {"roles": sorted(role_set)})

    def revoke_member(self, context: TenantContext, user_id: str) -> None:
        self.require(context, Capability.TENANT_ADMIN)
        membership = self.repo.get_membership(context.tenant_id, user_id)
        if Role.MASTER in membership.roles:
            raise ConflictError("master membership cannot be revoked")
        membership.active = False
        membership.version += 1
        self._audit(context.tenant_id, context.user_id, "membership.revoke", user_id, "succeeded", {})

    def create_command(
        self,
        context: TenantContext,
        kind: ApprovalKind,
        target_ref: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
        evidence: Sequence[Mapping[str, Any]] = (),
    ) -> tuple[Command, Approval]:
        # Human users may propose only work within their delegated boundary.
        # A future service principal gets a separate narrow policy, never this shortcut.
        self.require(context, APPROVAL_CAPABILITY[kind])
        if not idempotency_key.strip():
            raise ConflictError("idempotency key is required")
        digest = _digest({"kind": kind, "target_ref": target_ref, "payload": payload})
        idem_key = (context.tenant_id, idempotency_key)
        existing_id = self.repo.command_idempotency.get(idem_key)
        if existing_id:
            existing = self.repo.get_command(context.tenant_id, existing_id)
            if existing.payload_digest != digest:
                raise ConflictError("idempotency key reused for different command")
            return existing, self.repo.get_approval_for_command(context.tenant_id, existing.id)
        now = self._clock()
        command = Command(
            str(uuid4()), context.tenant_id, kind, target_ref, dict(payload), digest,
            idempotency_key, CommandState.AWAITING_APPROVAL, now,
        )
        approval = Approval(
            str(uuid4()), context.tenant_id, command.id, kind, ApprovalState.PENDING,
            now, now + timedelta(hours=24), tuple(dict(item) for item in evidence),
        )
        self.repo.commands[(context.tenant_id, command.id)] = command
        self.repo.approvals[(context.tenant_id, command.id)] = approval
        self.repo.command_idempotency[idem_key] = command.id
        self._audit(context.tenant_id, context.user_id, "command.create", command.id, "accepted", {"payload_digest": digest})
        return command, approval

    def decide(self, context: TenantContext, command_id: str, approve: bool, reason: str) -> Approval:
        try:
            command = self.repo.get_command(context.tenant_id, command_id)
        except AuthorizationError:
            self._audit(
                context.tenant_id, context.user_id, "command.cross_tenant_access",
                "redacted", "blocked", {},
            )
            raise
        approval = self.repo.get_approval_for_command(context.tenant_id, command_id)
        self.require(context, APPROVAL_CAPABILITY[command.kind])
        if approval.state != ApprovalState.PENDING:
            raise ConflictError("approval is no longer pending")
        now = self._clock()
        if now >= approval.expires_at:
            approval.state = ApprovalState.EXPIRED
            command.state = CommandState.EXPIRED
            self._audit(context.tenant_id, context.user_id, "approval.expire", approval.id, "blocked", {})
            raise ConflictError("approval expired")
        approval.state = ApprovalState.APPROVED if approve else ApprovalState.REJECTED
        command.state = CommandState.APPROVED if approve else CommandState.REJECTED
        approval.decided_by = context.user_id
        approval.decision_reason = reason
        self._audit(context.tenant_id, context.user_id, "approval.decide", approval.id, "succeeded", {"decision": approval.state})
        return approval

    def supersede(
        self,
        context: TenantContext,
        command_id: str,
        changed_payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> tuple[Command, Approval]:
        old = self.repo.get_command(context.tenant_id, command_id)
        old_approval = self.repo.get_approval_for_command(context.tenant_id, command_id)
        changed_digest = _digest({"kind": old.kind, "target_ref": old.target_ref, "payload": changed_payload})
        if changed_digest == old.payload_digest:
            raise ConflictError("no material change")
        if old_approval.state not in {ApprovalState.PENDING, ApprovalState.APPROVED}:
            raise ConflictError("command cannot be superseded")
        old_approval.state = ApprovalState.SUPERSEDED
        old.state = CommandState.SUPERSEDED
        new_command, new_approval = self.create_command(
            context, old.kind, old.target_ref, changed_payload, idempotency_key, old_approval.evidence
        )
        new_command.supersedes_id = old.id
        self._audit(context.tenant_id, context.user_id, "command.supersede", old.id, "succeeded", {"replacement": new_command.id})
        return new_command, new_approval

    def audit_log(self, context: TenantContext) -> tuple[AuditEvent, ...]:
        self.require(context, Capability.READ_AUDIT)
        return self.repo.audits_for(context.tenant_id)

    def verify_audit_chain(self, tenant_id: str) -> bool:
        previous: str | None = None
        for event in self.repo.audits_for(tenant_id):
            material = {
                "id": event.id, "tenant_id": event.tenant_id, "occurred_at": event.occurred_at,
                "actor_ref": event.actor_ref, "action": event.action, "target_ref": event.target_ref,
                "outcome": event.outcome, "correlation_id": event.correlation_id,
                "metadata": event.metadata, "prev_hash": previous,
            }
            if event.prev_hash != previous or event.event_hash != _digest(material):
                return False
            previous = event.event_hash
        return True

    def _audit(
        self, tenant_id: str, actor_ref: str, action: str, target_ref: str,
        outcome: str, metadata: Mapping[str, Any],
    ) -> None:
        events = self.repo.audit_events[tenant_id]
        previous = events[-1].event_hash if events else None
        event_id, correlation_id, now = str(uuid4()), str(uuid4()), self._clock()
        material = {
            "id": event_id, "tenant_id": tenant_id, "occurred_at": now,
            "actor_ref": actor_ref, "action": action, "target_ref": target_ref,
            "outcome": outcome, "correlation_id": correlation_id,
            "metadata": dict(metadata), "prev_hash": previous,
        }
        events.append(AuditEvent(event_hash=_digest(material), **material))
