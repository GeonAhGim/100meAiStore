"""Typed local DEMO tool gateway, agent-run ledger, BYOK refs, and budgets."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Mapping
from uuid import uuid4

from .domain import (Capability, DemoAgentRun, DemoBudgetLedgerEntry, DemoBudgetPolicy,
                     DemoByokReference, DemoToolCommand, OutboxEvent, OutboxState)
from .errors import ConflictError

_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}\Z")
_TOOLS = {"publish_offer", "update_stock", "update_price", "create_purchase_order", "claim_action", "reconcile", "pause_scope", "resume_scope"}
_TARGETS = {"offer", "order", "supplier", "channel", "tenant", "product"}
_MUTATING = _TOOLS - {"reconcile"}
_TIERS = {"economy", "balanced", "quality"}


def _opaque(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _OPAQUE.fullmatch(value): raise ConflictError(f"invalid {label}")
    return value


def _json(value: Any, label: str = "input") -> str:
    forbidden = {"api_key", "api_secret", "secret", "password", "authorization"}
    def walk(item: Any) -> None:
        if isinstance(item, Mapping):
            if any(not isinstance(k, str) or k.lower() in forbidden for k in item): raise ConflictError(f"raw secret in {label}")
            for child in item.values(): walk(child)
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
                          confidence: str, tool_calls: int, estimated_cost_minor: int, idempotency_key: str) -> DemoAgentRun:
    service.require(context, Capability.TENANT_ADMIN)
    for value, label in ((agent_id, "agent_id"), (model, "model"), (prompt_version, "prompt_version"), (idempotency_key, "idempotency_key")):
        _opaque(value, label)
    if not isinstance(goal, str) or not goal.strip() or type(policy_version) is not int or policy_version < 1 or type(tool_calls) is not int or tool_calls < 0 or type(estimated_cost_minor) is not int or estimated_cost_minor < 0 or confidence not in {"low", "medium", "high"}:
        raise ConflictError("invalid agent run")
    input_json = _json(input_value, "input")
    decision_json = _json(decision, "decision")
    digest = _digest(input_value)
    now = service._clock()
    with service.repo.transaction():
        policy = service.repo.get_budget_policy(context.tenant_id)
        if policy is None: raise ConflictError("budget policy is required")
        if policy.version != policy_version or tool_calls > policy.max_tool_calls:
            raise ConflictError("budget policy or tool-call limit changed")
        entries = service.repo.budget_entries_for(context.tenant_id)
        prior_entry = next((entry for entry in entries if entry.idempotency_key == idempotency_key), None)
        if prior_entry is not None:
            return service.repo.get_agent_run(context.tenant_id, prior_entry.run_id)
        day = now.date(); month = (now.year, now.month)
        daily = sum(e.amount_minor for e in entries if e.occurred_at.date() == day)
        monthly = sum(e.amount_minor for e in entries if (e.occurred_at.year, e.occurred_at.month) == month)
        generations = sum(1 for e in entries if e.occurred_at.date() == day)
        runs = len(service.repo.agent_runs_for(context.tenant_id))
        outcome = "RECORDED"
        if daily + estimated_cost_minor > policy.daily_limit_minor or monthly + estimated_cost_minor > policy.monthly_limit_minor or generations >= policy.generation_limit or runs >= policy.agent_run_limit:
            outcome = "BLOCKED_BUDGET"
        run = DemoAgentRun(str(uuid4()), context.tenant_id, agent_id, goal.strip(), policy_version, model, prompt_version, digest, decision_json, confidence, tool_calls, None, estimated_cost_minor, estimated_cost_minor if outcome == "RECORDED" else None, outcome, now)
        service.repo.save_agent_run(run)
        if outcome == "RECORDED":
            service.repo.save_budget_entry(DemoBudgetLedgerEntry(str(uuid4()), context.tenant_id, run.id, estimated_cost_minor, now, idempotency_key))
        service._audit(context.tenant_id, context.user_id, "agent.run_recorded", run.id, "accepted" if outcome == "RECORDED" else "blocked", {"outcome": outcome, "cost_minor": estimated_cost_minor})
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
    state, blocked = "accepted", None
    with service.repo.transaction():
        if tool in _MUTATING:
            if approval_id is None:
                state, blocked = "approval_required", "approval_required"
            else:
                approval = service.repo.get_approval(context.tenant_id, approval_id)
                if approval.state.value != "approved": state, blocked = "blocked", "approval_not_approved"
        value = DemoToolCommand(str(uuid4()), context.tenant_id, actor_type, actor_id, tool, target_type, target_id, encoded, idempotency_key, requested_policy_version, approval_id, "DEMO", state, blocked, service._clock())
        value, replay = service.repo.save_tool_command(value)
        if not replay:
            service._audit(context.tenant_id, context.user_id, "tool.command_accepted" if state == "accepted" else "tool.command_blocked", value.id, "accepted" if state == "accepted" else "blocked", {"tool": tool, "mode": "DEMO"})
            service.repo.append_outbox(OutboxEvent(str(uuid4()), context.tenant_id, "tool.command", value.id, {"command_id": value.id, "state": state, "mode": "DEMO"}, f"tool:{value.id}:accepted", OutboxState.PENDING, value.created_at))
        return {"command_id": value.id, "state": value.state, "external_refs": [], "policy_decision": {"mode": "DEMO"}, "verification": {}, "next_action": blocked}
