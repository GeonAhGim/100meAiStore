"""Automatic local-first supervisor for smart_store."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from .pm import ensure_workflow_tasks, requeue_blocked, status as pm_status
from .recovery import current as recovery_status, start as start_recovery
from .state import CONTROL_DIR, grant_handoff, read_json, write_json

_STOP = threading.Event()
_THREAD: threading.Thread | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def status() -> dict:
    return read_json(CONTROL_DIR / "autopilot.json", {"enabled": False, "local_first": True})


def tick() -> dict:
    config = status()
    if not config.get("enabled") or not config.get("local_first"):
        return {"action": "disabled"}
    pm = pm_status()
    runtime = read_json(CONTROL_DIR / "runtime.json", {})
    if (
        config.get("auto_handoff", True)
        and not runtime.get("handoff_granted")
        and not runtime.get("auto_handoff_blocked")
        and int(pm.get("capacity", {}).get("aios_available", 0)) > 0
    ):
        grant_handoff()
        pm = pm_status()
    queued = []
    reopened = []
    if not any(task.get("status") in {"ready", "needs_review", "in_progress", "reviewing"} for task in pm["tasks"]):
        reopened = requeue_blocked(limit=2)
        if reopened:
            pm = pm_status()
    if not any(task.get("status") in {"ready", "needs_review", "in_progress", "reviewing"} for task in pm["tasks"]):
        queued = ensure_workflow_tasks(limit=2)
        if queued:
            pm = pm_status()
    recovery = recovery_status()
    if recovery.get("status") in {"diagnosing", "running"}:
        recovery_updated = _parse(recovery.get("updated_at") or recovery.get("started_at"))
        if not recovery_updated or (_now() - recovery_updated).total_seconds() <= 180:
            return {"action": "waiting", "reason": "recovery already running"}
        # A previous process may have exited after persisting the state file.
        # Treat an old marker as stale so the local flow can self-heal.
        recovery = {"status": "stale"}
    running = [task for task in pm["tasks"] if task.get("status") in {"in_progress", "reviewing"}]
    stale_running = [task for task in running if task.get("health") == "stale"]
    if running and not stale_running:
        return {"action": "waiting", "reason": "worker active", "tasks": [task["id"] for task in running]}
    if stale_running:
        result = start_recovery()
        return {"action": "stale-recovery", "result": result, "tasks": [task["id"] for task in stale_running]}
    review_pending = int(pm.get("review_pending", 0))
    ready = [task for task in pm["tasks"] if task.get("status") in {"ready", "needs_review"}]
    last = _parse(config.get("last_started_at"))
    cooldown = int(config.get("cooldown_seconds", 1800))
    # A review queue is actionable work, not a repeated diagnostic storm. Keep
    # draining it one item at a time even while the normal PM cooldown is active.
    if not review_pending and not ready and not queued and last and (_now() - last).total_seconds() < cooldown:
        return {"action": "cooldown", "until_seconds": cooldown - int((_now() - last).total_seconds())}
    has_stale_worker = any(task.get("health") == "stale" for task in pm["tasks"])
    if not ready and not has_stale_worker:
        return {"action": "idle", "reason": "no eligible milestone or warning", "queued": queued, "reopened": [task["id"] for task in reopened]}
    result = start_recovery()
    if result.get("status") in {"diagnosing", "running"}:
        config["last_started_at"] = _now().isoformat().replace("+00:00", "Z")
        write_json(CONTROL_DIR / "autopilot.json", config)
    return {"action": "recovery", "result": result, "queued": [task["id"] for task in queued], "reopened": [task["id"] for task in reopened]}


def _loop() -> None:
    while not _STOP.wait(int(status().get("poll_seconds", 20))):
        try:
            tick()
        except Exception:
            continue


def start() -> dict:
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        return {"status": "running"}
    _STOP.clear()
    _THREAD = threading.Thread(target=_loop, name="smart-store-autopilot", daemon=True)
    _THREAD.start()
    return {"status": "running"}


def stop() -> dict:
    _STOP.set()
    return {"status": "stopped"}
