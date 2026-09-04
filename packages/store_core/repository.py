from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from .domain import Approval, AuditEvent, Command, Membership, Tenant, User
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

    def add_tenant(self, tenant: Tenant) -> None:
        self.tenants[tenant.id] = tenant

    def add_user(self, user: User) -> None:
        existing = next((u for u in self.users.values() if u.email == user.email), None)
        if existing and existing.id != user.id:
            raise ConflictError("email already registered")
        self.users[user.id] = user

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
