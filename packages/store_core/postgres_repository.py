"""PostgreSQL repository with row-level security (RLS) for multi-tenant isolation.

Uses ``set_config('app.current_tenant', ...)`` to enforce cross-tenant
blocking. The SQL fixture (``tests/contracts/postgres_rls_fixture.sql``)
creates the schema, enables RLS, and grants ``pg_signal_backend`` so the
test can assert ``pg_backend_pid()`` visibility.

``TenantAwarePostgresRepository`` is a thin adapter over the ``Repository``
interface. It delegates to a ``psycopg2`` connection and sets the tenant
via ``SET app.current_tenant = %(tenant)s`` before each operation.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterator

import psycopg2
import psycopg2.extras

from .domain import (
    Approval,
    ApprovalKind,
    ApprovalState,
    AuditEvent,
    Command,
    CommandState,
    AdapterCapability,
    AdapterCapabilityManifest,
    AgentState,
    AgentStatusSnapshot,
    InboxMessage,
    InboxState,
    Membership,
    OutboxEvent,
    OutboxState,
    Role,
    Tenant,
    User,
    ApprovalIntent,
    ExecutionPreparation,
    DemoExecutionControl,
    ExecutionAttempt,
    AttemptObservation,
    AttemptState,
    NormalizedInboundPayload,
    AdapterPollCheckpoint,
    ChannelOrder,
    OrderLine,
    RoutingDecision,
    SupplierPurchaseOrder,
    PurchaseLine,
    ChannelOrderState,
    RoutingState,
    PurchaseOrderState,
    TrackingObservation,
    DemoClaim,
    ClaimStatusObservation,
    ClaimStatus,
    DemoSettlementBatch,
    DemoSettlementLine,
    DemoRealizedProfit,
    SettlementStatus,
    DemoCatalogImport,
    DemoCatalogSnapshot,
    DemoCanonicalProduct,
    DemoProductLineage,
    DemoChannelOffer,
    DemoToolCommand,
    DemoAgentRun,
    DemoByokReference,
    DemoBudgetPolicy,
    DemoBudgetLedgerEntry,
    DemoNotificationPreference,
    DemoNotificationDelivery,
    DemoIncidentAcknowledgement,
    DemoStopControl,
    DemoBackupManifest,
    DemoInventorySnapshot,
    DemoPriceProjection,
    BrowserSession,
    ApprovalConfirmationNonce,
)
from .errors import ConflictError, NotFoundError, TenantBoundaryError
from .domain import DemoBudgetRequest


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── minimal domain helpers used by the postgres repo ──────────────────


def _load_tenant(row: dict) -> Tenant:
    return Tenant(id=row["id"], legal_name=row["legal_name"], created_at=row["created_at"])


def _load_user(row: dict) -> User:
    return User(id=row["id"], email=row["email"], created_at=row["created_at"])


def _load_membership(row: dict) -> Membership:
    return Membership(
        tenant_id=row["tenant_id"],
        user_id=row["user_id"],
        roles=[Role(r) for r in json.loads(row["roles_json"])],
        active=bool(row["active"]),
        version=row["version"],
    )


def _load_command(row: dict) -> Command:
    return Command(
        id=row["id"],
        state=CommandState(row["state"]),
        payload=row["payload"],
        created_at=row["created_at"],
    )


def _load_audit_event(row: dict) -> AuditEvent:
    return AuditEvent(
        id=row["id"],
        tenant_id=row["tenant_id"],
        actor=row["actor"],
        action=row["action"],
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        details=row["details"] or {},
        occurred_at=row["occurred_at"],
    )


def _load_agent_state(row: dict) -> AgentState:
    return AgentState(
        tenant_id=row["tenant_id"],
        agent_id=row["agent_id"],
        status=row["status"],
        run_log=json.loads(row["run_log"]) if row["run_log"] else [],
        version=row["version"],
        updated_at=row["updated_at"],
    )


def _load_agent_status_snapshot(row: dict) -> AgentStatusSnapshot:
    return AgentStatusSnapshot(
        tenant_id=row["tenant_id"],
        agent_id=row["agent_id"],
        status=row["status"],
        last_heartbeat=row["last_heartbeat"],
    )


def _load_adapter_capability(row: dict) -> AdapterCapability:
    return AdapterCapability(
        adapter_id=row["adapter_id"],
        capability=row["capability"],
        manifest=json.loads(row["manifest_json"]) if row["manifest_json"] else {},
        version=row["version"],
    )


def _load_adapter_capability_manifest(row: dict) -> AdapterCapabilityManifest:
    return AdapterCapabilityManifest(
        adapter_id=row["adapter_id"],
        capabilities=json.loads(row["capabilities_json"]) if row["capabilities_json"] else [],
        version=row["version"],
    )


def _load_agent_poll_checkpoint(row: dict) -> AdapterPollCheckpoint:
    return AdapterPollCheckpoint(
        adapter_id=row["adapter_id"],
        last_cursor=row["last_cursor"],
        updated_at=row["updated_at"],
    )


def _load_inbox_message(row: dict) -> InboxMessage:
    return InboxMessage(
        id=row["id"],
        tenant_id=row["tenant_id"],
        user_id=row["user_id"],
        from_actor=row["from_actor"],
        body=json.loads(row["body_json"]) if row["body_json"] else {},
        created_at=row["created_at"],
        read=bool(row["read"]),
    )


def _load_inbox_state(row: dict) -> InboxState:
    return InboxState(
        tenant_id=row["tenant_id"],
        user_id=row["user_id"],
        messages=[_load_inbox_message(m) for m in json.loads(row["messages_json"])],
        version=row["version"],
    )


def _load_outbox_event(row: dict) -> OutboxEvent:
    return OutboxEvent(
        id=row["id"],
        tenant_id=row["tenant_id"],
        kind=row["kind"],
        payload=json.loads(row["payload_json"]) if row["payload_json"] else {},
        state=OutboxState(row["state"]),
        created_at=row["created_at"],
        delivered_at=row["delivered_at"],
    )


def _load_approval(row: dict) -> Approval:
    return Approval(
        id=row["id"],
        kind=ApprovalKind(row["kind"]),
        state=ApprovalState(row["state"]),
        requestor_id=row["requestor_id"],
        approver_id=row["approver_id"],
        target_type=row["target_type"],
        target_id=row["target_id"],
        payload=json.loads(row["payload_json"]) if row["payload_json"] else {},
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


def _load_approval_intent(row: dict) -> ApprovalIntent:
    return ApprovalIntent(
        id=row["id"],
        kind=ApprovalKind(row["kind"]),
        requestor_id=row["requestor_id"],
        target_type=row["target_type"],
        target_id=row["target_id"],
        payload=json.loads(row["payload_json"]) if row["payload_json"] else {},
        created_at=row["created_at"],
    )


def _load_execution_preparation(row: dict) -> ExecutionPreparation:
    return ExecutionPreparation(
        attempt_id=row["attempt_id"],
        tenant_id=row["tenant_id"],
        agent_id=row["agent_id"],
        budget_policy_id=row["budget_policy_id"],
        tool_use_plan=json.loads(row["tool_use_plan"]) if row["tool_use_plan"] else [],
        budget_request=json.loads(row["budget_request"]) if row["budget_request"] else {},
        created_at=row["created_at"],
    )


def _load_demo_execution_control(row: dict) -> DemoExecutionControl:
    return DemoExecutionControl(
        task_id=row["task_id"],
        tenant_id=row["tenant_id"],
        status=row["status"],
        max_turns=row["max_turns"],
        budget_limit=row["budget_limit"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _load_execution_attempt(row: dict) -> ExecutionAttempt:
    return ExecutionAttempt(
        id=row["id"],
        task_id=row["task_id"],
        tenant_id=row["tenant_id"],
        agent_id=row["agent_id"],
        turn_number=row["turn_number"],
        status=row["status"],
        tool_calls=json.loads(row["tool_calls"]) if row["tool_calls"] else [],
        results=json.loads(row["results"]) if row["results"] else [],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


def _load_attempt_observation(row: dict) -> AttemptObservation:
    return AttemptObservation(
        attempt_id=row["attempt_id"],
        status=row["status"],
        details=json.loads(row["details"]) if row["details"] else {},
        occurred_at=row["occurred_at"],
    )


def _load_channel_order(row: dict) -> ChannelOrder:
    return ChannelOrder(
        id=row["id"],
        tenant_id=row["tenant_id"],
        channel=row["channel"],
        order_id=row["order_id"],
        lines=json.loads(row["lines_json"]) if row["lines_json"] else [],
        total_amount=row["total_amount"],
        currency=row["currency"],
        state=ChannelOrderState(row["state"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _load_order_line(row: dict) -> OrderLine:
    return OrderLine(
        product_id=row["product_id"],
        quantity=row["quantity"],
        unit_price=row["unit_price"],
    )


def _load_routing_decision(row: dict) -> RoutingDecision:
    return RoutingDecision(
        order_id=row["order_id"],
        tenant_id=row["tenant_id"],
        channel=row["channel"],
        decision=row["decision"],
        supplier_id=row["supplier_id"],
        reason=row["reason"],
        created_at=row["created_at"],
    )


def _load_supplier_purchase_order(row: dict) -> SupplierPurchaseOrder:
    return SupplierPurchaseOrder(
        id=row["id"],
        tenant_id=row["tenant_id"],
        supplier_id=row["supplier_id"],
        order_id=row["order_id"],
        lines=json.loads(row["lines_json"]) if row["lines_json"] else [],
        total_amount=row["total_amount"],
        currency=row["currency"],
        state=PurchaseOrderState(row["state"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _load_purchase_line(row: dict) -> PurchaseLine:
    return PurchaseLine(
        product_id=row["product_id"],
        quantity=row["quantity"],
        unit_price=row["unit_price"],
    )


def _load_tracking_observation(row: dict) -> TrackingObservation:
    return TrackingObservation(
        order_id=row["order_id"],
        tenant_id=row["tenant_id"],
        status=row["status"],
        location=row["location"],
        details=json.loads(row["details"]) if row["details"] else {},
        observed_at=row["observed_at"],
    )


def _load_demo_claim(row: dict) -> DemoClaim:
    return DemoClaim(
        id=row["id"],
        tenant_id=row["tenant_id"],
        order_id=row["order_id"],
        reason=row["reason"],
        amount=row["amount"],
        status=ClaimStatus(row["status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _load_claim_status_observation(row: dict) -> ClaimStatusObservation:
    return ClaimStatusObservation(
        claim_id=row["claim_id"],
        from_status=ClaimStatus(row["from_status"]),
        to_status=ClaimStatus(row["to_status"]),
        observed_at=row["observed_at"],
    )


def _load_demo_settlement_batch(row: dict) -> DemoSettlementBatch:
    return DemoSettlementBatch(
        id=row["id"],
        tenant_id=row["tenant_id"],
        channel=row["channel"],
        total_amount=row["total_amount"],
        fee_amount=row["fee_amount"],
        net_amount=row["net_amount"],
        status=SettlementStatus(row["status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _load_demo_settlement_line(row: dict) -> DemoSettlementLine:
    return DemoSettlementLine(
        batch_id=row["batch_id"],
        order_id=row["order_id"],
        gross_amount=row["gross_amount"],
        fee_amount=row["fee_amount"],
        net_amount=row["net_amount"],
    )


def _load_demo_realized_profit(row: dict) -> DemoRealizedProfit:
    return DemoRealizedProfit(
        id=row["id"],
        tenant_id=row["tenant_id"],
        order_id=row["order_id"],
        revenue=row["revenue"],
        cost=row["cost"],
        profit=row["profit"],
        realized_at=row["realized_at"],
    )


def _load_demo_catalog_import(row: dict) -> DemoCatalogImport:
    return DemoCatalogImport(
        id=row["id"],
        tenant_id=row["tenant_id"],
        source=row["source"],
        product_count=row["product_count"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _load_demo_catalog_snapshot(row: dict) -> DemoCatalogSnapshot:
    return DemoCatalogSnapshot(
        id=row["id"],
        tenant_id=row["tenant_id"],
        import_id=row["import_id"],
        snapshot=json.loads(row["snapshot_json"]) if row["snapshot_json"] else {},
        created_at=row["created_at"],
    )


def _load_demo_canonical_product(row: dict) -> DemoCanonicalProduct:
    return DemoCanonicalProduct(
        id=row["id"],
        tenant_id=row["tenant_id"],
        canonical_id=row["canonical_id"],
        attributes=json.loads(row["attributes_json"]) if row["attributes_json"] else {},
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _load_demo_product_lineage(row: dict) -> DemoProductLineage:
    return DemoProductLineage(
        canonical_id=row["canonical_id"],
        tenant_id=row["tenant_id"],
        source_tenant_id=row["source_tenant_id"],
        product_id=row["product_id"],
        mapped_at=row["mapped_at"],
    )


def _load_demo_channel_offer(row: dict) -> DemoChannelOffer:
    return DemoChannelOffer(
        id=row["id"],
        tenant_id=row["tenant_id"],
        channel=row["channel"],
        product_id=row["product_id"],
        price=row["price"],
        currency=row["currency"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _load_demo_tool_command(row: dict) -> DemoToolCommand:
    return DemoToolCommand(
        id=row["id"],
        tenant_id=row["tenant_id"],
        agent_id=row["agent_id"],
        tool_name=row["tool_name"],
        arguments=json.loads(row["arguments_json"]) if row["arguments_json"] else {},
        result=json.loads(row["result_json"]) if row["result_json"] else {},
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


def _load_demo_agent_run(row: dict) -> DemoAgentRun:
    return DemoAgentRun(
        id=row["id"],
        tenant_id=row["tenant_id"],
        agent_id=row["agent_id"],
        status=row["status"],
        steps=json.loads(row["steps_json"]) if row["steps_json"] else [],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


def _load_demo_byok_reference(row: dict) -> DemoByokReference:
    return DemoByokReference(
        id=row["id"],
        tenant_id=row["tenant_id"],
        key_type=row["key_type"],
        key_hint=row["key_hint"],
        created_at=row["created_at"],
    )


def _load_demo_budget_policy(row: dict) -> DemoBudgetPolicy:
    return DemoBudgetPolicy(
        id=row["id"],
        tenant_id=row["tenant_id"],
        policy_name=row["policy_name"],
        limits=json.loads(row["limits_json"]) if row["limits_json"] else {},
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _load_demo_budget_ledger_entry(row: dict) -> DemoBudgetLedgerEntry:
    return DemoBudgetLedgerEntry(
        id=row["id"],
        tenant_id=row["tenant_id"],
        policy_id=row["policy_id"],
        amount=row["amount"],
        description=row["description"],
        created_at=row["created_at"],
    )


def _load_demo_notification_preference(row: dict) -> DemoNotificationPreference:
    return DemoNotificationPreference(
        id=row["id"],
        tenant_id=row["tenant_id"],
        user_id=row["user_id"],
        channels=json.loads(row["channels_json"]) if row["channels_json"] else [],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _load_demo_notification_delivery(row: dict) -> DemoNotificationDelivery:
    return DemoNotificationDelivery(
        id=row["id"],
        tenant_id=row["tenant_id"],
        user_id=row["user_id"],
        channel=row["channel"],
        subject=row["subject"],
        body=json.loads(row["body_json"]) if row["body_json"] else {},
        sent_at=row["sent_at"],
    )


def _load_demo_incident_acknowledgement(row: dict) -> DemoIncidentAcknowledgement:
    return DemoIncidentAcknowledgement(
        id=row["id"],
        tenant_id=row["tenant_id"],
        incident_id=row["incident_id"],
        acknowledged_by=row["acknowledged_by"],
        acknowledged_at=row["acknowledged_at"],
    )


def _load_demo_stop_control(row: dict) -> DemoStopControl:
    return DemoStopControl(
        id=row["id"],
        tenant_id=row["tenant_id"],
        agent_id=row["agent_id"],
        stopped_at=row["stopped_at"],
    )


def _load_demo_backup_manifest(row: dict) -> DemoBackupManifest:
    return DemoBackupManifest(
        id=row["id"],
        tenant_id=row["tenant_id"],
        backup_path=row["backup_path"],
        created_at=row["created_at"],
    )


def _load_demo_inventory_snapshot(row: dict) -> DemoInventorySnapshot:
    return DemoInventorySnapshot(
        tenant_id=row["tenant_id"],
        product_id=row["product_id"],
        quantity=row["quantity"],
        updated_at=row["updated_at"],
    )


def _load_demo_price_projection(row: dict) -> DemoPriceProjection:
    return DemoPriceProjection(
        tenant_id=row["tenant_id"],
        product_id=row["product_id"],
        projected_price=row["projected_price"],
        currency=row["currency"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
    )


def _load_browser_session(row: dict) -> BrowserSession:
    return BrowserSession(
        id=row["id"],
        tenant_id=row["tenant_id"],
        user_id=row["user_id"],
        session_data=json.loads(row["session_data_json"]) if row["session_data_json"] else {},
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


def _load_approval_confirmation_nonce(row: dict) -> ApprovalConfirmationNonce:
    return ApprovalConfirmationNonce(
        id=row["id"],
        approval_id=row["approval_id"],
        nonce=row["nonce"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        consumed=bool(row["consumed"]),
    )


def _load_budget_request(row: dict) -> DemoBudgetRequest:
    return DemoBudgetRequest(
        id=row["id"],
        tenant_id=row["tenant_id"],
        policy_id=row["policy_id"],
        amount=row["amount"],
        description=row["description"],
        created_at=row["created_at"],
    )


# ── PostgreSQL repository ─────────────────────────────────────────────


class TenantAwarePostgresRepository:
    """Repository backed by PostgreSQL with RLS enforcement.

    Every public method sets ``app.current_tenant`` before issuing queries,
    so that RLS policies can filter by the ``tenant_id`` column. If a
    query would return rows from another tenant, RLS silently excludes
    them — the repository translates "zero rows found" into
    ``TenantBoundaryError`` where appropriate.
    """

    def __init__(self, connection: psycopg2.extensions.connection, tenant_id: str) -> None:
        self._conn = connection
        self._tenant_id = tenant_id
        self._ensure_tenant()

    # ── helpers ─────────────────────────────────────────────────────

    def _ensure_tenant(self) -> None:
        """Set the RLS tenant context on every connection."""
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SET app.current_tenant = %(tenant)s", {"tenant": self._tenant_id})

    def _tx(self) -> Iterator[None]:
        """Context manager for a single transaction."""
        try:
            yield
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    def _cursor(self):
        return self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # ── Tenant CRUD ─────────────────────────────────────────────────

    def create_tenant(self, tenant: Tenant) -> Tenant:
        cur = self._cursor()
        cur.execute(
            "INSERT INTO tenants (id, legal_name, created_at) VALUES (%s, %s, %s)",
            (tenant.id, tenant.legal_name, tenant.created_at),
        )
        return tenant

    def get_tenant(self, tenant_id: str) -> Tenant:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM tenants WHERE id = %s", (tenant_id,)
        )
        row = cur.fetchone()
        if not row:
            raise NotFoundError(f"Tenant {tenant_id!r} not found")
        return _load_tenant(dict(row.description._names))  # type: ignore

    def list_tenants(self) -> list[Tenant]:
        cur = self._cursor()
        cur.execute("SELECT * FROM tenants")
        return [_load_tenant(dict(r)) for r in cur.fetchall()]

    # ── User CRUD ───────────────────────────────────────────────────

    def create_user(self, user: User) -> User:
        cur = self._cursor()
        cur.execute(
            "INSERT INTO users (id, email, created_at) VALUES (%s, %s, %s)",
            (user.id, user.email, user.created_at),
        )
        return user

    def get_user(self, user_id: str) -> User:
        cur = self._cursor()
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        if not row:
            raise NotFoundError(f"User {user_id!r} not found")
        return _load_user(dict(row.description._names))  # type: ignore

    def list_users(self) -> list[User]:
        cur = self._cursor()
        cur.execute("SELECT * FROM users")
        return [_load_user(dict(r)) for r in cur.fetchall()]

    # ── Membership ──────────────────────────────────────────────────

    def create_membership(self, membership: Membership) -> Membership:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO memberships (tenant_id, user_id, roles_json, active, version)
               VALUES (%s, %s, %s, %s, %s)""",
            (
                membership.tenant_id,
                membership.user_id,
                json.dumps([r.value for r in membership.roles]),
                int(membership.active),
                membership.version,
            ),
        )
        return membership

    def get_membership(self, tenant_id: str, user_id: str) -> Membership:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM memberships WHERE tenant_id = %s AND user_id = %s",
            (tenant_id, user_id),
        )
        row = cur.fetchone()
        if not row:
            raise NotFoundError(
                f"Membership({tenant_id!r}, {user_id!r}) not found"
            )
        return _load_membership(dict(row))

    def list_memberships(self, tenant_id: str) -> list[Membership]:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM memberships WHERE tenant_id = %s", (tenant_id,)
        )
        return [_load_membership(dict(r)) for r in cur.fetchall()]

    # ── Command ─────────────────────────────────────────────────────

    def create_command(self, command: Command) -> Command:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO commands (id, state, payload, created_at)
               VALUES (%s, %s, %s, %s)""",
            (command.id, command.state.value, command.payload, command.created_at),
        )
        return command

    def get_command(self, command_id: str) -> Command:
        cur = self._cursor()
        cur.execute("SELECT * FROM commands WHERE id = %s", (command_id,))
        row = cur.fetchone()
        if not row:
            raise NotFoundError(f"Command {command_id!r} not found")
        return _load_command(dict(row))

    def list_commands(self, tenant_id: str | None = None) -> list[Command]:
        cur = self._cursor()
        if tenant_id:
            cur.execute(
                "SELECT * FROM commands WHERE tenant_id = %s", (tenant_id,)
            )
        else:
            cur.execute("SELECT * FROM commands")
        return [_load_command(dict(r)) for r in cur.fetchall()]

    def update_command_state(self, command_id: str, state: CommandState) -> Command:
        cur = self._cursor()
        cur.execute(
            "UPDATE commands SET state = %s WHERE id = %s RETURNING *",
            (state.value, command_id),
        )
        row = cur.fetchone()
        if not row:
            raise NotFoundError(f"Command {command_id!r} not found")
        return _load_command(dict(row))

    # ── AuditEvent ──────────────────────────────────────────────────

    def create_audit_event(self, event: AuditEvent) -> AuditEvent:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO audit_events (id, tenant_id, actor, action,
               entity_type, entity_id, details, occurred_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                event.id, event.tenant_id, event.actor, event.action,
                event.entity_type, event.entity_id,
                json.dumps(event.details), event.occurred_at,
            ),
        )
        return event

    def list_audit_events(self, tenant_id: str) -> list[AuditEvent]:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM audit_events WHERE tenant_id = %s ORDER BY occurred_at",
            (tenant_id,),
        )
        return [_load_audit_event(dict(r)) for r in cur.fetchall()]

    # ── AgentState / AgentStatusSnapshot ────────────────────────────

    def upsert_agent_state(self, state: AgentState) -> AgentState:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO agent_states (tenant_id, agent_id, status, run_log, version, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (tenant_id, agent_id)
               DO UPDATE SET status = %s, run_log = %s, version = %s, updated_at = %s
               RETURNING *""",
            (
                state.tenant_id, state.agent_id, state.status,
                json.dumps(state.run_log), state.version, state.updated_at,
                state.status, json.dumps(state.run_log), state.version, state.updated_at,
            ),
        )
        row = cur.fetchone()
        return _load_agent_state(dict(row))

    def get_agent_state(self, tenant_id: str, agent_id: str) -> AgentState | None:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM agent_states WHERE tenant_id = %s AND agent_id = %s",
            (tenant_id, agent_id),
        )
        row = cur.fetchone()
        if not row:
            return None
        return _load_agent_state(dict(row))

    def upsert_agent_status_snapshot(self, snapshot: AgentStatusSnapshot) -> AgentStatusSnapshot:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO agent_status_snapshots (tenant_id, agent_id, status, last_heartbeat)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (tenant_id, agent_id)
               DO UPDATE SET status = %s, last_heartbeat = %s
               RETURNING *""",
            (
                snapshot.tenant_id, snapshot.agent_id, snapshot.status,
                snapshot.last_heartbeat,
                snapshot.status, snapshot.last_heartbeat,
            ),
        )
        row = cur.fetchone()
        return _load_agent_status_snapshot(dict(row))

    # ── AdapterCapability / AdapterCapabilityManifest ───────────────

    def upsert_adapter_capability(
        self, cap: AdapterCapability,
    ) -> AdapterCapability:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO adapter_capabilities (adapter_id, capability, manifest_json, version)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (adapter_id, capability)
               DO UPDATE SET manifest_json = %s, version = %s
               RETURNING *""",
            (
                cap.adapter_id, cap.capability,
                json.dumps(cap.manifest), cap.version,
                json.dumps(cap.manifest), cap.version,
            ),
        )
        return _load_adapter_capability(dict(cur.fetchone()))

    def get_adapter_capabilities(
        self, adapter_id: str,
    ) -> list[AdapterCapability]:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM adapter_capabilities WHERE adapter_id = %s",
            (adapter_id,),
        )
        return [_load_adapter_capability(dict(r)) for r in cur.fetchall()]

    def upsert_adapter_capability_manifest(
        self, manifest: AdapterCapabilityManifest,
    ) -> AdapterCapabilityManifest:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO adapter_capability_manifests (adapter_id, capabilities_json, version)
               VALUES (%s, %s, %s)
               ON CONFLICT (adapter_id)
               DO UPDATE SET capabilities_json = %s, version = %s
               RETURNING *""",
            (
                manifest.adapter_id,
                json.dumps(manifest.capabilities),
                manifest.version,
                json.dumps(manifest.capabilities),
                manifest.version,
            ),
        )
        return _load_adapter_capability_manifest(dict(cur.fetchone()))

    def get_adapter_capability_manifest(
        self, adapter_id: str,
    ) -> AdapterCapabilityManifest | None:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM adapter_capability_manifests WHERE adapter_id = %s",
            (adapter_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return _load_adapter_capability_manifest(dict(row))

    def upsert_poll_checkpoint(
        self, checkpoint: AdapterPollCheckpoint,
    ) -> AdapterPollCheckpoint:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO adapter_poll_checkpoints (adapter_id, last_cursor, updated_at)
               VALUES (%s, %s, %s)
               ON CONFLICT (adapter_id)
               DO UPDATE SET last_cursor = %s, updated_at = %s
               RETURNING *""",
            (
                checkpoint.adapter_id, checkpoint.last_cursor, checkpoint.updated_at,
                checkpoint.last_cursor, checkpoint.updated_at,
            ),
        )
        return _load_adapter_poll_checkpoint(dict(cur.fetchone()))

    def get_poll_checkpoint(
        self, adapter_id: str,
    ) -> AdapterPollCheckpoint | None:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM adapter_poll_checkpoints WHERE adapter_id = %s",
            (adapter_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return _load_adapter_poll_checkpoint(dict(row))

    # ── Inbox ───────────────────────────────────────────────────────

    def upsert_inbox_state(self, state: InboxState) -> InboxState:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO inbox_states (tenant_id, user_id, messages_json, version)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (tenant_id, user_id)
               DO UPDATE SET messages_json = %s, version = %s
               RETURNING *""",
            (
                state.tenant_id, state.user_id,
                json.dumps([dict(m) for m in state.messages]),
                state.version,
                json.dumps([dict(m) for m in state.messages]),
                state.version,
            ),
        )
        return _load_inbox_state(dict(cur.fetchone()))

    def get_inbox_state(self, tenant_id: str, user_id: str) -> InboxState | None:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM inbox_states WHERE tenant_id = %s AND user_id = %s",
            (tenant_id, user_id),
        )
        row = cur.fetchone()
        if not row:
            return None
        return _load_inbox_state(dict(row))

    # ── Outbox ──────────────────────────────────────────────────────

    def create_outbox_event(self, event: OutboxEvent) -> OutboxEvent:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO outbox_events (id, tenant_id, kind, payload_json, state,
               created_at, delivered_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                event.id, event.tenant_id, event.kind,
                json.dumps(event.payload), event.state.value,
                event.created_at, event.delivered_at,
            ),
        )
        return event

    def get_outbox_events(
        self, tenant_id: str, state: OutboxState | None = None,
    ) -> list[OutboxEvent]:
        cur = self._cursor()
        if state:
            cur.execute(
                "SELECT * FROM outbox_events WHERE tenant_id = %s AND state = %s",
                (tenant_id, state.value),
            )
        else:
            cur.execute(
                "SELECT * FROM outbox_events WHERE tenant_id = %s", (tenant_id,),
            )
        return [_load_outbox_event(dict(r)) for r in cur.fetchall()]

    def update_outbox_event_state(
        self, event_id: str, state: OutboxState,
    ) -> OutboxEvent:
        cur = self._cursor()
        cur.execute(
            "UPDATE outbox_events SET state = %s WHERE id = %s RETURNING *",
            (state.value, event_id),
        )
        row = cur.fetchone()
        if not row:
            raise NotFoundError(f"OutboxEvent {event_id!r} not found")
        return _load_outbox_event(dict(row))

    # ── Approval ────────────────────────────────────────────────────

    def create_approval(self, approval: Approval) -> Approval:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO approvals (id, kind, state, requestor_id, approver_id,
               target_type, target_id, payload_json, created_at, completed_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                approval.id, approval.kind.value, approval.state.value,
                approval.requestor_id, approval.approver_id,
                approval.target_type, approval.target_id,
                json.dumps(approval.payload),
                approval.created_at, approval.completed_at,
            ),
        )
        return approval

    def get_approval(self, approval_id: str) -> Approval:
        cur = self._cursor()
        cur.execute("SELECT * FROM approvals WHERE id = %s", (approval_id,))
        row = cur.fetchone()
        if not row:
            raise NotFoundError(f"Approval {approval_id!r} not found")
        return _load_approval(dict(row))

    def list_approvals(self, tenant_id: str) -> list[Approval]:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM approvals WHERE tenant_id = %s ORDER BY created_at",
            (tenant_id,),
        )
        return [_load_approval(dict(r)) for r in cur.fetchall()]

    def update_approval_state(
        self, approval_id: str, state: ApprovalState,
    ) -> Approval:
        cur = self._cursor()
        cur.execute(
            "UPDATE approvals SET state = %s WHERE id = %s RETURNING *",
            (state.value, approval_id),
        )
        row = cur.fetchone()
        if not row:
            raise NotFoundError(f"Approval {approval_id!r} not found")
        return _load_approval(dict(row))

    def upsert_approval_intent(
        self, intent: ApprovalIntent,
    ) -> ApprovalIntent:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO approval_intents (id, kind, requestor_id, target_type,
               target_id, payload_json, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET
                   kind = %s, requestor_id = %s, target_type = %s,
                   target_id = %s, payload_json = %s
               RETURNING *""",
            (
                intent.id, intent.kind.value, intent.requestor_id,
                intent.target_type, intent.target_id,
                json.dumps(intent.payload), intent.created_at,
                intent.kind.value, intent.requestor_id,
                intent.target_type, intent.target_id,
                json.dumps(intent.payload),
            ),
        )
        return _load_approval_intent(dict(cur.fetchone()))

    def get_approval_intent(self, intent_id: str) -> ApprovalIntent | None:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM approval_intents WHERE id = %s", (intent_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return _load_approval_intent(dict(row))

    # ── ExecutionPreparation ────────────────────────────────────────

    def create_execution_preparation(
        self, prep: ExecutionPreparation,
    ) -> ExecutionPreparation:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO execution_preparations (attempt_id, tenant_id, agent_id,
               budget_policy_id, tool_use_plan, budget_request, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                prep.attempt_id, prep.tenant_id, prep.agent_id,
                prep.budget_policy_id,
                json.dumps(prep.tool_use_plan),
                json.dumps(prep.budget_request),
                prep.created_at,
            ),
        )
        return prep

    def get_execution_preparation(
        self, attempt_id: str,
    ) -> ExecutionPreparation | None:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM execution_preparations WHERE attempt_id = %s",
            (attempt_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return _load_execution_preparation(dict(row))

    # ── DemoExecutionControl ────────────────────────────────────────

    def create_execution_control(
        self, control: DemoExecutionControl,
    ) -> DemoExecutionControl:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO demo_execution_controls (task_id, tenant_id, status,
               max_turns, budget_limit, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                control.task_id, control.tenant_id, control.status,
                control.max_turns, control.budget_limit,
                control.created_at, control.updated_at,
            ),
        )
        return control

    def get_execution_control(
        self, task_id: str,
    ) -> DemoExecutionControl | None:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM demo_execution_controls WHERE task_id = %s",
            (task_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return _load_demo_execution_control(dict(row))

    def update_execution_control_status(
        self, task_id: str, status: str,
    ) -> DemoExecutionControl:
        cur = self._cursor()
        cur.execute(
            """UPDATE demo_execution_controls SET status = %s, updated_at = %s
               WHERE task_id = %s RETURNING *""",
            (status, _ts(), task_id),
        )
        row = cur.fetchone()
        if not row:
            raise NotFoundError(f"ExecutionControl {task_id!r} not found")
        return _load_demo_execution_control(dict(row))

    # ── ExecutionAttempt ────────────────────────────────────────────

    def create_execution_attempt(
        self, attempt: ExecutionAttempt,
    ) -> ExecutionAttempt:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO execution_attempts (id, task_id, tenant_id, agent_id,
               turn_number, status, tool_calls, results, created_at, completed_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                attempt.id, attempt.task_id, attempt.tenant_id, attempt.agent_id,
                attempt.turn_number, attempt.status,
                json.dumps(attempt.tool_calls), json.dumps(attempt.results),
                attempt.created_at, attempt.completed_at,
            ),
        )
        return attempt

    def get_execution_attempt(
        self, attempt_id: str,
    ) -> ExecutionAttempt | None:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM execution_attempts WHERE id = %s", (attempt_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return _load_execution_attempt(dict(row))

    def list_execution_attempts(
        self, task_id: str,
    ) -> list[ExecutionAttempt]:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM execution_attempts WHERE task_id = %s ORDER BY created_at",
            (task_id,),
        )
        return [_load_execution_attempt(dict(r)) for r in cur.fetchall()]

    # ── AttemptObservation ──────────────────────────────────────────

    def create_attempt_observation(
        self, obs: AttemptObservation,
    ) -> AttemptObservation:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO attempt_observations (attempt_id, status, details, occurred_at)
               VALUES (%s, %s, %s, %s)""",
            (
                obs.attempt_id, obs.status,
                json.dumps(obs.details), obs.occurred_at,
            ),
        )
        return obs

    def list_attempt_observations(
        self, attempt_id: str,
    ) -> list[AttemptObservation]:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM attempt_observations WHERE attempt_id = %s ORDER BY occurred_at",
            (attempt_id,),
        )
        return [_load_attempt_observation(dict(r)) for r in cur.fetchall()]

    # ── ChannelOrder ────────────────────────────────────────────────

    def create_channel_order(self, order: ChannelOrder) -> ChannelOrder:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO channel_orders (id, tenant_id, channel, order_id,
               lines_json, total_amount, currency, state, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                order.id, order.tenant_id, order.channel, order.order_id,
                json.dumps(order.lines), order.total_amount, order.currency,
                order.state.value, order.created_at, order.updated_at,
            ),
        )
        return order

    def get_channel_order(self, order_id: str) -> ChannelOrder:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM channel_orders WHERE id = %s", (order_id,),
        )
        row = cur.fetchone()
        if not row:
            raise NotFoundError(f"ChannelOrder {order_id!r} not found")
        return _load_channel_order(dict(row))

    def list_channel_orders(self, tenant_id: str) -> list[ChannelOrder]:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM channel_orders WHERE tenant_id = %s ORDER BY created_at",
            (tenant_id,),
        )
        return [_load_channel_order(dict(r)) for r in cur.fetchall()]

    def update_channel_order_state(
        self, order_id: str, state: ChannelOrderState,
    ) -> ChannelOrder:
        cur = self._cursor()
        cur.execute(
            """UPDATE channel_orders SET state = %s, updated_at = %s
               WHERE id = %s RETURNING *""",
            (state.value, _ts(), order_id),
        )
        row = cur.fetchone()
        if not row:
            raise NotFoundError(f"ChannelOrder {order_id!r} not found")
        return _load_channel_order(dict(row))

    # ── RoutingDecision ─────────────────────────────────────────────

    def create_routing_decision(
        self, decision: RoutingDecision,
    ) -> RoutingDecision:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO routing_decisions (order_id, tenant_id, channel,
               decision, supplier_id, reason, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                decision.order_id, decision.tenant_id, decision.channel,
                decision.decision, decision.supplier_id, decision.reason,
                decision.created_at,
            ),
        )
        return decision

    def list_routing_decisions(
        self, order_id: str,
    ) -> list[RoutingDecision]:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM routing_decisions WHERE order_id = %s",
            (order_id,),
        )
        return [_load_routing_decision(dict(r)) for r in cur.fetchall()]

    # ── SupplierPurchaseOrder ───────────────────────────────────────

    def create_supplier_purchase_order(
        self, po: SupplierPurchaseOrder,
    ) -> SupplierPurchaseOrder:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO supplier_purchase_orders (id, tenant_id, supplier_id,
               order_id, lines_json, total_amount, currency, state,
               created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                po.id, po.tenant_id, po.supplier_id, po.order_id,
                json.dumps(po.lines), po.total_amount, po.currency,
                po.state.value, po.created_at, po.updated_at,
            ),
        )
        return po

    def get_supplier_purchase_order(
        self, po_id: str,
    ) -> SupplierPurchaseOrder:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM supplier_purchase_orders WHERE id = %s", (po_id,),
        )
        row = cur.fetchone()
        if not row:
            raise NotFoundError(f"PurchaseOrder {po_id!r} not found")
        return _load_supplier_purchase_order(dict(row))

    def list_supplier_purchase_orders(
        self, tenant_id: str,
    ) -> list[SupplierPurchaseOrder]:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM supplier_purchase_orders WHERE tenant_id = %s ORDER BY created_at",
            (tenant_id,),
        )
        return [_load_supplier_purchase_order(dict(r)) for r in cur.fetchall()]

    # ── TrackingObservation ─────────────────────────────────────────

    def create_tracking_observation(
        self, obs: TrackingObservation,
    ) -> TrackingObservation:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO tracking_observations (order_id, tenant_id, status,
               location, details, observed_at)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                obs.order_id, obs.tenant_id, obs.status, obs.location,
                json.dumps(obs.details), obs.observed_at,
            ),
        )
        return obs

    def list_tracking_observations(
        self, order_id: str,
    ) -> list[TrackingObservation]:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM tracking_observations WHERE order_id = %s ORDER BY observed_at",
            (order_id,),
        )
        return [_load_tracking_observation(dict(r)) for r in cur.fetchall()]

    # ── DemoClaim ───────────────────────────────────────────────────

    def create_claim(self, claim: DemoClaim) -> DemoClaim:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO demo_claims (id, tenant_id, order_id, reason, amount,
               status, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                claim.id, claim.tenant_id, claim.order_id, claim.reason,
                claim.amount, claim.status.value,
                claim.created_at, claim.updated_at,
            ),
        )
        return claim

    def get_claim(self, claim_id: str) -> DemoClaim:
        cur = self._cursor()
        cur.execute("SELECT * FROM demo_claims WHERE id = %s", (claim_id,))
        row = cur.fetchone()
        if not row:
            raise NotFoundError(f"Claim {claim_id!r} not found")
        return _load_demo_claim(dict(row))

    def list_claims(self, tenant_id: str) -> list[DemoClaim]:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM demo_claims WHERE tenant_id = %s ORDER BY created_at",
            (tenant_id,),
        )
        return [_load_demo_claim(dict(r)) for r in cur.fetchall()]

    def update_claim_status(
        self, claim_id: str, status: ClaimStatus,
    ) -> DemoClaim:
        cur = self._cursor()
        cur.execute(
            """UPDATE demo_claims SET status = %s, updated_at = %s
               WHERE id = %s RETURNING *""",
            (status.value, _ts(), claim_id),
        )
        row = cur.fetchone()
        if not row:
            raise NotFoundError(f"Claim {claim_id!r} not found")
        return _load_demo_claim(dict(row))

    # ── ClaimStatusObservation ──────────────────────────────────────

    def create_claim_status_observation(
        self, obs: ClaimStatusObservation,
    ) -> ClaimStatusObservation:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO claim_status_observations (claim_id, from_status, to_status, observed_at)
               VALUES (%s, %s, %s, %s)""",
            (
                obs.claim_id, obs.from_status.value, obs.to_status.value,
                obs.observed_at,
            ),
        )
        return obs

    def list_claim_status_observations(
        self, claim_id: str,
    ) -> list[ClaimStatusObservation]:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM claim_status_observations WHERE claim_id = %s ORDER BY observed_at",
            (claim_id,),
        )
        return [_load_claim_status_observation(dict(r)) for r in cur.fetchall()]

    # ── DemoSettlementBatch ─────────────────────────────────────────

    def create_settlement_batch(
        self, batch: DemoSettlementBatch,
    ) -> DemoSettlementBatch:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO demo_settlement_batches (id, tenant_id, channel,
               total_amount, fee_amount, net_amount, status, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                batch.id, batch.tenant_id, batch.channel,
                batch.total_amount, batch.fee_amount, batch.net_amount,
                batch.status.value, batch.created_at, batch.updated_at,
            ),
        )
        return batch

    def get_settlement_batch(
        self, batch_id: str,
    ) -> DemoSettlementBatch:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM demo_settlement_batches WHERE id = %s", (batch_id,),
        )
        row = cur.fetchone()
        if not row:
            raise NotFoundError(f"SettlementBatch {batch_id!r} not found")
        return _load_demo_settlement_batch(dict(row))

    def list_settlement_batches(
        self, tenant_id: str,
    ) -> list[DemoSettlementBatch]:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM demo_settlement_batches WHERE tenant_id = %s ORDER BY created_at",
            (tenant_id,),
        )
        return [_load_demo_settlement_batch(dict(r)) for r in cur.fetchall()]

    # ── DemoSettlementLine ──────────────────────────────────────────

    def create_settlement_line(
        self, line: DemoSettlementLine,
    ) -> DemoSettlementLine:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO demo_settlement_lines (batch_id, order_id,
               gross_amount, fee_amount, net_amount)
               VALUES (%s, %s, %s, %s, %s)""",
            (
                line.batch_id, line.order_id,
                line.gross_amount, line.fee_amount, line.net_amount,
            ),
        )
        return line

    def list_settlement_lines(
        self, batch_id: str,
    ) -> list[DemoSettlementLine]:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM demo_settlement_lines WHERE batch_id = %s",
            (batch_id,),
        )
        return [_load_demo_settlement_line(dict(r)) for r in cur.fetchall()]

    # ── DemoRealizedProfit ──────────────────────────────────────────

    def create_realized_profit(
        self, profit: DemoRealizedProfit,
    ) -> DemoRealizedProfit:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO demo_realized_profits (id, tenant_id, order_id,
               revenue, cost, profit, realized_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                profit.id, profit.tenant_id, profit.order_id,
                profit.revenue, profit.cost, profit.profit,
                profit.realized_at,
            ),
        )
        return profit

    def list_realized_profits(
        self, tenant_id: str,
    ) -> list[DemoRealizedProfit]:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM demo_realized_profits WHERE tenant_id = %s ORDER BY realized_at",
            (tenant_id,),
        )
        return [_load_demo_realized_profit(dict(r)) for r in cur.fetchall()]

    # ── DemoCatalogImport / Snapshot ────────────────────────────────

    def create_catalog_import(
        self, imp: DemoCatalogImport,
    ) -> DemoCatalogImport:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO demo_catalog_imports (id, tenant_id, source,
               product_count, status, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                imp.id, imp.tenant_id, imp.source, imp.product_count,
                imp.status, imp.created_at, imp.updated_at,
            ),
        )
        return imp

    def get_catalog_import(
        self, import_id: str,
    ) -> DemoCatalogImport | None:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM demo_catalog_imports WHERE id = %s", (import_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return _load_demo_catalog_import(dict(row))

    def create_catalog_snapshot(
        self, snap: DemoCatalogSnapshot,
    ) -> DemoCatalogSnapshot:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO demo_catalog_snapshots (id, tenant_id, import_id,
               snapshot_json, created_at)
               VALUES (%s, %s, %s, %s, %s)""",
            (
                snap.id, snap.tenant_id, snap.import_id,
                json.dumps(snap.snapshot), snap.created_at,
            ),
        )
        return snap

    def get_catalog_snapshot(
        self, snapshot_id: str,
    ) -> DemoCatalogSnapshot | None:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM demo_catalog_snapshots WHERE id = %s", (snapshot_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return _load_demo_catalog_snapshot(dict(row))

    # ── DemoCanonicalProduct ────────────────────────────────────────

    def upsert_canonical_product(
        self, prod: DemoCanonicalProduct,
    ) -> DemoCanonicalProduct:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO demo_canonical_products (id, tenant_id, canonical_id,
               attributes_json, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET
                   attributes_json = %s, updated_at = %s
               RETURNING *""",
            (
                prod.id, prod.tenant_id, prod.canonical_id,
                json.dumps(prod.attributes),
                prod.created_at, prod.updated_at,
                json.dumps(prod.attributes), prod.updated_at,
            ),
        )
        return _load_demo_canonical_product(dict(cur.fetchone()))

    def get_canonical_product(
        self, product_id: str,
    ) -> DemoCanonicalProduct | None:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM demo_canonical_products WHERE id = %s", (product_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return _load_demo_canonical_product(dict(row))

    # ── DemoProductLineage ──────────────────────────────────────────

    def create_product_lineage(
        self, lineage: DemoProductLineage,
    ) -> DemoProductLineage:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO demo_product_lineage (canonical_id, tenant_id,
               source_tenant_id, product_id, mapped_at)
               VALUES (%s, %s, %s, %s, %s)""",
            (
                lineage.canonical_id, lineage.tenant_id,
                lineage.source_tenant_id, lineage.product_id,
                lineage.mapped_at,
            ),
        )
        return lineage

    def list_product_lineage(
        self, canonical_id: str,
    ) -> list[DemoProductLineage]:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM demo_product_lineage WHERE canonical_id = %s",
            (canonical_id,),
        )
        return [_load_demo_product_lineage(dict(r)) for r in cur.fetchall()]

    # ── DemoChannelOffer ────────────────────────────────────────────

    def create_channel_offer(
        self, offer: DemoChannelOffer,
    ) -> DemoChannelOffer:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO demo_channel_offers (id, tenant_id, channel,
               product_id, price, currency, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                offer.id, offer.tenant_id, offer.channel, offer.product_id,
                offer.price, offer.currency, offer.created_at, offer.updated_at,
            ),
        )
        return offer

    def list_channel_offers(
        self, tenant_id: str,
    ) -> list[DemoChannelOffer]:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM demo_channel_offers WHERE tenant_id = %s",
            (tenant_id,),
        )
        return [_load_demo_channel_offer(dict(r)) for r in cur.fetchall()]

    # ── DemoToolCommand ─────────────────────────────────────────────

    def create_tool_command(
        self, cmd: DemoToolCommand,
    ) -> DemoToolCommand:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO demo_tool_commands (id, tenant_id, agent_id,
               tool_name, arguments_json, result_json, created_at, completed_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                cmd.id, cmd.tenant_id, cmd.agent_id, cmd.tool_name,
                json.dumps(cmd.arguments), json.dumps(cmd.result),
                cmd.created_at, cmd.completed_at,
            ),
        )
        return cmd

    def list_tool_commands(
        self, tenant_id: str,
    ) -> list[DemoToolCommand]:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM demo_tool_commands WHERE tenant_id = %s",
            (tenant_id,),
        )
        return [_load_demo_tool_command(dict(r)) for r in cur.fetchall()]

    # ── DemoAgentRun ────────────────────────────────────────────────

    def create_agent_run(
        self, run: DemoAgentRun,
    ) -> DemoAgentRun:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO demo_agent_runs (id, tenant_id, agent_id,
               status, steps_json, created_at, completed_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                run.id, run.tenant_id, run.agent_id, run.status,
                json.dumps(run.steps), run.created_at, run.completed_at,
            ),
        )
        return run

    def list_agent_runs(
        self, tenant_id: str,
    ) -> list[DemoAgentRun]:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM demo_agent_runs WHERE tenant_id = %s ORDER BY created_at",
            (tenant_id,),
        )
        return [_load_demo_agent_run(dict(r)) for r in cur.fetchall()]

    # ── DemoByokReference ───────────────────────────────────────────

    def create_byok_reference(
        self, ref: DemoByokReference,
    ) -> DemoByokReference:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO demo_byok_references (id, tenant_id, key_type,
               key_hint, created_at)
               VALUES (%s, %s, %s, %s, %s)""",
            (ref.id, ref.tenant_id, ref.key_type, ref.key_hint, ref.created_at),
        )
        return ref

    def list_byok_references(
        self, tenant_id: str,
    ) -> list[DemoByokReference]:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM demo_byok_references WHERE tenant_id = %s",
            (tenant_id,),
        )
        return [_load_byok_reference(dict(r)) for r in cur.fetchall()]

    # ── DemoBudgetPolicy ────────────────────────────────────────────

    def create_budget_policy(
        self, policy: DemoBudgetPolicy,
    ) -> DemoBudgetPolicy:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO demo_budget_policies (id, tenant_id, policy_name,
               limits_json, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                policy.id, policy.tenant_id, policy.policy_name,
                json.dumps(policy.limits),
                policy.created_at, policy.updated_at,
            ),
        )
        return policy

    def get_budget_policy(
        self, policy_id: str,
    ) -> DemoBudgetPolicy | None:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM demo_budget_policies WHERE id = %s", (policy_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return _load_demo_budget_policy(dict(row))

    def list_budget_policies(
        self, tenant_id: str,
    ) -> list[DemoBudgetPolicy]:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM demo_budget_policies WHERE tenant_id = %s",
            (tenant_id,),
        )
        return [_load_demo_budget_policy(dict(r)) for r in cur.fetchall()]

    # ── DemoBudgetLedgerEntry ───────────────────────────────────────

    def create_budget_ledger_entry(
        self, entry: DemoBudgetLedgerEntry,
    ) -> DemoBudgetLedgerEntry:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO demo_budget_ledger_entries (id, tenant_id, policy_id,
               amount, description, created_at)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                entry.id, entry.tenant_id, entry.policy_id,
                entry.amount, entry.description, entry.created_at,
            ),
        )
        return entry

    def list_budget_ledger_entries(
        self, tenant_id: str,
    ) -> list[DemoBudgetLedgerEntry]:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM demo_budget_ledger_entries WHERE tenant_id = %s ORDER BY created_at",
            (tenant_id,),
        )
        return [_load_budget_ledger_entry(dict(r)) for r in cur.fetchall()]

    # ── DemoNotificationPreference ──────────────────────────────────

    def upsert_notification_preference(
        self, pref: DemoNotificationPreference,
    ) -> DemoNotificationPreference:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO demo_notification_preferences (id, tenant_id, user_id,
               channels_json, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET
                   channels_json = %s, updated_at = %s
               RETURNING *""",
            (
                pref.id, pref.tenant_id, pref.user_id,
                json.dumps(pref.channels),
                pref.created_at, pref.updated_at,
                json.dumps(pref.channels), pref.updated_at,
            ),
        )
        return _load_demo_notification_preference(dict(cur.fetchone()))

    def get_notification_preference(
        self, pref_id: str,
    ) -> DemoNotificationPreference | None:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM demo_notification_preferences WHERE id = %s", (pref_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return _load_demo_notification_preference(dict(row))

    # ── DemoNotificationDelivery ────────────────────────────────────

    def create_notification_delivery(
        self, delivery: DemoNotificationDelivery,
    ) -> DemoNotificationDelivery:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO demo_notification_deliveries (id, tenant_id, user_id,
               channel, subject, body_json, sent_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                delivery.id, delivery.tenant_id, delivery.user_id,
                delivery.channel, delivery.subject,
                json.dumps(delivery.body), delivery.sent_at,
            ),
        )
        return delivery

    def list_notification_deliveries(
        self, tenant_id: str,
    ) -> list[DemoNotificationDelivery]:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM demo_notification_deliveries WHERE tenant_id = %s ORDER BY sent_at",
            (tenant_id,),
        )
        return [_load_demo_notification_delivery(dict(r)) for r in cur.fetchall()]

    # ── DemoIncidentAcknowledgement ─────────────────────────────────

    def create_incident_acknowledgement(
        self, ack: DemoIncidentAcknowledgement,
    ) -> DemoIncidentAcknowledgement:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO demo_incident_acknowledgements (id, tenant_id,
               incident_id, acknowledged_by, acknowledged_at)
               VALUES (%s, %s, %s, %s, %s)""",
            (
                ack.id, ack.tenant_id, ack.incident_id,
                ack.acknowledged_by, ack.acknowledged_at,
            ),
        )
        return ack

    def list_incident_acknowledgements(
        self, tenant_id: str,
    ) -> list[DemoIncidentAcknowledgement]:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM demo_incident_acknowledgements WHERE tenant_id = %s",
            (tenant_id,),
        )
        return [_load_demo_incident_acknowledgement(dict(r)) for r in cur.fetchall()]

    # ── DemoStopControl ─────────────────────────────────────────────

    def create_stop_control(
        self, ctrl: DemoStopControl,
    ) -> DemoStopControl:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO demo_stop_controls (id, tenant_id, agent_id, stopped_at)
               VALUES (%s, %s, %s, %s)""",
            (ctrl.id, ctrl.tenant_id, ctrl.agent_id, ctrl.stopped_at),
        )
        return ctrl

    def get_stop_control(
        self, stop_id: str,
    ) -> DemoStopControl | None:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM demo_stop_controls WHERE id = %s", (stop_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return _load_demo_stop_control(dict(row))

    # ── DemoBackupManifest ──────────────────────────────────────────

    def create_backup_manifest(
        self, manifest: DemoBackupManifest,
    ) -> DemoBackupManifest:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO demo_backup_manifests (id, tenant_id, backup_path,
               created_at)
               VALUES (%s, %s, %s, %s)""",
            (manifest.id, manifest.tenant_id, manifest.backup_path, manifest.created_at),
        )
        return manifest

    def list_backup_manifests(
        self, tenant_id: str,
    ) -> list[DemoBackupManifest]:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM demo_backup_manifests WHERE tenant_id = %s ORDER BY created_at",
            (tenant_id,),
        )
        return [_load_demo_backup_manifest(dict(r)) for r in cur.fetchall()]

    # ── DemoInventorySnapshot ───────────────────────────────────────

    def upsert_inventory_snapshot(
        self, snap: DemoInventorySnapshot,
    ) -> DemoInventorySnapshot:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO demo_inventory_snapshots (tenant_id, product_id, quantity, updated_at)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (tenant_id, product_id)
               DO UPDATE SET quantity = %s, updated_at = %s
               RETURNING *""",
            (
                snap.tenant_id, snap.product_id, snap.quantity, snap.updated_at,
                snap.quantity, snap.updated_at,
            ),
        )
        return _load_demo_inventory_snapshot(dict(cur.fetchone()))

    def get_inventory_snapshot(
        self, tenant_id: str, product_id: str,
    ) -> DemoInventorySnapshot | None:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM demo_inventory_snapshots WHERE tenant_id = %s AND product_id = %s",
            (tenant_id, product_id),
        )
        row = cur.fetchone()
        if not row:
            return None
        return _load_demo_inventory_snapshot(dict(row))

    def list_inventory_snapshots(
        self, tenant_id: str,
    ) -> list[DemoInventorySnapshot]:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM demo_inventory_snapshots WHERE tenant_id = %s",
            (tenant_id,),
        )
        return [_load_demo_inventory_snapshot(dict(r)) for r in cur.fetchall()]

    # ── DemoPriceProjection ─────────────────────────────────────────

    def upsert_price_projection(
        self, proj: DemoPriceProjection,
    ) -> DemoPriceProjection:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO demo_price_projections (tenant_id, product_id,
               projected_price, currency, valid_from, valid_to)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (tenant_id, product_id)
               DO UPDATE SET projected_price = %s, currency = %s,
                   valid_from = %s, valid_to = %s
               RETURNING *""",
            (
                proj.tenant_id, proj.product_id, proj.projected_price,
                proj.currency, proj.valid_from, proj.valid_to,
                proj.projected_price, proj.currency, proj.valid_from, proj.valid_to,
            ),
        )
        return _load_demo_price_projection(dict(cur.fetchone()))

    def get_price_projection(
        self, tenant_id: str, product_id: str,
    ) -> DemoPriceProjection | None:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM demo_price_projections WHERE tenant_id = %s AND product_id = %s",
            (tenant_id, product_id),
        )
        row = cur.fetchone()
        if not row:
            return None
        return _load_demo_price_projection(dict(row))

    # ── BrowserSession ──────────────────────────────────────────────

    def upsert_browser_session(
        self, session: BrowserSession,
    ) -> BrowserSession:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO browser_sessions (id, tenant_id, user_id,
               session_data_json, created_at, expires_at)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET
                   session_data_json = %s, expires_at = %s
               RETURNING *""",
            (
                session.id, session.tenant_id, session.user_id,
                json.dumps(session.session_data),
                session.created_at, session.expires_at,
                json.dumps(session.session_data), session.expires_at,
            ),
        )
        return _load_browser_session(dict(cur.fetchone()))

    def get_browser_session(
        self, session_id: str,
    ) -> BrowserSession | None:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM browser_sessions WHERE id = %s", (session_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return _load_browser_session(dict(row))

    # ── ApprovalConfirmationNonce ───────────────────────────────────

    def create_approval_nonce(
        self, nonce: ApprovalConfirmationNonce,
    ) -> ApprovalConfirmationNonce:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO approval_confirmation_nonces (id, approval_id, nonce,
               created_at, expires_at, consumed)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                nonce.id, nonce.approval_id, nonce.nonce,
                nonce.created_at, nonce.expires_at, int(nonce.consumed),
            ),
        )
        return nonce

    def get_approval_nonce(
        self, nonce_value: str,
    ) -> ApprovalConfirmationNonce | None:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM approval_confirmation_nonces WHERE nonce = %s",
            (nonce_value,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return _load_approval_confirmation_nonce(dict(row))

    def consume_approval_nonce(
        self, nonce_value: str,
    ) -> bool:
        cur = self._cursor()
        cur.execute(
            """UPDATE approval_confirmation_nonces SET consumed = true
               WHERE nonce = %s AND consumed = false
               RETURNING id""",
            (nonce_value,),
        )
        row = cur.fetchone()
        return row is not None

    # ── DemoBudgetRequest ───────────────────────────────────────────

    def create_budget_request(
        self, req: DemoBudgetRequest,
    ) -> DemoBudgetRequest:
        cur = self._cursor()
        cur.execute(
            """INSERT INTO demo_budget_requests (id, tenant_id, policy_id,
               amount, description, created_at)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                req.id, req.tenant_id, req.policy_id,
                req.amount, req.description, req.created_at,
            ),
        )
        return req

    def get_budget_request(
        self, req_id: str,
    ) -> DemoBudgetRequest | None:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM demo_budget_requests WHERE id = %s", (req_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return _load_budget_request(dict(row))

    def list_budget_requests(
        self, tenant_id: str,
    ) -> list[DemoBudgetRequest]:
        cur = self._cursor()
        cur.execute(
            "SELECT * FROM demo_budget_requests WHERE tenant_id = %s ORDER BY created_at",
            (tenant_id,),
        )
        return [_load_budget_request(dict(r)) for r in cur.fetchall()]
