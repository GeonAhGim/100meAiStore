"""Local-only supervisor for stalled smart_store PM/worker activity."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from .local_llm import complete
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
        after = pm_status()
        slots = max(1, int(capacity["effective"]))
        needs_review = any(t.get("status") == "needs_review" for t in after["tasks"])
        active = any(t.get("status") in {"in_progress", "reviewing"} for t in after["tasks"])
        if needs_review:
            fn, prefix = review_once, "local-review"
        elif not active:
            fn, prefix = run_once, "local-impl"
        else:
            fn, prefix = None, ""
        if fn:
            # One thread per slot per implementer lane: the local lane plus the
            # cursor/gemini spare-capacity lanes, each bounded by its own capacity.
            jobs = [(fn, f"{prefix}-{index}") for index in range(1, slots + 1)]
            if fn is run_once:
                for lane in implementer_lanes():
                    if lane == "local-impl":
                        continue
                    lane_slots = int(effective_capacity(lane)["effective"])
                    jobs += [(run_once, f"{lane}-{index}") for index in range(1, lane_slots + 1)]
            with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
                futures = [pool.submit(job_fn, name) for job_fn, name in jobs]
                worker_result = [future.result() for future in futures]
        else:
            worker_result = {"status": "not_started", "reason": "worker already active"}
        _save({"status": "completed", "requeued": recovered, "artifact": str(artifact), "worker": worker_result, "updated_at": _stamp()})
    except Exception as exc:
        _save({"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:300], "updated_at": _stamp()})
    finally:
        _LOCK.release()


def start() -> dict:
    if not _LOCK.acquire(blocking=False):
        return {"status": "running", "message": "자동 재점검·복구가 이미 실행 중입니다."}
    _save({"status": "diagnosing", "started_at": _stamp(), "updated_at": _stamp()})
    threading.Thread(target=_run, daemon=True).start()
    return {"status": "diagnosing", "message": "재점검과 자동 복구를 시작했습니다."}
