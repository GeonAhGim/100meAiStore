"""Typed local DEMO tool gateway, agent-run ledger, BYOK refs, and budgets."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Mapping
from uuid import uuid4

from .domain import (ApprovalKind, Capability, DemoAgentRun, DemoBudgetLedgerEntry, DemoBudgetPolicy, DemoBudgetRequest,
                     DemoByokReference, DemoToolCommand, OutboxEvent, OutboxState)
from .errors import AuthorizationError, ConflictError, NotFoundError, TenantBoundaryError
from .budget import PLATFORM_WARNING_MINOR, PLATFORM_HARD_CAP_MINOR, budget_time

_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}\Z")
_TOOLS = {"publish_offer", "update_stock", "update_price", "create_purchase_order", "claim_action", "dispatch_shipment", "reconcile", "pause_scope", "resume_scope"}
_TARGETS = {"offer", "order", "supplier", "channel", "tenant", "product", "shipment"}
_MUTATING = _TOOLS - {"reconcile"}
_TIERS = {"economy", "balanced", "quality"}
_TOOL_APPROVAL_KINDS = {
    "publish_offer": frozenset({ApprovalKind.PRODUCT}),
    "update_stock": frozenset({ApprovalKind.PRODUCT}),
    "update_price": frozenset({ApprovalKind.PRODUCT}),
    "create_purchase_order": frozenset({ApprovalKind.PURCHASE}),
    "claim_action": frozenset({ApprovalKind.REFUND}),
    "dispatch_shipment": frozenset({ApprovalKind.PURCHASE}),
    "pause_scope": frozenset({ApprovalKind.PAUSE}),
    "resume_scope": frozenset({ApprovalKind.PAUSE}),
}


def _opaque(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _OPAQUE.fullmatch(value): raise ConflictError(f"invalid {label}")
    return value


def _json(value: Any, label: str = "input") -> str:
    forbidden_parts = {"secret", "password", "authorization", "token", "credential"}

    def normalized_key(value: str) -> str:
        return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value).replace("-", "_").lower()

    def walk(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ConflictError(f"raw secret in {label}")
                normalized = normalized_key(key)
                parts = set(normalized.split("_"))
                sensitive = bool(parts & forbidden_parts or {"api", "key"} <= parts)
                if normalized == "secret_ref" and isinstance(child, str) and re.fullmatch(r"secret-ref:[A-Za-z0-9_.:-]{1,240}", child):
                    sensitive = False
                if sensitive:
                    raise ConflictError(f"raw secret in {label}")
                walk(child)
        elif isinstance(item, (list, tuple)):
            for child in item: walk(child)
        elif item is not None and type(item) not in (str, int, float, bool): raise ConflictError(f"{label} must be JSON")
    try:
        walk(value)
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc: raise ConflictError(f"{label} must be finite JSON") from exc
    if len(encoded.encode()) > 64 * 1024: raise ConflictError(f"{label} too large")
    return encoded


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _validated_tool_approval(service: Any, context: Any, approval_id: str, tool: str,
                             target_type: str, target_id: str, input_value: Mapping[str, Any],
                             requested_policy_version: int, idempotency_key: str) -> tuple[str, str]:
    approval = service.repo.get_approval(context.tenant_id, approval_id)
    command = service.repo.get_command(context.tenant_id, approval.command_id)
    intent = service.repo.get_approval_intent(context.tenant_id, command.id)
    if (intent is None or command.kind not in _TOOL_APPROVAL_KINDS.get(tool, ())
            or command.target_ref != f"{target_type}:{target_id}"
            or _json(command.payload) != _json(input_value)
            or intent.policy_version != requested_policy_version):
        raise ConflictError("tool approval intent mismatch")
    if any(row.approval_id == approval_id and row.state == "accepted"
           and row.idempotency_key != idempotency_key
           for row in service.repo.tool_commands_for(context.tenant_id)):
        raise ConflictError("approval_already_used")
    preparation, _ = service.prepare_execution(
        context, command.id, requested_policy_version, intent.target_version)
    if preparation.canonical_digest != intent.canonical_digest:
        raise ConflictError("tool approval intent mismatch")
    return command.id, intent.canonical_digest


def configure_demo_byok(service: Any, context: Any, provider: str, secret_ref: str, validation_status: str = "UNVERIFIED") -> DemoByokReference:
    service.require(context, Capability.TENANT_ADMIN)
    provider, secret_ref = _opaque(provider, "provider"), _opaque(secret_ref, "secret_ref")
    if not secret_ref.startswith("secret-ref:") or validation_status not in {"UNVERIFIED", "VALID", "INVALID"}:
        raise ConflictError("only opaque BYOK reference metadata is accepted")
    with service.repo.transaction():
        value = service.repo.save_byok_reference(DemoByokReference(str(uuid4()), context.tenant_id, provider, secret_ref, validation_status, service._clock()))
        service._audit(context.tenant_id, context.user_id, "byok.reference_configured", value.id, "succeeded", {"provider": provider, "validation_status": validation_status})
        return value


def set_demo_budget_policy(service: Any, context: Any, *, daily_limit_minor: int, monthly_limit_minor: int,
                           generation_limit: int, agent_run_limit: int, max_tokens: int, max_tool_calls: int,
                           model_tier: str) -> DemoBudgetPolicy:
    service.require(context, Capability.TENANT_ADMIN)
    values = (daily_limit_minor, monthly_limit_minor, generation_limit, agent_run_limit)
    if any(type(v) is not int or v < 0 for v in values) or type(max_tokens) is not int or max_tokens < 1 or type(max_tool_calls) is not int or max_tool_calls < 1 or model_tier not in _TIERS:
        raise ConflictError("invalid budget policy")
    with service.repo.transaction():
        prior = service.repo.get_budget_policy(context.tenant_id)
        version = prior.version + 1 if prior else 1
        value = service.repo.save_budget_policy(DemoBudgetPolicy(context.tenant_id, daily_limit_minor, monthly_limit_minor, generation_limit, agent_run_limit, max_tokens, max_tool_calls, model_tier, version))
        service._audit(context.tenant_id, context.user_id, "budget.policy_configured", context.tenant_id, "succeeded", {"version": version})
        return value


def record_demo_agent_run(service: Any, context: Any, *, agent_id: str, goal: str, policy_version: int,
                          model: str, prompt_version: str, input_value: Any, decision: Mapping[str, Any],
                          confidence: str, tool_calls: int, estimated_cost_minor: int, idempotency_key: str,
                          work_kind: str = "optional") -> DemoAgentRun:
    service.require(context, Capability.TENANT_ADMIN)
    for value, label in ((agent_id, "agent_id"), (model, "model"), (prompt_version, "prompt_version"), (idempotency_key, "idempotency_key")):
        _opaque(value, label)
    if not isinstance(goal, str) or not goal.strip() or type(policy_version) is not int or policy_version < 1 or type(tool_calls) is not int or tool_calls < 0 or type(estimated_cost_minor) is not int or not 0 <= estimated_cost_minor <= 2**63 - 1 or confidence not in {"low", "medium", "high"} or work_kind not in {"optional", "growth"}:
        raise ConflictError("invalid agent run")
    input_json = _json(input_value, "input")
    decision_json = _json(decision, "decision")
    digest = _digest(input_value)
    request_digest = _digest({"agent_id": agent_id, "goal": goal.strip(), "policy_version": policy_version,
                              "model": model, "prompt_version": prompt_version, "input": json.loads(input_json),
                              "decision": json.loads(decision_json), "confidence": confidence, "tool_calls": tool_calls,
                              "estimated_cost_minor": estimated_cost_minor, "work_kind": work_kind})
    now = budget_time(service._clock())
    with service.repo.transaction():
        prior_request = service.repo.get_budget_request(context.tenant_id, idempotency_key, request_digest)
        if prior_request is not None:
            return service.repo.get_agent_run(context.tenant_id, prior_request.run_id)
        policy = service.repo.get_budget_policy(context.tenant_id)
        if policy is None: raise ConflictError("budget policy is required")
        if policy.version != policy_version or tool_calls > policy.max_tool_calls:
            raise ConflictError("budget policy or tool-call limit changed")
        entries = service.repo.budget_entries_for(context.tenant_id)
        day = now.date(); month = (now.year, now.month)
        daily = sum(e.amount_minor for e in entries if budget_time(e.occurred_at).date() == day)
        monthly = sum(e.amount_minor for e in entries if (budget_time(e.occurred_at).year, budget_time(e.occurred_at).month) == month)
        generations = sum(1 for e in entries if budget_time(e.occurred_at).date() == day)
        runs = len(service.repo.agent_runs_for(context.tenant_id))
        outcome = "RECORDED"
        if daily + estimated_cost_minor > policy.daily_limit_minor or monthly + estimated_cost_minor > policy.monthly_limit_minor or generations >= policy.generation_limit or runs >= policy.agent_run_limit:
            outcome = "BLOCKED_BUDGET"
        # Fresh authoritative ledger telemetry is generated inside the same UoW
        # as reservation. No dashboard/cache or caller supplied costs authorize it.
        try:
            snapshot = service.repo.platform_budget_snapshot(now)
            valid = (isinstance(snapshot, dict) and type(snapshot.get('reserved_minor')) is int
                     and snapshot['reserved_minor'] >= 0
                     and 0 <= (now - budget_time(snapshot.get('computed_at'))).total_seconds() <= 60)
        except Exception:
            valid = False
        if not valid:
            outcome = "BLOCKED_TELEMETRY"
        else:
            projected = snapshot['reserved_minor'] + estimated_cost_minor
            if projected >= PLATFORM_HARD_CAP_MINOR:
                outcome = "BLOCKED_GLOBAL_HARD_CAP"
            elif projected >= PLATFORM_WARNING_MINOR:
                if work_kind == "growth":
                    outcome = "BLOCKED_GLOBAL_WARNING"
                elif outcome == "RECORDED":
                    outcome = "RECORDED_WARNING"
        accepted = outcome in {"RECORDED", "RECORDED_WARNING"}
        run = DemoAgentRun(str(uuid4()), context.tenant_id, agent_id, goal.strip(), policy_version, model, prompt_version, digest, decision_json, confidence, tool_calls, None, estimated_cost_minor, estimated_cost_minor if accepted else None, outcome, now)
        service.repo.save_agent_run(run)
        service.repo.save_budget_request(DemoBudgetRequest(context.tenant_id, idempotency_key, request_digest, run.id, outcome, now))
        if accepted:
            service.repo.reserve_budget_entry(DemoBudgetLedgerEntry(str(uuid4()), context.tenant_id, run.id, estimated_cost_minor, now, idempotency_key))
        service._audit(context.tenant_id, context.user_id, "agent.run_recorded", run.id, "accepted" if accepted else "blocked", {"outcome": outcome, "cost_minor": estimated_cost_minor})
        return run


def submit_demo_tool(service: Any, context: Any, *, actor_type: str, actor_id: str, tool: str,
                     target_type: str, target_id: str, input_value: Mapping[str, Any], idempotency_key: str,
                     requested_policy_version: int, approval_id: str | None = None) -> dict[str, Any]:
    service.require(context, Capability.TENANT_ADMIN)
    if actor_type not in {"user", "agent", "workflow"} or not isinstance(input_value, Mapping) or tool not in _TOOLS or target_type not in _TARGETS or type(requested_policy_version) is not int or requested_policy_version < 1:
        raise ConflictError("invalid typed DEMO tool command")
    actor_id, target_id, idempotency_key = _opaque(actor_id, "actor_id"), _opaque(target_id, "target_id"), _opaque(idempotency_key, "idempotency_key")
    if approval_id is not None: approval_id = _opaque(approval_id, "approval_id")
    encoded = _json(input_value)
    state, blocked, approval_command_id, intent_digest = "accepted", None, None, None
    with service.repo.transaction():
        if service.repo.demo_stop_active(context.tenant_id, target_id if target_type == "channel" else None):
            state, blocked = "blocked", "stop_active"
        elif tool in _MUTATING:
            if approval_id is None:
                state, blocked = "approval_required", "approval_required"
            else:
                try:
                    approval_command_id, intent_digest = _validated_tool_approval(
                        service, context, approval_id, tool, target_type, target_id,
                        input_value, requested_policy_version, idempotency_key)
                except (AuthorizationError, ConflictError, NotFoundError, TenantBoundaryError) as exc:
                    state = "blocked"
                    blocked = "approval_already_used" if str(exc) == "approval_already_used" else "approval_intent_mismatch"
        persisted_approval_id = approval_id if approval_command_id is not None else None
        value = DemoToolCommand(str(uuid4()), context.tenant_id, actor_type, actor_id, tool, target_type, target_id, encoded, idempotency_key, requested_policy_version, persisted_approval_id, "DEMO", state, blocked, service._clock(), approval_command_id, intent_digest)
        value, replay = service.repo.save_tool_command(value)
        if not replay:
            service._audit(context.tenant_id, context.user_id, "tool.command_accepted" if state == "accepted" else "tool.command_blocked", value.id, "accepted" if state == "accepted" else "blocked", {"tool": tool, "mode": "DEMO"})
            # Only an exact user-approved mutating command is executable. Read-only
            # bookkeeping commands have no provider execution contract here.
            if state == "accepted" and approval_command_id is not None:
                service.repo.append_outbox(OutboxEvent(str(uuid4()), context.tenant_id, "tool.command", value.id, {"command_id": value.id, "state": state, "mode": "DEMO", "intent_digest": intent_digest}, f"tool:{value.id}:accepted", OutboxState.PENDING, value.created_at))
        return {"command_id": value.id, "state": value.state, "external_refs": [], "policy_decision": {"mode": "DEMO"}, "verification": {}, "next_action": blocked}
