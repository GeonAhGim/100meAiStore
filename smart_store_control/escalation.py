"""Escalation ladder: local pool -> Codex pipeline -> Claude Code.

The local model gets bounded retries. When its budget is spent, the task is
handed to the Codex ``dev.task`` pipeline in ``smart_store_aios`` (spec,
implement, worker-verified tests, publish on a branch). While Codex is
unavailable (usage limit) the job waits there; if it waits longer than
``CODEX_WAIT_SECONDS`` or ends dead, the task is raised to ``needs_claude``
for the Claude Code loop, which acts on it directly. Every hop is recorded
on the task under ``escalation`` so the dashboard can show where it is.

Runs inside the ten-minute triage. Nothing here calls a model. Each triage
run also rewrites ``data/control/handoff.md``: every task that left the local
pool, or is blocked in it, with its diagnosis, attempt history, artifacts and
prompt, so Codex or a Claude Code session can pick it up without the ledger.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import loopguard
from .pm import now
from .state import CONTROL_DIR, read_json, write_text

HANDOFF_PATH = CONTROL_DIR / "handoff.md"

CODEX_WAIT_SECONDS = 24 * 3600
# A job nobody claims (Codex daemon down, quota spent) is not "in progress":
# task 18 sat queued with zero attempts for 20 hours and held M4.7 behind it.
CODEX_PICKUP_SECONDS = 2 * 3600
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
    guard = task.get("loop_guard") or {}
    if guard:
        learned += f"\nLoop guard: {guard.get('reason')} ({guard.get('attempts')})."
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
    try:
        ready_at = datetime.fromisoformat(str(job.get("available_at")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        ready_at = started
    waited = datetime.now(timezone.utc).timestamp() - max(started, ready_at)
    if job["status"] == "queued" and not int(job.get("attempts") or 0) and waited > CODEX_PICKUP_SECONDS:
        return "timeout", {"reason": f"codex job {job['id']} was never picked up in {int(waited // 3600)}h "
                                     "(no Codex worker running or quota spent)", "available_at": job.get("available_at")}
    if datetime.now(timezone.utc).timestamp() - started > CODEX_WAIT_SECONDS:
        return "timeout", {"reason": f"codex job {job['id']} still {job['status']} after {CODEX_WAIT_SECONDS // 3600}h",
                           "available_at": job.get("available_at")}
    return "waiting", {"status": job["status"], "available_at": job.get("available_at"),
                       "stage": checkpoint.get("stage"), "last_error": str(job.get("last_error") or "")[:160]}


def withdraw_codex(record: dict[str, Any], reason: str) -> bool:
    """Take an unclaimed Codex job back so a Codex worker started later does not duplicate Claude Code's work."""
    db = _db() if record.get("engine") == "codex" and record.get("job_id") else None
    return bool(db and db.withdraw(int(record["job_id"]), reason))


def _owner(task: dict[str, Any]) -> str:
    if task.get("status") == "escalated":
        return "codex"
    if task.get("phase") == "needs_claude":
        return "claude"
    return "local"


def handoff_markdown(tasks: list[dict[str, Any]]) -> str:
    rows = [t for t in tasks if t.get("status") in ("blocked", "escalated") or t.get("phase") == "needs_claude"]
    titles = {"codex": "Codex에 위임됨 (dev.task 대기·진행)", "claude": "Claude Code 조치 필요",
              "local": "로컬 워커풀에서 차단됨 (재시도·루프 가드 대상)"}
    out = ["# smart_store 인수인계", "",
           f"생성: {now()} · 관제 원장 data/control/tasks.json 기준, 10분마다 triage가 다시 씀.", "",
           "로컬 풀이 반복 실패하거나 비효율적인 작업은 Codex(`dev.task`, AIOS store.db) 또는 Claude Code로 넘어간다. "
           "각 항목은 원장 없이도 이어서 작업할 수 있도록 원인, 시도 이력, 산출물, 원래 지시를 담는다.", ""]
    if not rows:
        return "\n".join(out + ["넘길 작업 없음.", ""])
    for owner in ("claude", "codex", "local"):
        group = [t for t in rows if _owner(t) == owner]
        if not group:
            continue
        out += [f"## {titles[owner]} ({len(group)})", ""]
        for t in sorted(group, key=lambda t: (-int(t.get("priority", 0)), int(t["id"]))):
            note = " ".join(str(t.get("note") or "").split())  # one bullet line, even for a CLI's multi-line stderr
            guard = t.get("loop_guard") or {}
            cause = guard.get("cause") or loopguard.cause_of(note)
            codex = (t.get("escalation") or {})
            out += [f"### #{t['id']} {t.get('title', '')}", "",
                    f"- 상태: {t.get('status')} / {t.get('phase')} · 마일스톤 {t.get('milestone')} · 우선순위 {t.get('priority')}",
                    f"- 원인: {cause}" + (f" · 루프 가드 {guard.get('stage')}: {guard.get('reason')}" if guard else ""),
                    f"- 시도: {loopguard.summary(t)} · retry {t.get('retry_count', 0)}",
                    f"- 마지막 노트: {note[:400]}"]
            last_error = " ".join(str(t.get("last_error") or "").split())
            if last_error and last_error != note:
                out.append(f"- 직전 오류: {last_error[:400]}")
            diagnosis = t.get("diagnosis") or {}
            if diagnosis.get("fix"):
                out.append(f"- 진단({diagnosis.get('source')}{', 반복' if diagnosis.get('repeat') else ''}): "
                           f"{diagnosis.get('cause', '')} → {diagnosis['fix'][:400]}")
            if codex.get("job_id"):
                state = (codex.get("codex") or {}).get("status", "queued")
                out.append(f"- Codex job {codex['job_id']} ({codex.get('task_id')}) · {state}")
            hint = loopguard.instruction_for(cause)
            if hint:
                out.append(f"- 진단 지시: {hint}")
            for label, key in (("패치", "artifact"), ("리뷰", "review_artifact")):
                if t.get(key):
                    out.append(f"- {label}: `{t[key]}`")
            out += ["", "<details><summary>원래 지시</summary>", "", str(t.get("prompt") or "")[:3000], "", "</details>", ""]
    return "\n".join(out)


def write_handoff(tasks: list[dict[str, Any]], path: Path | None = None) -> Path:
    target = path or HANDOFF_PATH
    write_text(target, handoff_markdown(tasks))
    return target
