from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping


class Role(str, Enum):
    MASTER = "master"
    FUNDS = "funds"
    CATALOG_CS = "catalog_cs"
    AUDITOR = "auditor"


class Capability(str, Enum):
    TENANT_ADMIN = "tenant_admin"
    FUNDS = "funds"
    CATALOG = "catalog"
    CS = "cs"
    APPROVE_PURCHASE = "approve_purchase"
    APPROVE_REFUND = "approve_refund"
    APPROVE_PRODUCT = "approve_product"
    APPROVE_CS = "approve_cs"
    READ_AUDIT = "read_audit"
    RECOVERY = "recovery"


ROLE_CAPABILITIES: Mapping[Role, frozenset[Capability]] = {
    Role.MASTER: frozenset(Capability),
    Role.FUNDS: frozenset(
        {Capability.FUNDS, Capability.APPROVE_PURCHASE, Capability.APPROVE_REFUND}
    ),
    Role.CATALOG_CS: frozenset(
        {
            Capability.CATALOG,
            Capability.CS,
            Capability.APPROVE_PRODUCT,
            Capability.APPROVE_CS,
        }
    ),
    Role.AUDITOR: frozenset({Capability.READ_AUDIT}),
}


class ApprovalKind(str, Enum):
    PURCHASE = "purchase"
    PRODUCT = "product"
    SUPPLIER_REPLACEMENT = "supplier_replacement"
    REFUND = "refund"
    CS = "cs"
    PAUSE = "pause"


APPROVAL_CAPABILITY: Mapping[ApprovalKind, Capability] = {
    ApprovalKind.PURCHASE: Capability.APPROVE_PURCHASE,
    ApprovalKind.PRODUCT: Capability.APPROVE_PRODUCT,
    ApprovalKind.SUPPLIER_REPLACEMENT: Capability.APPROVE_PRODUCT,
    ApprovalKind.REFUND: Capability.APPROVE_REFUND,
    ApprovalKind.CS: Capability.APPROVE_CS,
    ApprovalKind.PAUSE: Capability.RECOVERY,
}


class CommandState(str, Enum):
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class ApprovalState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    user_id: str
    membership_version: int


@dataclass
class Tenant:
    id: str
    legal_name: str
    created_at: datetime


@dataclass
class User:
    id: str
    email: str
    created_at: datetime


@dataclass
class Membership:
    tenant_id: str
    user_id: str
    roles: frozenset[Role]
    active: bool = True
    version: int = 1


@dataclass
class Command:
    id: str
    tenant_id: str
    kind: ApprovalKind
    target_ref: str
    payload: Mapping[str, Any]
    payload_digest: str
    idempotency_key: str
    state: CommandState
    created_at: datetime
    supersedes_id: str | None = None


@dataclass
class Approval:
    id: str
    tenant_id: str
    command_id: str
    kind: ApprovalKind
    state: ApprovalState
    requested_at: datetime
    expires_at: datetime
    evidence: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    decided_by: str | None = None
    decision_reason: str | None = None


@dataclass(frozen=True)
class AuditEvent:
    id: str
    tenant_id: str
    occurred_at: datetime
    actor_ref: str
    action: str
    target_ref: str
    outcome: str
    correlation_id: str
    metadata: Mapping[str, Any]
    prev_hash: str | None
    event_hash: str


class OutboxState(str, Enum):
    PENDING = "pending"
    LEASED = "leased"
    RETRY = "retry"
    COMPLETED = "completed"
    DEAD = "dead"


@dataclass
class OutboxEvent:
    id: str
    tenant_id: str
    topic: str
    aggregate_ref: str
    payload: Mapping[str, Any]
    idempotency_key: str
    state: OutboxState
    created_at: datetime
    checkpoint: Mapping[str, Any] = field(default_factory=dict)
    lease_owner: str | None = None
    lease_until: datetime | None = None
    fencing_token: int = 0
    completed_at: datetime | None = None
    attempts: int = 0
    available_at: datetime | None = None
    last_error: str | None = None
