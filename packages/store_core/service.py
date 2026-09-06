from __future__ import annotations

import hashlib
import json
import re
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
    AgentState,
    AgentStatusSnapshot,
    Capability,
    Command,
    CommandState,
    Membership,
    OutboxEvent,
    OutboxState,
    Role,
    Tenant,
    TenantContext,
    User,
)
from .errors import AuthorizationError, ConflictError, NotFoundError
from .dashboard import DashboardProjection
from .repository import InMemoryRepository
from .domain import AdapterCapability, AdapterCapabilityManifest, InboxMessage, InboxState, ApprovalIntent, ExecutionPreparation


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _strict_intent_digest(value: Any) -> str:
    def validate(item: Any) -> None:
        if isinstance(item, dict):
            if any(type(key) is not str for key in item):
                raise ValueError('intent keys must be strings')
            for child in item.values():
                validate(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                validate(child)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise TypeError('intent must contain JSON values')
    try:
        validate(value)
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ConflictError('intent must contain finite JSON values with string keys') from exc
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


class StoreControlPlane:
    """Safe local control-plane slice; it performs no external side effects."""

    def __init__(
        self,
        repository: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repo = repository or InMemoryRepository()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _inbound_identifier(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,254}', value):
            raise ConflictError('invalid opaque inbound identifier')
        return value

    def register_adapter_manifest(self, context: TenantContext, manifest: AdapterCapabilityManifest) -> None:
        with self.repo.transaction():
            self.require(context, Capability.TENANT_ADMIN)
            if manifest.tenant_id != context.tenant_id:
                raise AuthorizationError('manifest tenant mismatch')
            for value in (manifest.provider, manifest.connection_id, manifest.adapter_version):
                self._inbound_identifier(value)
            if any(not isinstance(c, AdapterCapability) for c in manifest.capabilities) or any(type(v) is not int or v < 1 for v in manifest.inbound_schema_versions):
                raise ConflictError('invalid manifest capabilities/schema versions')
            if manifest.updated_at.tzinfo is None:
                raise ConflictError('manifest timestamp must be timezone aware')
            self.repo.save_adapter_manifest(manifest)
            self._audit(context.tenant_id, context.user_id, 'adapter.manifest_registered', manifest.connection_id, 'succeeded', {'adapter_version': manifest.adapter_version})

    def get_inbox(self, context: TenantContext, inbox_id: str) -> InboxMessage:
        self.require(context, Capability.TENANT_ADMIN)
        return self.repo.get_inbox(context.tenant_id, inbox_id)

    def inbox_for(self, context: TenantContext) -> tuple[InboxMessage, ...]:
        self.require(context, Capability.TENANT_ADMIN)
        return self.repo.inbox_for(context.tenant_id)

    def receive_inbound(self, context: TenantContext, provider: str, connection_id: str,
                        external_event_id: str, schema_version: int, payload_digest: str,
                        raw_payload_ref: str | None = None) -> tuple[InboxMessage, bool]:
        with self.repo.transaction():
            self.require(context, Capability.TENANT_ADMIN)
            for value in (provider, connection_id, external_event_id):
                self._inbound_identifier(value)
            if type(schema_version) is not int or schema_version < 1:
                raise ConflictError('invalid inbound schema version')
            if not isinstance(payload_digest, str) or not re.fullmatch('[0-9a-f]{64}', payload_digest):
                raise ConflictError('invalid SHA-256 digest')
            if raw_payload_ref is not None:
                self._inbound_identifier(raw_payload_ref)
            manifest = self.repo.get_adapter_manifest(context.tenant_id, provider, connection_id)
            if AdapterCapability.INBOUND_EVENTS not in manifest.capabilities or schema_version not in manifest.inbound_schema_versions:
                raise ConflictError('unsupported inbound capability/schema')
            now = self._clock()
            message, replayed = self.repo.receive_inbox(InboxMessage(str(uuid4()), context.tenant_id,
                provider, connection_id, external_event_id, schema_version, now, payload_digest, raw_payload_ref))
            if not replayed:
                self._audit(context.tenant_id, context.user_id, 'inbox.received', message.id, 'accepted', {'payload_digest': payload_digest})
                self._inbox_outbox(message, 'inbox.process_requested', 'process', now)
            return message, replayed

    def _inbox_outbox(self, message: InboxMessage, topic: str, suffix: str, now: datetime) -> None:
        self.repo.append_outbox(OutboxEvent(str(uuid4()), message.tenant_id, topic, message.id,
            {'inbox_id': message.id, 'payload_digest': message.payload_digest}, f'inbox:{message.id}:{suffix}', OutboxState.PENDING, now))

    def process_inbound(self, context: TenantContext, inbox_id: str, expected_version: int) -> tuple[InboxMessage, bool]:
        """Accept a receipt for downstream routing; never execute an order."""
        with self.repo.transaction():
            self.require(context, Capability.TENANT_ADMIN)
            if type(expected_version) is not int or expected_version < 1:
                raise ConflictError('invalid expected version')
            message = self.repo.get_inbox(context.tenant_id, inbox_id)
            if message.state == InboxState.PROCESSED:
                return message, True
            now = self._clock()
            message = self.repo.mark_inbox_processed(context.tenant_id, inbox_id, expected_version, now)
            self._inbox_outbox(message, 'inbound.accepted', 'processed', now)
            self._audit(context.tenant_id, context.user_id, 'inbox.processed', message.id, 'accepted', {'payload_digest': message.payload_digest})
            return message, False

    def poll_demo_connection(self, context: TenantContext, provider: str, connection_id: str,
                             expected_checkpoint_version: int, adapter: Any,
                             overlap_from: datetime | None = None) -> Any:
        """Ingest exactly one local DEMO page with payload/receipt/cursor atomicity."""
        from .ingestion import poll_demo_connection
        return poll_demo_connection(self, context, provider, connection_id,
                                    expected_checkpoint_version, adapter, overlap_from)

    def get_normalized_payload(self, context: TenantContext, immutable_ref: str) -> Any:
        """Read one immutable payload only through the authenticated tenant context."""
        self.require(context, Capability.TENANT_ADMIN)
        self._inbound_identifier(immutable_ref)
        return self.repo.get_normalized_payload(context.tenant_id, immutable_ref)

    def normalized_payloads_for(self, context: TenantContext) -> tuple[Any, ...]:
        self.require(context, Capability.TENANT_ADMIN)
        return self.repo.normalized_payloads_for(context.tenant_id)

    def poll_checkpoint(self, context: TenantContext, provider: str, connection_id: str) -> Any:
        self.require(context, Capability.TENANT_ADMIN)
        return self.repo.get_poll_checkpoint(context.tenant_id, provider, connection_id)

    def ingest_order(self, context: TenantContext, channel_id: str, payload_ref: str,
                     idempotency_key: str | None = None) -> tuple[Any, bool]:
        from .orders import ingest_order
        return ingest_order(self, context, channel_id, payload_ref, idempotency_key)

    def propose_routing(self, context: TenantContext, order_id: str, supplier_options: Mapping[str, Any],
                        expected_order_version: int = 1) -> tuple[Any, ...]:
        from .orders import propose_routing
        return propose_routing(self, context, order_id, supplier_options, expected_order_version)

    def order(self, context: TenantContext, order_id: str) -> Any:
        self.require(context, Capability.TENANT_ADMIN)
        return self.repo.get_channel_order(context.tenant_id, order_id)

    def order_lines(self, context: TenantContext, order_id: str) -> tuple[Any, ...]:
        self.require(context, Capability.TENANT_ADMIN)
        return self.repo.order_lines_for(context.tenant_id, order_id)

    def purchase_orders(self, context: TenantContext, order_id: str) -> tuple[Any, ...]:
        self.require(context, Capability.TENANT_ADMIN)
        return self.repo.purchase_orders_for(context.tenant_id, order_id)

    def approve_demo_po(self, context: TenantContext, po_id: str, approve: bool, reason: str) -> Any:
        from .order02 import approve_demo_po
        return approve_demo_po(self, context, po_id, approve, reason)

    def submit_demo_po(self, context: TenantContext, po_id: str) -> Any:
        from .order02 import submit_demo_po
        return submit_demo_po(self, context, po_id)

    def reconcile_demo_po(self, context: TenantContext, po_id: str, response: Mapping[str, Any]) -> tuple[Any, bool]:
        from .order02 import reconcile_demo_po
        return reconcile_demo_po(self, context, po_id, response)

    def request_demo_cancel(self, context: TenantContext, order_id: str, reason: str,
                            expected_order_version: int) -> tuple[Any, bool]:
        from .order03 import request_demo_cancel
        return request_demo_cancel(self, context, order_id, reason, expected_order_version)

    def ingest_demo_tracking(self, context: TenantContext, order_line_id: str, tracking_key: str,
                             status: str, observed_at: datetime | None = None,
                             expected_line_version: int | None = None) -> tuple[Any, bool]:
        from .order03 import ingest_demo_tracking
        return ingest_demo_tracking(self, context, order_line_id, tracking_key, status,
                                    observed_at, expected_line_version)

    def tracking_for(self, context: TenantContext, order_line_id: str) -> tuple[Any, ...]:
        self.require(context, Capability.TENANT_ADMIN)
        return self.repo.tracking_for(context.tenant_id, order_line_id)

    def open_demo_claim(self, context: TenantContext, order_id: str, claim_type: str,
                        amount_minor: int, idempotency_key: str) -> tuple[Any, bool]:
        from .claim01 import open_demo_claim
        return open_demo_claim(self, context, order_id, claim_type, amount_minor, idempotency_key)

    def record_demo_claim_status(self, context: TenantContext, claim_id: str, status_kind: str,
                                 status: str, expected_version: int) -> tuple[Any, bool]:
        from .claim01 import record_demo_claim_status
        return record_demo_claim_status(self, context, claim_id, status_kind, status, expected_version)

    def claim(self, context: TenantContext, claim_id: str) -> Any:
        self.require(context, Capability.TENANT_ADMIN)
        return self.repo.get_claim(context.tenant_id, claim_id)

    def claim_observations(self, context: TenantContext, claim_id: str) -> tuple[Any, ...]:
        self.require(context, Capability.TENANT_ADMIN)
        return self.repo.claim_observations_for(context.tenant_id, claim_id)

    def import_demo_settlement(self, context: TenantContext, channel_id: str, period: str,
                               rows: Sequence[Mapping[str, Any]], idempotency_key: str) -> tuple[Any, bool]:
        from .finance01 import import_demo_settlement
        return import_demo_settlement(self, context, channel_id, period, rows, idempotency_key)

    def ingest_demo_catalog(self, context: TenantContext, supplier_id: str,
                            rows: Sequence[Mapping[str, Any]], idempotency_key: str) -> tuple[Any, bool]:
        from .catalog import ingest_demo_catalog
        return ingest_demo_catalog(self, context, supplier_id, rows, idempotency_key)

    def project_demo_offer(self, context: TenantContext, canonical_product_id: str,
                           channel_id: str, price_minor: int | None = None) -> tuple[Any, bool]:
        from .catalog import project_demo_offer
        return project_demo_offer(self, context, canonical_product_id, channel_id, price_minor)

    def catalog_snapshots(self, context: TenantContext, import_id: str) -> tuple[Any, ...]:
        self.require(context, Capability.TENANT_ADMIN)
        return self.repo.catalog_snapshots_for(context.tenant_id, import_id)

    def canonical_product(self, context: TenantContext, product_id: str) -> Any:
        self.require(context, Capability.TENANT_ADMIN)
        return self.repo.get_canonical_product(context.tenant_id, product_id)

    def product_lineage(self, context: TenantContext, product_id: str) -> tuple[Any, ...]:
        self.require(context, Capability.TENANT_ADMIN)
        return self.repo.lineage_for(context.tenant_id, product_id)

    def channel_offers(self, context: TenantContext, product_id: str) -> tuple[Any, ...]:
        self.require(context, Capability.TENANT_ADMIN)
        return self.repo.channel_offers_for(context.tenant_id, product_id)

    def settlement_batch(self, context: TenantContext, batch_id: str) -> Any:
        self.require(context, Capability.TENANT_ADMIN)
        return self.repo.get_settlement_batch(context.tenant_id, batch_id)

    def settlement_lines(self, context: TenantContext, batch_id: str) -> tuple[Any, ...]:
        self.require(context, Capability.TENANT_ADMIN)
        return self.repo.settlement_lines_for(context.tenant_id, batch_id)

    def realized_profits(self, context: TenantContext, batch_id: str) -> tuple[Any, ...]:
        self.require(context, Capability.TENANT_ADMIN)
        return self.repo.realized_profits_for(context.tenant_id, batch_id)

    def approval_inbox(self, context: TenantContext) -> dict[str, Any]:
        from .approvals import approval_inbox
        return approval_inbox(self, context)

    def approval_detail(self, context: TenantContext, approval_id: str) -> dict[str, Any]:
        from .approvals import approval_detail
        return approval_detail(self, context, approval_id)

    def decide_approval(self, context: TenantContext, approval_id: str, approve: bool,
                        reason: str, confirmation_nonce: str) -> Approval:
        from .approvals import decide_approval
        return decide_approval(self, context, approval_id, approve, reason, confirmation_nonce)

    def dashboard_snapshot(self, context: TenantContext, *, project_root: str | None = None) -> dict[str, Any]:
        """Return a tenant-scoped, read-only dashboard projection.

        Dashboard reads are available to active tenant members and never call a
        model or external service.  The membership check happens before the
        projection queries any tenant-owned rows.
        """
        self._membership(context)
        return DashboardProjection(self.repo, project_root=project_root).snapshot(context, now=self._clock())

    @staticmethod
    def _intent_digest(command: Command, approval: Approval, policy_version: int, target_version: int) -> str:
        return _strict_intent_digest({'kind': command.kind.value, 'target_ref': command.target_ref, 'payload': command.payload,
            'evidence': approval.evidence, 'expires_at': approval.expires_at.isoformat(),
            'policy_version': policy_version, 'target_version': target_version})

    def _check_intent(self, command: Command, approval: Approval, intent: ApprovalIntent) -> None:
        if self._intent_digest(command, approval, intent.policy_version, intent.target_version) != intent.canonical_digest:
            raise ConflictError('approval intent changed; new approval required')

    def request_approval(self, context: TenantContext, kind: ApprovalKind, target_ref: str,
                         payload: Mapping[str, Any], idempotency_key: str, policy_version: int,
                         target_version: int, evidence: Sequence[Mapping[str, Any]] = ()) -> tuple[Command, Approval]:
        with self.repo.transaction():
            if any(type(v) is not int or v < 1 for v in (policy_version, target_version)):
                raise ConflictError('positive policy and target versions required')
            _strict_intent_digest({'payload': payload, 'evidence': evidence, 'target_ref': target_ref})
            command, approval = self.create_command(context, kind, target_ref, payload, idempotency_key, evidence)
            # Include the newly supplied evidence on replay, not merely the stored evidence.
            digest = _strict_intent_digest({'kind': kind.value, 'target_ref': target_ref, 'payload': payload,
                'evidence': evidence, 'expires_at': approval.expires_at.isoformat(),
                'policy_version': policy_version, 'target_version': target_version})
            prior = self.repo.get_approval_intent(context.tenant_id, command.id)
            if prior:
                if prior.canonical_digest != digest:
                    raise ConflictError('idempotent approval intent mismatch')
                self._check_intent(command, approval, prior)
            else:
                if approval.state != ApprovalState.PENDING or self._intent_digest(command, approval, policy_version, target_version) != digest:
                    raise ConflictError('cannot attach changed intent to existing approval')
                self.repo.save_approval_intent(ApprovalIntent(context.tenant_id, command.id, digest, policy_version, target_version, self._clock()))
            return command, approval

    def prepare_execution(self, context: TenantContext, command_id: str, policy_version: int,
                          target_version: int) -> tuple[ExecutionPreparation, bool]:
        with self.repo.transaction():
            self._membership(context)
            command = self.repo.get_command(context.tenant_id, command_id)
            capability = APPROVAL_CAPABILITY[command.kind]
            self.require(context, capability)
            approval = self.repo.get_approval_for_command(context.tenant_id, command_id)
            intent = self.repo.get_approval_intent(context.tenant_id, command_id)
            if intent is None:
                raise ConflictError('immutable approval intent required')
            if command.state != CommandState.APPROVED or approval.state != ApprovalState.APPROVED:
                raise ConflictError('approval is not executable')
            now = self._clock()
            if now >= approval.expires_at:
                raise ConflictError('approval expired')
            if any(type(v) is not int or v < 1 for v in (policy_version, target_version)) or (policy_version, target_version) != (intent.policy_version, intent.target_version):
                raise ConflictError('policy/target version changed; new approval required')
            self._check_intent(command, approval, intent)
            if not approval.decided_by:
                raise ConflictError('approval has no deciding member')
            approver = self.context_for(context.tenant_id, approval.decided_by)
            self.require(approver, capability)
            prior = self.repo.get_execution_preparation(context.tenant_id, command_id)
            if prior:
                return prior, True
            result = ExecutionPreparation(str(uuid4()), context.tenant_id, command_id, intent.canonical_digest, context.user_id, now)
            self.repo.save_execution_preparation(result)
            self._audit(context.tenant_id, context.user_id, 'execution.prepared', command_id, 'accepted', {'intent_digest': intent.canonical_digest})
            self.repo.append_outbox(OutboxEvent(str(uuid4()), context.tenant_id, 'execution.prepared', command_id,
                {'command_id': command_id, 'preparation_id': result.id, 'intent_digest': intent.canonical_digest},
                f'execution.prepared:{command_id}', OutboxState.PENDING, now))
            return result, False

    def bootstrap_tenant(self, legal_name: str, master_email: str) -> TenantContext:
        with self.repo.transaction():
            now = self._clock()
            tenant = Tenant(str(uuid4()), legal_name, now)
            user = User(str(uuid4()), master_email.strip().lower(), now)
            self.repo.add_tenant(tenant)
            self.repo.add_user(user)
            membership = Membership(tenant.id, user.id, frozenset({Role.MASTER}))
            self.repo.save_membership(membership)
            # Seed explicit, persisted slots so a fresh tenant dashboard shows
            # root/PM/worker status as UNKNOWN until a real heartbeat arrives;
            # this is evidence-neutral and survives a restart.
            if hasattr(self.repo, "save_agent_status"):
                for agent_id, role in (("root", "orchestrator"), ("codex_pm_luna", "pm"), ("worker", "worker")):
                    self.repo.save_agent_status(AgentStatusSnapshot(
                        tenant.id, agent_id, role, AgentState.UNKNOWN, None, None, None, None,
                        None, None, None, None, None, False, now,
                    ))
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
        with self.repo.transaction():
            self.require(context, Capability.TENANT_ADMIN)
            role_set = frozenset(roles)
            if not role_set or Role.MASTER in role_set:
                raise ConflictError("delegated member requires non-master role(s)")
            active_count = sum(1 for m in self.repo.tenant_memberships(context.tenant_id) if m.active)
            if active_count >= 3:
                raise ConflictError("a tenant supports one master plus two active members")
            normalized = email.strip().lower()
            user = self.repo.find_user_by_email(normalized)
            if user is None:
                user = User(str(uuid4()), normalized, self._clock())
                self.repo.add_user(user)
            try:
                prior = self.repo.get_membership(context.tenant_id, user.id)
            except NotFoundError:
                prior = None
            version = prior.version + 1 if prior else 1
            membership = Membership(context.tenant_id, user.id, role_set, True, version)
            self.repo.save_membership(membership)
            self._audit(context.tenant_id, context.user_id, "membership.add", user.id, "succeeded", {"roles": sorted(role.value for role in role_set)})
        return TenantContext(context.tenant_id, user.id, version)

    def change_member_roles(self, context: TenantContext, user_id: str, roles: Sequence[Role]) -> None:
        with self.repo.transaction():
            self.require(context, Capability.TENANT_ADMIN)
            membership = self.repo.get_membership(context.tenant_id, user_id)
            if Role.MASTER in membership.roles:
                raise ConflictError("master role cannot be delegated or changed")
            role_set = frozenset(roles)
            if not role_set or Role.MASTER in role_set:
                raise ConflictError("invalid delegated roles")
            membership.roles = role_set
            membership.version += 1
            self.repo.save_membership(membership)
            self._audit(context.tenant_id, context.user_id, "membership.roles_changed", user_id, "succeeded", {"roles": sorted(role.value for role in role_set)})

    def revoke_member(self, context: TenantContext, user_id: str) -> None:
        with self.repo.transaction():
            self.require(context, Capability.TENANT_ADMIN)
            membership = self.repo.get_membership(context.tenant_id, user_id)
            if Role.MASTER in membership.roles:
                raise ConflictError("master membership cannot be revoked")
            membership.active = False
            membership.version += 1
            self.repo.save_membership(membership)
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
        with self.repo.transaction():
            # Human users may propose only work within their delegated boundary.
            self.require(context, APPROVAL_CAPABILITY[kind])
            if not idempotency_key.strip():
                raise ConflictError("idempotency key is required")
            digest = _digest({"kind": kind, "target_ref": target_ref, "payload": payload})
            existing_id = self.repo.command_id_for_key(context.tenant_id, idempotency_key)
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
            self.repo.save_command(command)
            self.repo.save_approval(approval)
            self.repo.bind_command_key(context.tenant_id, idempotency_key, command.id)
            self._audit(context.tenant_id, context.user_id, "command.create", command.id, "accepted", {"payload_digest": digest})
            self.repo.append_outbox(OutboxEvent(
                id=str(uuid4()), tenant_id=context.tenant_id, topic="approval.requested",
                aggregate_ref=command.id, payload={"command_id": command.id, "approval_id": approval.id},
                idempotency_key=f"approval.requested:{command.id}", state=OutboxState.PENDING,
                created_at=now,
            ))
            return command, approval

    def decide(self, context: TenantContext, command_id: str, approve: bool, reason: str) -> Approval:
        # Record boundary probes in their own committed unit before re-raising.
        try:
            self.repo.get_command(context.tenant_id, command_id)
        except AuthorizationError:
            with self.repo.transaction():
                self._audit(
                    context.tenant_id, context.user_id, "command.cross_tenant_access",
                    "redacted", "blocked", {},
                )
            raise
        expired = False
        result: Approval | None = None
        with self.repo.transaction():
            result, expired = self._decide(context, command_id, approve, reason)
        if expired:
            raise ConflictError("approval expired")
        assert result is not None
        return result

    def _decide(self, context: TenantContext, command_id: str, approve: bool, reason: str) -> tuple[Approval, bool]:
        command = self.repo.get_command(context.tenant_id, command_id)
        approval = self.repo.get_approval_for_command(context.tenant_id, command_id)
        self.require(context, APPROVAL_CAPABILITY[command.kind])
        intent = self.repo.get_approval_intent(context.tenant_id, command_id)
        if intent is not None:
            self._check_intent(command, approval, intent)
        if approval.state != ApprovalState.PENDING:
            raise ConflictError("approval is no longer pending")
        now = self._clock()
        if now >= approval.expires_at:
            approval.state = ApprovalState.EXPIRED
            command.state = CommandState.EXPIRED
            self._audit(context.tenant_id, context.user_id, "approval.expire", approval.id, "blocked", {})
            self.repo.save_approval(approval)
            self.repo.save_command(command)
            self.repo.append_outbox(OutboxEvent(
                id=str(uuid4()), tenant_id=context.tenant_id, topic="approval.expired",
                aggregate_ref=command.id, payload={"command_id": command.id, "approval_id": approval.id},
                idempotency_key=f"approval.expired:{approval.id}", state=OutboxState.PENDING, created_at=now,
            ))
            return approval, True
        approval.state = ApprovalState.APPROVED if approve else ApprovalState.REJECTED
        command.state = CommandState.APPROVED if approve else CommandState.REJECTED
        approval.decided_by = context.user_id
        approval.decision_reason = reason
        self.repo.save_approval(approval)
        self.repo.save_command(command)
        self._audit(context.tenant_id, context.user_id, "approval.decide", approval.id, "succeeded", {"decision": approval.state})
        self.repo.append_outbox(OutboxEvent(
            id=str(uuid4()), tenant_id=context.tenant_id, topic="approval.decided",
            aggregate_ref=command.id,
            payload={"command_id": command.id, "approval_id": approval.id, "decision": approval.state.value},
            idempotency_key=f"approval.decided:{approval.id}", state=OutboxState.PENDING, created_at=now,
        ))
        return approval, False

    def supersede(
        self,
        context: TenantContext,
        command_id: str,
        changed_payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> tuple[Command, Approval]:
        with self.repo.transaction():
            old = self.repo.get_command(context.tenant_id, command_id)
            old_approval = self.repo.get_approval_for_command(context.tenant_id, command_id)
            changed_digest = _digest({"kind": old.kind, "target_ref": old.target_ref, "payload": changed_payload})
            if changed_digest == old.payload_digest:
                raise ConflictError("no material change")
            if old_approval.state not in {ApprovalState.PENDING, ApprovalState.APPROVED}:
                raise ConflictError("command cannot be superseded")
            old_approval.state = ApprovalState.SUPERSEDED
            old.state = CommandState.SUPERSEDED
            self.repo.save_approval(old_approval)
            self.repo.save_command(old)
            new_command, new_approval = self.create_command(
                context, old.kind, old.target_ref, changed_payload, idempotency_key, old_approval.evidence
            )
            new_command.supersedes_id = old.id
            self.repo.save_command(new_command)
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
        events = self.repo.audits_for(tenant_id)
        previous = events[-1].event_hash if events else None
        event_id, correlation_id, now = str(uuid4()), str(uuid4()), self._clock()
        material = {
            "id": event_id, "tenant_id": tenant_id, "occurred_at": now,
            "actor_ref": actor_ref, "action": action, "target_ref": target_ref,
            "outcome": outcome, "correlation_id": correlation_id,
            "metadata": dict(metadata), "prev_hash": previous,
        }
        self.repo.append_audit(AuditEvent(event_hash=_digest(material), **material))
