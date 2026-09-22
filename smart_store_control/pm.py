"""Independent smart_store PM and file-backed worker-pool scheduler."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
import threading
from pathlib import Path
from typing import Any

from .state import CONTROL_DIR, read_json, snapshot, write_json


TASKS_PATH = CONTROL_DIR / "tasks.json"
POOLS_PATH = CONTROL_DIR / "pools.json"
_CLAIM_LOCK = threading.Lock()


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
    effective = max(min(configured, available), min(configured, floor)) if handoff else 0
    return {
        "role": role,
        "configured": configured,
        "aios_available": available,
        "operator_floor": floor,
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
    active = [t for t in view_tasks if t.get("status") in {"in_progress", "reviewing"}]
    pool_cfg = read_json(POOLS_PATH, {}).get("pools", {}).get("local-impl", {})
    target = int(pool_cfg.get("min_size", pool_cfg.get("size", 2)))
    return {
        "project": "smart_store", "tasks": view_tasks, "counts": counts,
        "attention": attention, "review_pending": review_pending,
        "capacity": capacity,
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


def claim(worker: str, role: str = "local-impl") -> dict[str, Any] | None:
    with _CLAIM_LOCK:
        data = _load()
        tasks = data.get("tasks", [])
        cap = effective_capacity(role)
        active = sum(1 for t in tasks if t.get("status") in {"in_progress", "reviewing"})
        if cap["effective"] <= active:
            return None
        candidates = [t for t in tasks if t.get("role") == role and t.get("status") == "ready"]
        if not candidates:
            return None
        task = sorted(candidates, key=lambda t: (-int(t.get("priority", 0)), int(t["id"])))[0]
        task.update({"status": "in_progress", "worker": worker, "phase": "claimed", "heartbeat_at": now(), "started_at": now(), "updated_at": now(), "last_error": None})
        _save(data)
        return task


def finish(task_id: int, status_name: str, artifact: str = "", note: str = "") -> dict[str, Any]:
    data = _load()
    for task in data.get("tasks", []):
        if int(task["id"]) == task_id:
            task.update({"status": status_name, "artifact": artifact, "note": note, "phase": "finished" if status_name != "blocked" else "error", "heartbeat_at": now(), "updated_at": now()})
            _save(data)
            return task
    raise KeyError(f"unknown task {task_id}")


def touch(task_id: int, worker: str, phase: str) -> bool:
    data = _load()
    for task in data.get("tasks", []):
        if int(task["id"]) == task_id and task.get("status") == "in_progress" and task.get("worker") == worker:
            task.update({"phase": phase, "heartbeat_at": now(), "updated_at": now()})
            _save(data)
            return True
    return False


def claim_review(worker: str = "local-review-1") -> dict[str, Any] | None:
    with _CLAIM_LOCK:
        data = _load()
        tasks = data.get("tasks", [])
        cap = effective_capacity("local-impl")
        active = sum(1 for t in tasks if t.get("status") in {"in_progress", "reviewing"})
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
    data = _load()
    for task in data.get("tasks", []):
        if int(task["id"]) == task_id:
            final = "reviewed" if decision == "pass" else "blocked"
            task.update({"status": final, "review_decision": decision, "review_artifact": artifact, "note": note, "phase": "review_finished", "heartbeat_at": now(), "updated_at": now()})
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
        if any(task.get("status") in {"ready", "in_progress", "reviewing", "needs_review", "reviewed", "done", "blocked"} for task in history):
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
        ]
        candidates.sort(key=lambda task: (-int(task.get("priority", 0)), int(task["id"])))
        reopened = []
        for task in candidates[:limit]:
            retries = int(task.get("retry_count", 0)) + 1
            task.update({
                "status": "ready", "worker": "", "reviewer": "",
                "phase": "retry_queued", "retry_count": retries,
                "review_decision": None,
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
