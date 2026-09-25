"""Automatic local-first supervisor for smart_store."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from .land import land_reviewed
from .triage import run_triage
from .pm import ensure_workflow_tasks, requeue_blocked, status as pm_status
from .recovery import current as recovery_status, dispatch, start as start_recovery, start_doctor, start_integrator
from .state import CONTROL_DIR, grant_handoff, read_json

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
    landed = land_reviewed()
    # Landed work reaches main (full suite on the merge first) and completes its
    # milestone; without this the milestone graph never advanced and the pool idled.
    # In its own thread: the suite takes minutes and, run inline, it held this
    # tick, so no slot was refilled while a merge was being tested.
    start_integrator()
    triaged = run_triage()  # self-throttled to every 10 minutes
    if landed or triaged.get("actions"):
        pm = pm_status()
    # Retry blocked work whenever the queue has nothing ready, not only when every
    # slot is idle: one long run no longer holds the other slots empty.
    if not any(task.get("status") in {"ready", "needs_review"} for task in pm["tasks"]):
        reopened = requeue_blocked(limit=2)
        if reopened:
            pm = pm_status()
    # Keep enough ready work for every lane: top up when the ready queue is
    # shorter than the summed lane capacity, not only when it is empty.
    lane_capacity = sum(int(lane.get("effective", 0)) for lane in pm.get("lanes", [])) or int(pm.get("capacity", {}).get("effective", 0))
    ready_count = sum(1 for task in pm["tasks"] if task.get("status") == "ready")
    if ready_count < max(1, lane_capacity):
        queued = ensure_workflow_tasks(limit=max(1, lane_capacity - ready_count))
        if queued:
            pm = pm_status()
    # A failed attempt is diagnosed before its task runs again (doctor.py).
    start_doctor()
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
    if stale_running:
        result = start_recovery()
        return {"action": "stale-recovery", "result": result, "tasks": [task["id"] for task in stale_running]}
    ready = [task for task in pm["tasks"] if task.get("status") in {"ready", "needs_review"}]
    if not ready:
        if running:
            return {"action": "waiting", "reason": "worker active", "tasks": [task["id"] for task in running]}
        return {"action": "idle", "reason": "no eligible milestone or warning", "queued": queued, "reopened": [task["id"] for task in reopened]}
    # Fill every free slot now. Dispatch makes no model call, so it needs no
    # cooldown; the model diagnosis stays with stale recovery.
    started = dispatch()
    return {"action": "dispatch", "started": started, "queued": [task["id"] for task in queued],
            "reopened": [task["id"] for task in reopened]}


def _loop() -> None:
    while not _STOP.wait(int(status().get("poll_seconds", 20))):
        try:
            tick()
        except Exception as exc:  # noqa: BLE001 - the loop must survive, but never silently
            logging.getLogger(__name__).warning("autopilot tick failed: %s: %s", type(exc).__name__, exc)


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
