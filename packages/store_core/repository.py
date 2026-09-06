from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from threading import RLock
from contextlib import contextmanager
from datetime import datetime
from typing import Iterable

from .domain import AgentStatusSnapshot, Approval, AuditEvent, Command, Membership, OutboxEvent, OutboxState, Tenant, User
from .errors import ConflictError, NotFoundError, TenantBoundaryError
from .domain import (AdapterCapabilityManifest, InboxMessage, InboxState, ApprovalIntent,
                     ExecutionPreparation, NormalizedInboundPayload, AdapterPollCheckpoint)
from .domain import (ChannelOrder, OrderLine, RoutingDecision, SupplierPurchaseOrder, PurchaseLine,
                     ChannelOrderState, RoutingState, PurchaseOrderState)
from .domain import DemoExecutionControl, ExecutionAttempt, AttemptObservation


class InMemoryRepository:
    """DEMO adapter. Every tenant-owned lookup requires an explicit tenant id.

    The service layer groups writes using the snapshot-backed transaction. A
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
        self.agent_status: dict[tuple[str, str], AgentStatusSnapshot] = {}
        self.inbox: dict[tuple[str, str], InboxMessage] = {}
        self.manifests: dict[tuple[str, str, str], AdapterCapabilityManifest] = {}
        self._lock = RLock()
        self.approval_intents: dict[tuple[str, str], ApprovalIntent] = {}
        self.execution_preparations: dict[tuple[str, str], ExecutionPreparation] = {}
        self.demo_controls: dict[tuple[str, str], DemoExecutionControl] = {}
        self.attempts: dict[tuple[str, str], ExecutionAttempt] = {}
        self.observations: dict[tuple[str, str], list[AttemptObservation]] = defaultdict(list)
        self.normalized_payloads: dict[tuple[str, str], NormalizedInboundPayload] = {}
        self.poll_checkpoints: dict[tuple[str, str, str], AdapterPollCheckpoint] = {}
        self.channel_orders: dict[tuple[str, str], ChannelOrder] = {}
        self.order_lines: dict[tuple[str, str], OrderLine] = {}
        self.routing_decisions: dict[tuple[str, str], RoutingDecision] = {}
        self.purchase_orders: dict[tuple[str, str], SupplierPurchaseOrder] = {}
        self.purchase_lines: dict[tuple[str, str], PurchaseLine] = {}

    def save_demo_control(self, value: DemoExecutionControl) -> None:
        self.get_command(value.tenant_id, value.command_id)
        self.demo_controls[(value.tenant_id, value.command_id)] = value

    def get_demo_control(self, tenant_id: str, command_id: str) -> DemoExecutionControl:
        try:
            return self.demo_controls[(tenant_id, command_id)]
        except KeyError as exc:
            raise NotFoundError('demo execution control missing') from exc

    def insert_attempt(self, value: ExecutionAttempt) -> None:
        preparation = self.get_execution_preparation(value.tenant_id, value.command_id)
        if preparation is None or preparation.id != value.preparation_id:
            raise NotFoundError('preparation not found')
        if self.attempt_for_key(value.tenant_id, value.operation_key) or (value.tenant_id, value.id) in self.attempts:
            raise ConflictError('attempt already exists')
        self.attempts[(value.tenant_id, value.id)] = deepcopy(value)

    def get_attempt(self, tenant_id: str, attempt_id: str) -> ExecutionAttempt:
        try:
            return deepcopy(self.attempts[(tenant_id, attempt_id)])
        except KeyError as exc:
            raise NotFoundError('attempt not found') from exc

    def attempt_for_key(self, tenant_id: str, operation_key: str) -> ExecutionAttempt | None:
        return next((deepcopy(value) for (tid, _), value in self.attempts.items() if tid == tenant_id and value.operation_key == operation_key), None)

    def update_attempt(self, value: ExecutionAttempt, expected_version: int) -> None:
        current = self.get_attempt(value.tenant_id, value.id)
        if current.version != expected_version or value.version != expected_version + 1:
            raise ConflictError('attempt version conflict')
        self.attempts[(value.tenant_id, value.id)] = deepcopy(value)

    def append_observation(self, value: AttemptObservation) -> None:
        self.get_attempt(value.tenant_id, value.attempt_id)
        rows = self.observations[(value.tenant_id, value.attempt_id)]
        if any(row.id == value.id for row in rows):
            raise ConflictError('observation already exists')
        rows.append(value)

    def observations_for(self, tenant_id: str, attempt_id: str) -> tuple[AttemptObservation, ...]:
        self.get_attempt(tenant_id, attempt_id)
        return tuple(self.observations[(tenant_id, attempt_id)])

    def save_approval_intent(self, intent: ApprovalIntent) -> None:
        self.get_command(intent.tenant_id, intent.command_id)
        key = (intent.tenant_id, intent.command_id)
        if key in self.approval_intents:
            raise ConflictError('approval intent already exists')
        self.approval_intents[key] = intent

    def get_approval_intent(self, tenant_id: str, command_id: str) -> ApprovalIntent | None:
        return self.approval_intents.get((tenant_id, command_id))

    def save_execution_preparation(self, value: ExecutionPreparation) -> None:
        key = (value.tenant_id, value.command_id)
        if key not in self.approval_intents:
            raise NotFoundError('approval intent not found')
        if key in self.execution_preparations:
            raise ConflictError('execution already prepared')
        self.execution_preparations[key] = value

    def get_execution_preparation(self, tenant_id: str, command_id: str) -> ExecutionPreparation | None:
        return self.execution_preparations.get((tenant_id, command_id))

    @contextmanager
    def transaction(self):
        with self._lock:
            snapshot = deepcopy({k: v for k, v in self.__dict__.items() if k != '_lock'})
            try:
                yield self
            except BaseException:
                # Preserve legacy aggregate references when a rejected operation
                # did not mutate them; inbox reads themselves are detached.
                for name, previous in snapshot.items():
                    current = self.__dict__[name]
                    if isinstance(current, dict):
                        for key in list(current):
                            if key not in previous:
                                del current[key]
                        for key, value in previous.items():
                            if key not in current or current[key] != value:
                                current[key] = value
                    else:
                        self.__dict__[name] = previous
                raise

    def save_adapter_manifest(self, manifest: AdapterCapabilityManifest) -> None:
        if manifest.tenant_id not in self.tenants:
            raise NotFoundError('tenant not found')
        self.manifests[(manifest.tenant_id, manifest.provider, manifest.connection_id)] = deepcopy(manifest)

    def get_adapter_manifest(self, tenant_id: str, provider: str, connection_id: str) -> AdapterCapabilityManifest:
        try:
            return deepcopy(self.manifests[(tenant_id, provider, connection_id)])
        except KeyError as exc:
            raise NotFoundError('adapter manifest not found') from exc

    def receive_inbox(self, message: InboxMessage) -> tuple[InboxMessage, bool]:
        with self.transaction():
            for prior in self.inbox_for(message.tenant_id):
                if (prior.provider, prior.connection_id, prior.external_event_id) == (message.provider, message.connection_id, message.external_event_id):
                    if (prior.payload_digest, prior.schema_version) != (message.payload_digest, message.schema_version):
                        raise ConflictError('inbound identity reused with different content')
                    return prior, True
            if message.tenant_id not in self.tenants:
                raise NotFoundError('tenant not found')
            if (message.tenant_id, message.id) in self.inbox:
                raise ConflictError('inbox id already exists')
            self.inbox[(message.tenant_id, message.id)] = deepcopy(message)
            return deepcopy(message), False

    def get_inbox(self, tenant_id: str, inbox_id: str) -> InboxMessage:
        try:
            return deepcopy(self.inbox[(tenant_id, inbox_id)])
        except KeyError as exc:
            raise NotFoundError('inbox message not found') from exc

    def inbox_for(self, tenant_id: str) -> tuple[InboxMessage, ...]:
        return tuple(deepcopy(v) for (tid, _), v in self.inbox.items() if tid == tenant_id)

    def mark_inbox_processed(self, tenant_id: str, inbox_id: str, expected_version: int, processed_at: datetime) -> InboxMessage:
        with self.transaction():
            value = self.get_inbox(tenant_id, inbox_id)
            if value.version != expected_version or value.state != InboxState.RECEIVED:
                raise ConflictError('inbox state/version conflict')
            value.state, value.version, value.processed_at = InboxState.PROCESSED, value.version + 1, processed_at
            self.inbox[(tenant_id, inbox_id)] = deepcopy(value)
            return value

    def save_normalized_payload(self, value: NormalizedInboundPayload) -> NormalizedInboundPayload:
        key = (value.tenant_id, value.immutable_ref)
        prior = self.normalized_payloads.get(key)
        if prior:
            if prior.canonical_digest != value.canonical_digest or prior.payload_json != value.payload_json:
                raise ConflictError("immutable payload reference reused with different content")
            return deepcopy(prior)
        self.normalized_payloads[key] = deepcopy(value)
        return deepcopy(value)

    def get_normalized_payload(self, tenant_id: str, immutable_ref: str) -> NormalizedInboundPayload:
        try:
            return deepcopy(self.normalized_payloads[(tenant_id, immutable_ref)])
        except KeyError as exc:
            raise NotFoundError("normalized payload not found") from exc

    def normalized_payloads_for(self, tenant_id: str) -> tuple[NormalizedInboundPayload, ...]:
        return tuple(deepcopy(value) for (tid, _), value in self.normalized_payloads.items() if tid == tenant_id)

    def get_poll_checkpoint(self, tenant_id: str, provider: str, connection_id: str) -> AdapterPollCheckpoint | None:
        return deepcopy(self.poll_checkpoints.get((tenant_id, provider, connection_id)))

    def insert_or_advance_poll_checkpoint(self, value: AdapterPollCheckpoint, expected_version: int) -> AdapterPollCheckpoint:
        key = (value.tenant_id, value.provider, value.connection_id)
        prior = self.poll_checkpoints.get(key)
        actual = prior.version if prior else 0
        if actual != expected_version or value.version != expected_version + 1:
            raise ConflictError("adapter poll checkpoint version conflict")
        self.poll_checkpoints[key] = deepcopy(value)
        return deepcopy(value)

    def save_channel_order(self, value: ChannelOrder) -> tuple[ChannelOrder, bool]:
        key = (value.tenant_id, value.id)
        prior = self.channel_orders.get(key)
        existing = next((row for (tenant, _), row in self.channel_orders.items()
                         if tenant == value.tenant_id and row.channel_id == value.channel_id
                         and row.external_order_key == value.external_order_key), None)
        if prior or existing:
            prior = prior or existing
            if (prior.channel_id, prior.external_order_key, prior.payload_ref, prior.total_minor, prior.currency) != (value.channel_id, value.external_order_key, value.payload_ref, value.total_minor, value.currency):
                raise ConflictError("order identity reused with different content")
            return deepcopy(prior), True
        self.channel_orders[key] = deepcopy(value)
        return deepcopy(value), False

    def get_channel_order(self, tenant_id: str, order_id: str) -> ChannelOrder:
        try: return deepcopy(self.channel_orders[(tenant_id, order_id)])
        except KeyError as exc: raise NotFoundError("order not found") from exc

    def update_channel_order(self, value: ChannelOrder, expected_version: int) -> None:
        current = self.get_channel_order(value.tenant_id, value.id)
        if current.version != expected_version or value.version != expected_version + 1: raise ConflictError("order version conflict")
        self.channel_orders[(value.tenant_id, value.id)] = deepcopy(value)

    def save_order_line(self, value: OrderLine) -> None:
        self.order_lines[(value.tenant_id, value.id)] = deepcopy(value)

    def update_order_line(self, value: OrderLine, expected_version: int) -> None:
        current = next((v for (tid, _), v in self.order_lines.items() if tid == value.tenant_id and v.id == value.id), None)
        if current is None: raise NotFoundError("order line not found")
        if current.version != expected_version or value.version != expected_version + 1: raise ConflictError("order line version conflict")
        self.order_lines[(value.tenant_id, value.id)] = deepcopy(value)

    def order_lines_for(self, tenant_id: str, order_id: str) -> tuple[OrderLine, ...]:
        self.get_channel_order(tenant_id, order_id)
        return tuple(deepcopy(row) for (tid, _), row in self.order_lines.items()
                     if tid == tenant_id and row.channel_order_id == order_id)

    def save_routing_decision(self, value: RoutingDecision) -> None:
        key = (value.tenant_id, value.order_line_id)
        prior = self.routing_decisions.get(key)
        if prior and prior != value: raise ConflictError("routing decision already exists")
        self.routing_decisions[key] = deepcopy(value)

    def routing_for(self, tenant_id: str, order_id: str) -> tuple[RoutingDecision, ...]:
        lines = {line.id for line in self.order_lines_for(tenant_id, order_id)}
        return tuple(deepcopy(row) for (tid, line_id), row in self.routing_decisions.items()
                     if tid == tenant_id and line_id in lines)

    def save_purchase_order(self, value: SupplierPurchaseOrder) -> tuple[SupplierPurchaseOrder, bool]:
        existing = next((row for (tid, _), row in self.purchase_orders.items()
                         if tid == value.tenant_id and row.idempotency_key == value.idempotency_key), None)
        if existing:
            if (existing.channel_order_id, existing.supplier_id) != (value.channel_order_id, value.supplier_id):
                raise ConflictError("purchase idempotency key reused")
            return deepcopy(existing), True
        key = (value.tenant_id, value.id)
        if key in self.purchase_orders: raise ConflictError("purchase order already exists")
        self.purchase_orders[key] = deepcopy(value)
        return deepcopy(value), False

    def update_purchase_order(self, value: SupplierPurchaseOrder, expected_version: int) -> None:
        current = self.purchase_orders.get((value.tenant_id, value.id))
        if current is None: raise NotFoundError("purchase order not found")
        if current.version != expected_version or value.version != expected_version + 1: raise ConflictError("purchase order version conflict")
        self.purchase_orders[(value.tenant_id, value.id)] = deepcopy(value)

    def get_purchase_order(self, tenant_id: str, po_id: str) -> SupplierPurchaseOrder:
        try: return deepcopy(self.purchase_orders[(tenant_id, po_id)])
        except KeyError as exc: raise NotFoundError("purchase order not found") from exc

    def save_purchase_line(self, value: PurchaseLine) -> None:
        self.purchase_lines[(value.tenant_id, value.id)] = deepcopy(value)

    def purchase_orders_for(self, tenant_id: str, order_id: str) -> tuple[SupplierPurchaseOrder, ...]:
        self.get_channel_order(tenant_id, order_id)
        return tuple(deepcopy(row) for (tid, _), row in self.purchase_orders.items()
                     if tid == tenant_id and row.channel_order_id == order_id)

    def purchase_lines_for(self, tenant_id: str, po_id: str) -> tuple[PurchaseLine, ...]:
        return tuple(deepcopy(row) for (tid, _), row in self.purchase_lines.items()
                     if tid == tenant_id and row.purchase_order_id == po_id)

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

    def save_agent_status(self, status: AgentStatusSnapshot) -> None:
        self.agent_status[(status.tenant_id, status.agent_id)] = status

    def agent_status_for(self, tenant_id: str) -> tuple[AgentStatusSnapshot, ...]:
        return tuple(status for (tid, _), status in sorted(self.agent_status.items()) if tid == tenant_id)
