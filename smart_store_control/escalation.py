"""Escalation ladder: local pool -> Codex pipeline -> Claude Code.

The local model gets bounded retries. When its budget is spent, the task is
handed to the Codex ``dev.task`` pipeline in ``smart_store_aios`` (spec,
implement, worker-verified tests, publish on a branch). While Codex is
unavailable (usage limit) the job waits there; if it waits longer than
``CODEX_WAIT_SECONDS`` or ends dead, the task is raised to ``needs_claude``
for the Claude Code loop, which acts on it directly. Every hop is recorded
on the task under ``escalation`` so the dashboard can show where it is.

Runs inside the ten-minute triage. Nothing here calls a model.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .pm import now
from .state import CONTROL_DIR, read_json

CODEX_WAIT_SECONDS = 24 * 3600
_EXIT = re.compile(r"Exit criteria:\s*(.+?)(?:\.\s|\. Evidence|$)", re.IGNORECASE | re.DOTALL)
_FILES = re.compile(r"files to inspect:\s*(.+?)(?:\.(?:\s|$)|\n|$)", re.IGNORECASE)


def codex_database() -> Path | None:
    cfg = read_json(CONTROL_DIR / "runtime.json", {}).get("escalation", {})
    raw = cfg.get("codex_database")
    return Path(raw) if raw else None


def codex_enabled() -> bool:
    cfg = read_json(CONTROL_DIR / "runtime.json", {}).get("escalation", {})
    return bool(cfg.get("codex", True)) and codex_database() is not None


def dev_task_payload(task: dict[str, Any]) -> dict[str, Any]:
    """Translate a control task into a Codex pipeline packet with what the local pool learned."""
    prompt = str(task.get("prompt") or "")
    exit_match = _EXIT.search(prompt)
    exit_text = exit_match.group(1).strip() if exit_match else str(task.get("title") or "")
    acceptance = [part.strip() for part in re.split(r"[;·]\s*", exit_text) if part.strip()][:4] or [exit_text or "task completes"]
    files_match = _FILES.search(prompt)
    files = [f.strip().replace("\\", "/") for f in re.split(r"[;,]\s*", files_match.group(1)) if f.strip()] if files_match else []
    learned = str(task.get("last_error") or task.get("note") or "")[:600]
    return {
        "task_id": f"ctl-{task['id']}",
        "title": str(task.get("title") or f"control task {task['id']}"),
        "goal": prompt + ("\n\nThe local worker pool already tried this and failed with: " + learned if learned else ""),
        "acceptance": acceptance,
        "files": [f for f in files if "/" in f],
        "max_fix_rounds": 3,
        "source_item": f"control-task-{task['id']}",
    }


def _db():
    from smart_store_aios.db import StoreDB  # local import: the control package must not hard-depend on it
    path = codex_database()
    return StoreDB(path) if path else None


def hand_to_codex(task: dict[str, Any]) -> dict[str, Any] | None:
    """Enqueue a dev.task for this control task; returns the escalation record or None if disabled."""
    if not codex_enabled():
        return None
    db = _db()
    payload = dev_task_payload(task)
    live = db.find_job("dev.task", payload["task_id"])
    if live and live["status"] not in ("done", "dead"):
        job_id = live["id"]
    else:
        job_id = db.enqueue("dev.task", payload)
    return {"engine": "codex", "job_id": job_id, "task_id": payload["task_id"], "at": now()}


def codex_outcome(record: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """('waiting'|'done'|'dead'|'timeout', details) for a handed-off task."""
    db = _db()
    job = db.job(int(record["job_id"])) if db else None
    if job is None:
        return "dead", {"reason": "codex job missing"}
    checkpoint = job.get("checkpoint") or {}
    if job["status"] == "done" and checkpoint.get("commit"):
        return "done", {"commit": checkpoint.get("commit"), "branch": f"worker/{record['task_id']}", "spec": checkpoint.get("spec")}
    if job["status"] == "dead":
        return "dead", {"reason": str(job.get("last_error") or "")[:300]}
    try:
        started = datetime.fromisoformat(str(record.get("at")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        started = datetime.now(timezone.utc).timestamp()
    if datetime.now(timezone.utc).timestamp() - started > CODEX_WAIT_SECONDS:
        return "timeout", {"reason": f"codex job {job['id']} still {job['status']} after {CODEX_WAIT_SECONDS // 3600}h",
                           "available_at": job.get("available_at")}
    return "waiting", {"status": job["status"], "available_at": job.get("available_at"),
                       "stage": checkpoint.get("stage"), "last_error": str(job.get("last_error") or "")[:160]}
