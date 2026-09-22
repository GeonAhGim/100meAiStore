"""Periodic blocked-task triage: classify why a task is blocked and apply the remedy.

Runs from the autopilot every ``INTERVAL_SECONDS``. Each blocked task's note
is matched against known causes, and only causes with a safe, mechanical
remedy are acted on. Everything else is escalated to the operator by phase
``needs_operator`` and stays visible in the dashboard's decision panel. Every
decision is appended to ``data/control/triage.json`` so a human can see what
the system did and why.

Remedies (all without spending the task's retry budget, because none of these
is the implementer's fault):

- transient       "local LLM error", "review error", "TimeoutError", "deadline"
                  -> ready
- stale_engine    a refusal recorded by the retired HTTP engine (no agent
                  summary artifact) -> ready under claude-local
- base_moved      "landing failed"/"base moved" -> ready, phase rebase_needed
- missing_patch   "patch artifact missing" -> ready

Causes that need the implementer (gate failure, no diff, apply error) are left
to the normal bounded retry. A task whose retry budget is spent, or that has
been needs_decision for longer than ``DECISION_STALE_SECONDS``, is escalated.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .escalation import codex_outcome, hand_to_codex
from .pm import _CLAIM_LOCK, _load, _save, now
from .state import CONTROL_DIR, read_json, write_json

INTERVAL_SECONDS = 600
DECISION_STALE_SECONDS = 6 * 3600
MAX_RETRIES = 3
TRIAGE_PATH = CONTROL_DIR / "triage.json"
TRANSIENT = ("local llm error", "review error", "timeouterror", "deadline", "throttled", "connection")


def classify(task: dict[str, Any]) -> tuple[str, str | None]:
    """Return (cause, remedy). remedy None means escalate or leave to normal retry."""
    note = str(task.get("note") or "").lower()
    retries = int(task.get("retry_count", 0) or 0)
    artifact = str(task.get("artifact") or "")
    agent_summary = Path(artifact).with_suffix(".agent.json") if artifact else None
    if any(marker in note for marker in TRANSIENT):
        return "transient", "ready"
    if note.startswith("landing") or "base moved" in note:
        return "base_moved", "rebase"
    if "patch artifact missing" in note:
        return "missing_patch", "ready"
    if note.startswith("model refused") and not (agent_summary and agent_summary.exists()):
        return "stale_engine", "ready"
    if retries >= MAX_RETRIES:
        return "retries_exhausted", None
    return "implementer", None


def run_triage(force: bool = False) -> dict[str, Any]:
    state = read_json(TRIAGE_PATH, {"last_run_at": None, "log": []})
    last = state.get("last_run_at")
    if last and not force:
        try:
            age = datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(last.replace("Z", "+00:00")).timestamp()
        except ValueError:
            age = INTERVAL_SECONDS
        if age < INTERVAL_SECONDS:
            return {"skipped": True, "next_in_seconds": int(INTERVAL_SECONDS - age)}
    actions: list[dict[str, Any]] = []
    dirty = False
    with _CLAIM_LOCK:
        data = _load()
        for task in data.get("tasks", []):
            status = task.get("status")
            if status == "blocked":
                cause, remedy = classify(task)
                if remedy in ("ready", "rebase"):
                    task.update({"status": "ready", "worker": "", "reviewer": "",
                                 "phase": "rebase_needed" if remedy == "rebase" else "retry_queued",
                                 "last_error": task.get("note"), "note": f"triage: {cause}, requeued without retry cost",
                                 "updated_at": now()})
                    actions.append({"task": task["id"], "cause": cause, "action": "requeued"})
                elif cause == "retries_exhausted" and task.get("phase") != "needs_claude" and not task.get("escalation"):
                    # includes tasks parked as needs_operator before the ladder existed
                    # Ladder: local pool spent -> Codex pipeline; no Codex -> Claude Code.
                    record = hand_to_codex(task)
                    if record:
                        task.update({"status": "escalated", "phase": "codex", "escalation": record,
                                     "note": f"local pool exhausted; handed to Codex job {record['job_id']}", "updated_at": now()})
                        actions.append({"task": task["id"], "cause": cause, "action": "codex"})
                    else:
                        task.update({"phase": "needs_claude", "updated_at": now()})
                        actions.append({"task": task["id"], "cause": cause, "action": "claude"})
            elif status == "escalated":
                record = task.get("escalation") or {}
                outcome, details = codex_outcome(record) if record.get("engine") == "codex" else ("dead", {"reason": "no record"})
                if outcome == "done":
                    task.update({"status": "done", "phase": "landed", "commit": details.get("commit"), "branch": details.get("branch"),
                                 "note": f"Codex pipeline landed {details.get('branch')} at {details.get('commit')}", "updated_at": now()})
                    actions.append({"task": task["id"], "cause": "codex_done", "action": "done"})
                elif outcome in ("dead", "timeout"):
                    task.update({"status": "blocked", "phase": "needs_claude",
                                 "last_error": f"codex: {details.get('reason')}", "note": "Codex could not finish; Claude Code to act",
                                 "updated_at": now()})
                    actions.append({"task": task["id"], "cause": f"codex_{outcome}", "action": "claude"})
                else:
                    task["escalation"] = {**record, "codex": details}
                    dirty = True
            elif status == "needs_decision":
                try:
                    waited = datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(
                        str(task.get("updated_at")).replace("Z", "+00:00")).timestamp()
                except ValueError:
                    waited = 0
                if waited > DECISION_STALE_SECONDS and task.get("phase") != "needs_operator":
                    task.update({"phase": "needs_operator", "updated_at": now()})
                    actions.append({"task": task["id"], "cause": "decision_pending", "action": "escalated"})
        if actions or dirty:
            _save(data)
    stamp = now()
    log = (state.get("log") or [])[-49:]
    log.append({"at": stamp, "actions": actions})
    write_json(TRIAGE_PATH, {"last_run_at": stamp, "log": log})
    return {"skipped": False, "at": stamp, "actions": actions}


def status() -> dict[str, Any]:
    state = read_json(TRIAGE_PATH, {"last_run_at": None, "log": []})
    recent = [entry for entry in (state.get("log") or []) if entry.get("actions")][-10:]
    return {"last_run_at": state.get("last_run_at"), "interval_seconds": INTERVAL_SECONDS, "recent": recent}
