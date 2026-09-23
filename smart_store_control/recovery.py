"""Local-only supervisor for stalled smart_store PM/worker activity."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone

from . import doctor
from .local_llm import complete
from .pm import _CLAIM_LOCK as pm_lock, _load as pm_load
from .pm import TASKS_PATH, effective_capacity, finish, implementer_lanes, requeue_stale, status as pm_status
from .pm_cycle import ARTIFACT_DIR, RUN_PATH
from .state import CONTROL_DIR, read_json, write_json
from .worker import run_once
from .review import run_once as review_once


_LOCK = threading.Lock()
STALE_MINUTES = 30
REVIEW_STALE_MINUTES = 10
DIAGNOSIS_EVERY_SECONDS = 30 * 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp() -> str:
    return _now().isoformat().replace("+00:00", "Z")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def current() -> dict:
    return read_json(RUN_PATH, {"status": "idle", "updated_at": None})


def _save(value: dict) -> None:
    write_json(RUN_PATH, value)


def _requeue_stale() -> list[int]:
    # Heartbeat-based and lock-protected (see pm.requeue_stale). The old rule
    # keyed on started_at, so a dead holder was ignored for 30 minutes while a
    # long but healthy run could be yanked; and it wrote the ledger unlocked.
    return requeue_stale()


def _run() -> None:
    try:
        recovered = _requeue_stale()
        state = pm_status()
        capacity = effective_capacity("local-impl")
        if capacity["effective"] < 1:
            _save({"status": "blocked", "reason": "AIOS 우선 슬롯 또는 핸드오프 미충족", "requeued": recovered, "updated_at": _stamp()})
            return
        endpoint = read_json(CONTROL_DIR / "runtime.json", {}).get("local_llm", {}).get("endpoint", "http://127.0.0.1:8081")
        prompt = (
            "smart_store supervisor diagnosis. AIOS is a separate read-only project. "
            "Check why local implementation may be idle or stalled, recommend safe remediation, "
            "and do not change code, use Codex, or make external marketplace/payment calls. "
            f"Capacity={json.dumps(capacity, ensure_ascii=False)}\nState={json.dumps(state, ensure_ascii=False)}"
        )
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        previous = max(ARTIFACT_DIR.glob("recovery-*.md"), key=lambda p: p.stat().st_mtime, default=None)
        fresh = previous is not None and _now().timestamp() - previous.stat().st_mtime < DIAGNOSIS_EVERY_SECONDS
        artifact = previous
        if recovered or not fresh:
            # Every recovery start used to ask the shared model for a diagnosis,
            # so a queue the workers could not drain paid for one every tick.
            # Ask only when something actually stalled, or once per interval.
            try:
                diagnosis = complete(prompt, endpoint=endpoint, model="qwen3.6-35b-a3b", timeout=30)
            except Exception as exc:
                # Diagnosis is observability, not a gate. Keep the implementation
                # stream moving when the local PM endpoint is slow or unavailable.
                diagnosis = f"diagnosis unavailable: {type(exc).__name__}: {exc}"[:500]
            artifact = ARTIFACT_DIR / f"recovery-{_now().strftime('%Y%m%dT%H%M%SZ')}.md"
            artifact.write_text(diagnosis, encoding="utf-8")
        started = dispatch()
        _save({"status": "completed", "requeued": recovered, "artifact": str(artifact), "dispatched": started, "updated_at": _stamp()})
    except Exception as exc:
        _save({"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:300], "updated_at": _stamp()})
    finally:
        _LOCK.release()


_SLOTS: dict[str, threading.Thread] = {}
_SLOTS_LOCK = threading.Lock()


def slot_names() -> list[str]:
    """One name per effective slot: local slots, then each spare-capacity lane's slots."""
    names = [f"local-{index}" for index in range(1, int(effective_capacity("local-impl")["effective"]) + 1)]
    for lane in implementer_lanes():
        if lane != "local-impl":
            names += [f"{lane}-{index}" for index in range(1, int(effective_capacity(lane)["effective"]) + 1)]
    return names


def _slot(name: str) -> None:
    """One claim cycle for one slot. Claims enforce capacity, so a surplus slot just idles."""
    try:
        if name.startswith("local-"):
            index = name.rsplit("-", 1)[1]
            # Reviews first: a reviewed patch lands and unblocks more than a new draft.
            if review_once(f"local-review-{index}").get("status") == "idle":
                run_once(f"local-impl-{index}")
        else:
            run_once(name)
    except Exception as exc:  # noqa: BLE001 - a slot thread must never take the dispatcher down
        logging.getLogger(__name__).warning("slot %s failed: %s: %s", name, type(exc).__name__, exc)


def dispatch() -> list[str]:
    """Start a worker on every slot whose previous worker has finished; returns the slots started.

    The pool used to run in batches: a recovery run started one kind of job
    (all reviews, or all implementations) on every slot and waited for the
    whole batch, so one pending review idled the other local slot and both
    spare lanes for up to the 25-minute agent wall clock. Each slot now
    refills independently as soon as its own worker returns.
    """
    started = []
    with _SLOTS_LOCK:
        for name in slot_names():
            thread = _SLOTS.get(name)
            if thread is not None and thread.is_alive():
                continue
            thread = threading.Thread(target=_slot, args=(name,), name=f"smart-store-slot-{name}", daemon=True)
            _SLOTS[name] = thread
            thread.start()
            started.append(name)
    return started


def _doctor() -> None:
    try:
        doctor.run_pending()
    except Exception as exc:  # noqa: BLE001 - the doctor must never take the dispatcher down
        logging.getLogger(__name__).warning("doctor failed: %s: %s", type(exc).__name__, exc)


def start_doctor() -> bool:
    """Run the doctor worker (doctor.py) when a failed attempt awaits diagnosis; True when started.

    It is a slot of its own, outside the implementer capacity: most failures are
    explained by probes that need no model, and a claim waits for the diagnosis.
    """
    with _SLOTS_LOCK:
        thread = _SLOTS.get("doctor")
        if thread is not None and thread.is_alive():
            return False
        with pm_lock:
            todo = doctor.pending(pm_load().get("tasks", []))
        if not todo:
            return False
        thread = threading.Thread(target=_doctor, name="smart-store-doctor", daemon=True)
        _SLOTS["doctor"] = thread
        thread.start()
        return True


def start() -> dict:
    if not _LOCK.acquire(blocking=False):
        return {"status": "running", "message": "자동 재점검·복구가 이미 실행 중입니다."}
    _save({"status": "diagnosing", "started_at": _stamp(), "updated_at": _stamp()})
    threading.Thread(target=_run, daemon=True).start()
    return {"status": "diagnosing", "message": "재점검과 자동 복구를 시작했습니다."}
