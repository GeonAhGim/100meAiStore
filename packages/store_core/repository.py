from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from typing import Iterable

from .domain import Approval, AuditEvent, Command, Membership, OutboxEvent, OutboxState, Tenant, User
from .errors import ConflictError, NotFoundError, TenantBoundaryError


class InMemoryRepository:
    """DEMO adapter. Every tenant-owned lookup requires an explicit tenant id.

    The service layer owns transaction semantics for this in-memory adapter. A
    PostgreSQL adapter must implement the same methods inside one transaction,
    add tenant predicates to every query, and enable/test row-level security.
    """

    def __init__(self) -> None:
        self.tenants: dict[str, Tenant] = {}
        self.users: dict[str, User] = {}
        self.memberships: dict[tuple[str, str], Membership] = {}
        self.commands: dict[tuple[str, str], Command] = {}
        self.approvals: dict[tuple[str, str], Approval] = {}
        self.command_idempotency: dict[tuple[str, str], str] = {}
        self.audit_events: dict[str, list[AuditEvent]] = defaultdict(list)
        self.outbox: dict[tuple[str, str], OutboxEvent] = {}
        self.outbox_idempotency: dict[tuple[str, str], str] = {}

    @contextmanager
    def transaction(self):
        # Compatibility UoW: the original DEMO adapter retains object identity.
        # Durable atomic rollback is supplied by SQLite/PostgreSQL adapters.
        yield self

    def add_tenant(self, tenant: Tenant) -> None:
        self.tenants[tenant.id] = tenant

    def add_user(self, user: User) -> None:
        existing = next((u for u in self.users.values() if u.email == user.email), None)
        if existing and existing.id != user.id:
            raise ConflictError("email already registered")
        self.users[user.id] = user

    def find_user_by_email(self, email: str) -> User | None:
        return next((user for user in self.users.values() if user.email == email), None)

    def save_membership(self, membership: Membership) -> None:
        self.memberships[(membership.tenant_id, membership.user_id)] = membership

    def save_command(self, command: Command) -> None:
        self.commands[(command.tenant_id, command.id)] = command

    def save_approval(self, approval: Approval) -> None:
        self.approvals[(approval.tenant_id, approval.command_id)] = approval

    def command_id_for_key(self, tenant_id: str, idempotency_key: str) -> str | None:
        return self.command_idempotency.get((tenant_id, idempotency_key))

    def bind_command_key(self, tenant_id: str, idempotency_key: str, command_id: str) -> None:
        self.command_idempotency[(tenant_id, idempotency_key)] = command_id

    def append_audit(self, event: AuditEvent) -> None:
        self.audit_events[event.tenant_id].append(event)

    def append_outbox(self, event: OutboxEvent) -> None:
        key = (event.tenant_id, event.idempotency_key)
        if key in self.outbox_idempotency:
            raise ConflictError("outbox idempotency key already exists")
        self.outbox[(event.tenant_id, event.id)] = event
        self.outbox_idempotency[key] = event.id

    def outbox_for(self, tenant_id: str) -> tuple[OutboxEvent, ...]:
        return tuple(event for (tid, _), event in self.outbox.items() if tid == tenant_id)

    def claim_outbox(self, tenant_id: str, event_id: str, worker_id: str, now: datetime, lease_until: datetime) -> OutboxEvent:
        try:
            event = self.outbox[(tenant_id, event_id)]
        except KeyError as exc:
            if any(eid == event_id for _, eid in self.outbox):
                raise TenantBoundaryError("cross-tenant outbox access denied") from exc
            raise NotFoundError("outbox event not found") from exc
        if event.state == OutboxState.COMPLETED:
            raise ConflictError("outbox event already completed")
        if event.lease_until is not None and event.lease_until > now and event.lease_owner != worker_id:
            raise ConflictError("outbox event already leased")
        event.state = OutboxState.LEASED
        event.lease_owner = worker_id
        event.lease_until = lease_until
        event.fencing_token += 1
        return event

    def checkpoint_outbox(self, tenant_id: str, event_id: str, worker_id: str, fencing_token: int, checkpoint: dict, now: datetime, completed: bool = False) -> OutboxEvent:
        event = self.claimed_outbox(tenant_id, event_id, worker_id, fencing_token, now)
        event.checkpoint = dict(checkpoint)
        if completed:
            event.state = OutboxState.COMPLETED
            event.completed_at = now
            event.lease_owner = None
            event.lease_until = None
        return event

    def claimed_outbox(self, tenant_id: str, event_id: str, worker_id: str, fencing_token: int, now: datetime) -> OutboxEvent:
        event = next((e for e in self.outbox_for(tenant_id) if e.id == event_id), None)
        if event is None:
            raise NotFoundError("outbox event not found")
        if event.state != OutboxState.LEASED or event.lease_owner != worker_id or event.fencing_token != fencing_token or event.lease_until is None or event.lease_until <= now:
            raise ConflictError("stale or expired outbox lease")
        return event

    def get_user(self, user_id: str) -> User:
        try:
            return self.users[user_id]
        except KeyError as exc:
            raise NotFoundError("user not found") from exc

    def get_membership(self, tenant_id: str, user_id: str) -> Membership:
        try:
            return self.memberships[(tenant_id, user_id)]
        except KeyError as exc:
            raise NotFoundError("membership not found") from exc

    def tenant_memberships(self, tenant_id: str) -> Iterable[Membership]:
        return (m for (tid, _), m in self.memberships.items() if tid == tenant_id)

    def get_command(self, tenant_id: str, command_id: str) -> Command:
        command = self.commands.get((tenant_id, command_id))
        if command is None:
            if any(cid == command_id for _, cid in self.commands):
                raise TenantBoundaryError("cross-tenant command access denied")
            raise NotFoundError("command not found")
        return command

    def get_approval_for_command(self, tenant_id: str, command_id: str) -> Approval:
        command = self.get_command(tenant_id, command_id)
        try:
            return self.approvals[(tenant_id, command.id)]
        except KeyError as exc:
            raise NotFoundError("approval not found") from exc

    def audits_for(self, tenant_id: str) -> tuple[AuditEvent, ...]:
        return tuple(self.audit_events[tenant_id])
