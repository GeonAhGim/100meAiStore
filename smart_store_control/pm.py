"""Independent smart_store PM and file-backed worker-pool scheduler."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
import threading
from pathlib import Path
from typing import Any

from . import loopguard
from .state import CONTROL_DIR, read_json, snapshot, write_json


TASKS_PATH = CONTROL_DIR / "tasks.json"
POOLS_PATH = CONTROL_DIR / "pools.json"
_CLAIM_LOCK = threading.RLock()  # guards every read-modify-write of tasks.json in this process


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _health(task: dict[str, Any]) -> str:
    if task.get("status") in {"in_progress", "reviewing"}:
        raw = task.get("heartbeat_at") or task.get("started_at")
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(raw.replace("Z", "+00:00"))).total_seconds()
        except (AttributeError, TypeError, ValueError):
            return "unknown"
        return "healthy" if age <= 45 else "stale"
    if task.get("status") == "blocked":
        return "error"
    if task.get("status") == "done":
        return "completed"
    return "queued"


def _load() -> dict[str, Any]:
    return read_json(TASKS_PATH, {"version": 1, "next_id": 1, "tasks": []})


def _save(data: dict[str, Any]) -> None:
    write_json(TASKS_PATH, data)


def effective_capacity(role: str = "local-impl") -> dict[str, Any]:
    pools = read_json(POOLS_PATH, {}).get("pools", {})
    pool = pools.get(role, {})
    live = snapshot()["aios_live"]
    runtime = read_json(CONTROL_DIR / "runtime.json", {})
    handoff = bool(runtime.get("handoff_granted"))
    configured = min(int(pool.get("size", 0)), int(pool.get("max_size", pool.get("size", 0))))
    available = int(live.get("available_slots", 0))
    # Explicit operator decision: keep at least this many smart_store slots
    # running even when AIOS holds every probed slot (llama.cpp queues the
    # extra request). 0 by default, so AIOS priority is unchanged unless set.
    floor = max(0, int(runtime.get("slots", {}).get("operator_floor", 0)))
    paused_until = str((runtime.get("lanes") or {}).get(role, {}).get("paused_until") or "")
    paused = paused_until > now()
    if str(pool.get("engine", "")) in EXTERNAL_ENGINES:
        # Spare-capacity lanes (cursor, gemini) do not use the shared local
        # llama.cpp, so AIOS priority and the handoff do not apply to them.
        # A lane that hit its provider quota is paused until paused_until.
        effective = 0 if paused else configured
    else:
        effective = max(min(configured, available), min(configured, floor)) if handoff else 0
    return {
        "role": role,
        "engine": str(pool.get("engine", "local-http")),
        "configured": configured,
        "aios_available": available,
        "operator_floor": floor,
        "paused_until": paused_until if paused else None,
        "effective": effective,
        "handoff_granted": handoff,
        "aios_priority": True,
        "live": live,
    }


def status() -> dict[str, Any]:
    data = _load()
    tasks = data.get("tasks", [])
    counts: dict[str, int] = {}
    for task in tasks:
        counts[task.get("status", "unknown")] = counts.get(task.get("status", "unknown"), 0) + 1
    view_tasks = [dict(task, health=_health(task)) for task in tasks]
    attention = sum(1 for task in view_tasks if task["health"] in {"stale", "error"})
    review_pending = sum(1 for task in view_tasks if task.get("status") == "needs_review")
    capacity = effective_capacity()
    lanes = [effective_capacity(lane) for lane in implementer_lanes()]
    active = [t for t in view_tasks if t.get("status") in {"in_progress", "reviewing"}]
    pool_cfg = read_json(POOLS_PATH, {}).get("pools", {}).get("local-impl", {})
    target = int(pool_cfg.get("min_size", pool_cfg.get("size", 2)))
    return {
        "project": "smart_store", "tasks": view_tasks, "counts": counts,
        "attention": attention, "review_pending": review_pending,
        "capacity": capacity,
        "lanes": lanes,
        "worker_pool": {
            "name": "smart-store-local", "target": target,
            "configured": capacity["configured"], "effective": capacity["effective"],
            "active": len(active), "deficit": max(target - capacity["effective"], 0),
            "aios_available": capacity["aios_available"],
            "in_progress": sum(1 for t in active if t.get("status") == "in_progress"),
            "reviewing": sum(1 for t in active if t.get("status") == "reviewing"),
            "ready": sum(1 for t in view_tasks if t.get("status") == "ready"),
            "needs_review": review_pending,
            "workers": [{"worker": t.get("worker") or t.get("reviewer"), "task_id": t.get("id"), "title": t.get("title"), "phase": t.get("phase"), "status": t.get("status"), "health": t.get("health"), "heartbeat_at": t.get("heartbeat_at"), "started_at": t.get("started_at")} for t in active],
        },
    }


EXTERNAL_ENGINES = ("cursor", "gemini", "claude")
IMPL_ROLE = "local-impl"  # every implementation task carries this role; any implementer lane may claim it


def lane_of(worker: str) -> str:
    """'cursor-impl-1' -> 'cursor-impl'; 'local-impl-2' -> 'local-impl'."""
    return worker.rsplit("-", 1)[0] if worker.rsplit("-", 1)[-1].isdigit() else worker


LANE_PAUSE_SECONDS = 30 * 60
LANE_FAULT_MARKERS = ("usage limit", "quota", "rate limit", "not trusted", "not logged in", "unauthorized",
                      "can't reach the api server", "enotfound", "econnrefused",
                      "authentication", "429", "actionrequirederror",
                      "503", "high demand", "overloaded", "temporarily unavailable", "resource_exhausted")


def lane_fault(text: str) -> str | None:
    """The provider-side reason an external lane cannot work right now, or None."""
    lowered = (text or "").lower()
    for marker in LANE_FAULT_MARKERS:
        if marker in lowered:
            return marker
    return None


def pause_lane(lane: str, reason: str, seconds: int = LANE_PAUSE_SECONDS) -> str:
    """Take a lane out of capacity for a while; the task it held is not charged a retry."""
    from datetime import timedelta
    until = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    runtime = read_json(CONTROL_DIR / "runtime.json", {})
    runtime.setdefault("lanes", {})[lane] = {"paused_until": until, "reason": reason[:200], "paused_at": now()}
    write_json(CONTROL_DIR / "runtime.json", runtime)
    return until


def implementer_lanes() -> list[str]:
    pools = read_json(POOLS_PATH, {}).get("pools", {})
    return [name for name, pool in pools.items()
            if name == IMPL_ROLE or str(pool.get("engine", "")) in EXTERNAL_ENGINES]


def claude_lane() -> str | None:
    """The configured Claude lane (engine "claude", size > 0), or None."""
    pools = read_json(POOLS_PATH, {}).get("pools", {})
    return next((name for name, pool in pools.items()
                 if pool.get("engine") == "claude" and int(pool.get("size", 0)) > 0), None)


def claim(worker: str, role: str = "local-impl") -> dict[str, Any] | None:
    """Claim a ready implementation task for ``worker`` in lane ``role``.

    Capacity is per lane: the local lane is bounded by shared llama.cpp slots,
    the cursor and gemini lanes by their own pool size. Active tasks are
    counted by the lane prefix of their holder, so one saturated lane never
    starves another.
    """
    with _CLAIM_LOCK:
        data = _load()
        tasks = data.get("tasks", [])
        cap = effective_capacity(role)
        active = sum(1 for t in tasks if t.get("status") == "in_progress" and lane_of(str(t.get("worker") or "")) == role)
        if role == IMPL_ROLE:
            active += sum(1 for t in tasks if t.get("status") == "reviewing")  # reviews share the local slots
        if cap["effective"] <= active:
            return None
        candidates = [t for t in tasks if t.get("role") == IMPL_ROLE and t.get("status") == "ready"
                      and (not t.get("preferred_lane") or t.get("preferred_lane") == role)
                      # the doctor diagnoses a failure before the task runs again (doctor.py)
                      and not loopguard.awaiting_diagnosis(t)]
        if not candidates:
            return None
        task = sorted(candidates, key=lambda t: (-int(t.get("priority", 0)), int(t["id"])))[0]
        task.update({"status": "in_progress", "worker": worker, "phase": "claimed", "heartbeat_at": now(),
                     "started_at": now(), "updated_at": now(), "last_error": None,
                     # M0.4 evidence: the live AIOS slot and handoff state that justified this claim
                     "claim_basis": {"lane": role, "engine": cap["engine"], "aios_available": cap["aios_available"],
                                     "handoff_granted": cap["handoff_granted"], "operator_floor": cap["operator_floor"],
                                     "effective": cap["effective"], "active_before": active}})
        _save(data)
        return task


def finish(task_id: int, status_name: str, artifact: str = "", note: str = "") -> dict[str, Any]:
    # Every read-modify-write of the ledger shares _CLAIM_LOCK. Without it a
    # heartbeat from the worker thread could overwrite a concurrent claim or
    # finish made by the autopilot or review thread (lost update), which is how
    # tasks were left "in_progress" with a frozen heartbeat.
    with _CLAIM_LOCK:
        data = _load()
        for task in data.get("tasks", []):
            if str(task["id"]) == str(task_id):
                if status_name == "blocked":
                    loopguard.record(task, note, "implement")
                task.update({"status": status_name, "artifact": artifact, "note": note,
                             "phase": "finished" if status_name != "blocked" else "error",
                             "heartbeat_at": now(), "updated_at": now()})
                _save(data)
                return task
    raise KeyError(f"unknown task {task_id}")


def release(task_id: int, note: str) -> dict[str, Any]:
    """Hand a task back to the queue because its lane failed, not the task.

    A provider quota or login fault used to finish the task as blocked, which
    recorded the lane's fault as the task's failed attempt and replaced the
    task's real last error in the next prompt. Nothing is charged here.
    """
    with _CLAIM_LOCK:
        data = _load()
        for task in data.get("tasks", []):
            if str(task["id"]) == str(task_id):
                task.update({"status": "ready", "worker": "", "phase": "lane_released", "note": note,
                             "heartbeat_at": now(), "updated_at": now()})
                _save(data)
                return task
    raise KeyError(f"unknown task {task_id}")


ACTIVE_STATES = {"in_progress", "reviewing"}
HEARTBEAT_SECONDS = 15


OPERATOR_ACTIONS = ("retry", "approve", "reject", "rereview", "park", "supersede")
MAX_INSTRUCTION_CHARS = 4000


def create_operator_task(title: str, instruction: str, *, milestone: str = "OPS", priority: int = 100,
                         files: list[str] | None = None, lane: str | None = None) -> dict[str, Any]:
    """A task the operator dictates from the dashboard. Same pipeline as every other task."""
    title, instruction = title.strip()[:120], instruction.strip()[:MAX_INSTRUCTION_CHARS]
    if not title or not instruction:
        raise ValueError("title and instruction are required")
    if lane and lane not in implementer_lanes():
        raise ValueError(f"unknown lane {lane}")
    prompt = instruction
    if files:
        prompt += "\n\nEvidence/files to inspect: " + "; ".join(f.strip() for f in files if f.strip()) + "."
    prompt += " Work offline and fail closed; do not modify AIOS or make external marketplace/payment writes."
    with _CLAIM_LOCK:
        data = _load()
        task = {"id": int(data.get("next_id", 1)), "milestone": milestone, "title": title, "role": IMPL_ROLE,
                "priority": int(priority), "status": "ready", "prompt": prompt, "source": "operator",
                "preferred_lane": lane, "operator_instructions": [], "created_at": now(), "updated_at": now(),
                "note": "operator-dictated task"}
        data["next_id"] = task["id"] + 1
        data.setdefault("tasks", []).append(task)
        _save(data)
        return dict(task)


def add_instruction(task_id: int, text: str) -> dict[str, Any]:
    """Attach an operator instruction; the next run of the task sees it in its prompt.

    A running CLI cannot be interrupted mid-turn, so the instruction lands on
    the next attempt (retry, re-review feedback, or the next lane pickup).
    """
    text = text.strip()[:MAX_INSTRUCTION_CHARS]
    if not text:
        raise ValueError("instruction text is required")
    with _CLAIM_LOCK:
        data = _load()
        for task in data.get("tasks", []):
            if str(task["id"]) == str(task_id):
                task.setdefault("operator_instructions", []).append({"at": now(), "text": text})
                task["updated_at"] = now()
                _save(data)
                return dict(task)
    raise KeyError(f"unknown task {task_id}")


def operator_action(task_id: int, action: str, reason: str = "", lane: str | None = None) -> dict[str, Any]:
    """One explicit operator decision on a task, recorded in the ledger.

    retry:    blocked/needs_decision/planned -> ready with a fresh retry budget
    approve:  needs_decision -> reviewed (the autopilot lands it)
    reject:   needs_decision -> blocked, retry budget spent, reason kept
    rereview: blocked with a patch artifact -> needs_review (objective gate again)
    park:     any non-active -> planned (out of the queue, nothing lost)

    ``lane`` (retry only) routes the task to one implementer lane, e.g. the
    Claude lane for work the local model could not finish.
    """
    if action not in OPERATOR_ACTIONS:
        raise ValueError(f"unknown action {action}")
    if lane and (action != "retry" or lane not in implementer_lanes()):
        raise ValueError(f"lane {lane} applies to retry on a configured implementer lane only")
    withdrawn = None
    with _CLAIM_LOCK:
        data = _load()
        for task in data.get("tasks", []):
            if str(task["id"]) != str(task_id):
                continue
            status = task.get("status")
            if status in ACTIVE_STATES:
                raise ValueError(f"task {task_id} is {status}; wait for it to finish")
            stamp = now()
            who = f"operator: {reason}".strip(": ") if reason else "operator"
            if action == "retry":
                # A human decided to try again: a fresh retry budget and a fresh
                # loop-guard window, or the old history would escalate it at once.
                withdrawn = task.get("escalation") if status == "escalated" else None
                if lane:
                    task["preferred_lane"] = lane
                task.update({"status": "ready", "worker": "", "reviewer": "", "phase": "retry_queued", "retry_count": 0,
                             "loop_guard": None, "escalation": None, "guard_reset_at": stamp,
                             "last_error": task.get("note"), "note": f"{who} requested retry", "updated_at": stamp})
            elif action == "approve":
                if status != "needs_decision":
                    raise ValueError("approve applies to needs_decision only")
                task.update({"status": "reviewed", "phase": "operator_approved", "review_decision": "pass",
                             "note": f"{who} approved after model objection", "updated_at": stamp})
            elif action == "reject":
                if status != "needs_decision":
                    raise ValueError("reject applies to needs_decision only")
                task.update({"status": "blocked", "phase": "operator_rejected", "review_decision": "fail",
                             "retry_count": 99, "note": f"{who} rejected", "updated_at": stamp})
            elif action == "rereview":
                if not task.get("artifact"):
                    raise ValueError("no patch artifact to review")
                task.update({"status": "needs_review", "reviewer": "", "phase": "requeued_for_gate",
                             "note": f"{who} requested re-review", "updated_at": stamp})
            elif action == "park":
                task.update({"status": "planned", "worker": "", "reviewer": "", "phase": "parked",
                             "note": f"{who} parked", "updated_at": stamp})
            elif action == "supersede":
                # Terminal without counting as delivered work: an exact duplicate of
                # another task, or work that landed elsewhere. Never re-queued.
                task.update({"status": "superseded", "worker": "", "reviewer": "", "phase": "superseded",
                             "note": f"{who} superseded", "updated_at": stamp})
            _save(data)
            if withdrawn and withdrawn.get("engine") == "codex":
                # The operator took the task back: a Codex worker started later must not redo it.
                from .escalation import withdraw_codex  # local import: escalation imports pm
                withdraw_codex(withdrawn, f"control: operator retried task {task_id} locally")
            return dict(task)
    raise KeyError(f"unknown task {task_id}")


STALE_AFTER_SECONDS = 5 * 60  # 20 missed heartbeats: the holder is gone, not slow


def requeue_stale(stale_after_seconds: int = STALE_AFTER_SECONDS, note: str = "supervisor requeued stale worker") -> list[int]:
    """Return held tasks whose heartbeat stopped to the queue.

    Keyed on ``heartbeat_at``, not ``started_at``: a live worker beats every
    ``HEARTBEAT_SECONDS`` however long its model call takes, while a worker
    whose process died (restart, crash, reboot) leaves the heartbeat frozen.
    Called at server start so orphans from the previous process are released
    immediately, and by recovery on every stale signal.
    """
    cutoff = datetime.now(timezone.utc).timestamp() - stale_after_seconds
    recovered: list[int] = []
    with _CLAIM_LOCK:
        data = _load()
        for task in data.get("tasks", []):
            if task.get("status") not in ACTIVE_STATES:
                continue
            raw = task.get("heartbeat_at") or task.get("started_at") or ""
            try:
                beat = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
            except ValueError:
                beat = 0.0
            if beat > cutoff:
                continue
            next_status = "needs_review" if task.get("status") == "reviewing" else "ready"
            # A task whose holder keeps dying (it may be what kills the server)
            # must show up as a loop too, so an orphaning counts as an attempt.
            loopguard.record(task, note, "orphaned", until=beat or None)
            task.update({"status": next_status, "worker": "", "reviewer": "", "phase": "requeued",
                         "note": note, "updated_at": now()})
            recovered.append(int(task["id"]))
        if recovered:
            _save(data)
    return recovered


def heartbeat_loop(stop: threading.Event, task_id: int, holder: str, phase: str,
                   interval: float = HEARTBEAT_SECONDS) -> None:
    """Refresh a task's heartbeat until ``stop`` is set.

    A transient failure (a concurrent reader holding the ledger, a momentary
    disk error) is logged and retried on the next tick. The loop never dies
    silently: a silent death is exactly what made live tasks look stale.
    """
    while not stop.wait(interval):
        try:
            if not touch(task_id, holder, phase):
                return  # the task is no longer ours; stop touching it
        except Exception as exc:  # noqa: BLE001 - keep beating
            logging.getLogger(__name__).warning("heartbeat for task %s failed: %s", task_id, exc)


def touch(task_id: int, worker: str, phase: str) -> bool:
    """Refresh the heartbeat of a task this worker or reviewer currently holds."""
    with _CLAIM_LOCK:
        data = _load()
        for task in data.get("tasks", []):
            if str(task["id"]) != str(task_id) or task.get("status") not in ACTIVE_STATES:
                continue
            holder = task.get("worker") if task.get("status") == "in_progress" else task.get("reviewer")
            if holder != worker:
                continue
            task.update({"phase": phase, "heartbeat_at": now(), "updated_at": now()})
            _save(data)
            return True
    return False


def claim_review(worker: str = "local-review-1") -> dict[str, Any] | None:
    with _CLAIM_LOCK:
        data = _load()
        tasks = data.get("tasks", [])
        cap = effective_capacity("local-impl")
        # Reviews run on the local slots: count local implementations and
        # reviews only. A task running on the Claude, cursor or gemini lane
        # holds no local slot, and counting it stopped every review with one slot.
        active = sum(1 for t in tasks if t.get("status") == "reviewing"
                     or (t.get("status") == "in_progress" and lane_of(str(t.get("worker") or "")) == IMPL_ROLE))
        if cap["effective"] <= active:
            return None
        candidates = [t for t in tasks if t.get("status") == "needs_review"]
        if not candidates:
            return None
        task = sorted(candidates, key=lambda t: (-int(t.get("priority", 0)), int(t["id"])))[0]
        task.update({"status": "reviewing", "reviewer": worker, "phase": "review_llm", "heartbeat_at": now(), "updated_at": now()})
        _save(data)
        return task


def finish_review(task_id: int, decision: str, artifact: str = "", note: str = "") -> dict[str, Any]:
    with _CLAIM_LOCK:
        data = _load()
        for task in data.get("tasks", []):
            if str(task["id"]) == str(task_id):
                # pass -> reviewed (landed by the autopilot); needs_decision ->
                # a human reads the model's objection, no retry spent; else blocked.
                final = {"pass": "reviewed", "needs_decision": "needs_decision"}.get(decision, "blocked")
                if final == "blocked":
                    loopguard.record(task, note, "review")
                task.update({"status": final, "review_decision": decision, "review_artifact": artifact, "note": note,
                             "phase": "review_finished", "heartbeat_at": now(), "updated_at": now()})
                _save(data)
                return task
    raise KeyError(f"unknown task {task_id}")


def enqueue(title: str, prompt: str, milestone: str, priority: int = 50) -> dict[str, Any]:
    data = _load()
    task = {"id": int(data.get("next_id", 1)), "milestone": milestone, "title": title, "role": "local-impl", "priority": priority, "status": "ready", "prompt": prompt, "created_at": now(), "updated_at": now()}
    data["next_id"] = task["id"] + 1
    data.setdefault("tasks", []).append(task)
    _save(data)
    return task


def ensure_workflow_tasks(limit: int = 2) -> list[dict[str, Any]]:
    """Keep the local implementation stream populated from the milestone graph.

    This is deliberately deterministic and local-only. It does not touch AIOS and
    does not retry a milestone that already produced a terminal result.
    """
    data = _load()
    tasks = data.get("tasks", [])
    milestones = read_json(CONTROL_DIR / "milestones.json", {}).get("milestones", [])
    existing = {}
    for task in tasks:
        existing.setdefault(str(task.get("milestone", "")), []).append(task)
    milestone_state = {str(item.get("id")): item for item in milestones}
    created: list[dict[str, Any]] = []
    candidates = sorted(
        (item for item in milestones if item.get("status") in {"ready", "in_progress"}),
        key=lambda item: (-int(item.get("priority", 0)), str(item.get("id"))),
    )
    for item in candidates:
        if len(created) >= limit:
            break
        milestone_id = str(item.get("id"))
        history = existing.get(milestone_id, [])
        # Any prior task for the milestone, in whatever state, means it was already
        # generated; parked or superseded duplicates must not spawn a third copy.
        if any(task.get("status") in {"ready", "in_progress", "reviewing", "needs_review", "reviewed", "done", "blocked",
                                      "planned", "superseded", "needs_decision", "escalated"} for task in history):
            continue
        dependencies = [milestone_state.get(str(dep)) for dep in item.get("depends_on", [])]
        if any(dep is None or dep.get("status") != "done" for dep in dependencies):
            continue
        task = enqueue(
            title=f"{milestone_id} · {item.get('title', '다음 MVP 구현 작업')}",
            prompt=(
                f"Implement milestone {milestone_id} for smart_store only. "
                f"Scope: {item.get('title', '')}. "
                f"Exit criteria: {item.get('exit_criteria', '')}. "
                f"Evidence/files to inspect: {item.get('evidence', '')}. "
                "Work offline and fail closed; do not modify AIOS, call Codex, or make external marketplace/payment writes. "
                "Return a minimal unified diff and focused tests."
            ),
            milestone=milestone_id,
            priority=int(item.get("priority", 50)),
        )
        task["source"] = "milestone-workflow"
        data = _load()
        data["tasks"][-1]["source"] = "milestone-workflow"
        _save(data)
        existing.setdefault(milestone_id, []).append(task)
        created.append(task)
    return created


def requeue_blocked(limit: int = 2, max_retries: int = 3) -> list[dict[str, Any]]:
    """Reopen failed local tasks for bounded worker-pool retries."""
    with _CLAIM_LOCK:
        data = _load()
        candidates = [
            task for task in data.get("tasks", [])
            if task.get("status") == "blocked" and int(task.get("retry_count", 0)) < max_retries
            # A looping task waits for triage's loop guard instead of another blind retry.
            and not loopguard.verdict(task, data.get("tasks", []))
        ]
        candidates.sort(key=lambda task: (-int(task.get("priority", 0)), int(task["id"])))
        reopened = []
        for task in candidates[:limit]:
            retries = int(task.get("retry_count", 0)) + 1
            task.update({
                "status": "ready", "worker": "", "reviewer": "",
                "phase": "retry_queued", "retry_count": retries,
                "review_decision": None,
                # Keep why it was blocked: the next attempt gets it as feedback
                # and an operator can still read it after the note changes.
                "last_error": task.get("note") or task.get("last_error"),
                "note": f"local worker-pool retry {retries}/{max_retries}",
                "updated_at": now(),
            })
            reopened.append(dict(task))
        if reopened:
            _save(data)
        return reopened


def main() -> None:
    parser = argparse.ArgumentParser(description="smart_store local PM")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    claim_parser = sub.add_parser("claim")
    claim_parser.add_argument("--worker", default="local-impl-1")
    enqueue_parser = sub.add_parser("enqueue")
    enqueue_parser.add_argument("title")
    enqueue_parser.add_argument("prompt")
    enqueue_parser.add_argument("--milestone", default="M1")
    enqueue_parser.add_argument("--priority", type=int, default=50)
    args = parser.parse_args()
    if args.command == "status":
        result = status()
    elif args.command == "claim":
        result = claim(args.worker) or {"claimed": False}
    else:
        result = enqueue(args.title, args.prompt, args.milestone, args.priority)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
