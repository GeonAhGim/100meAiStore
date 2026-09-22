"""One-shot local-LLM PM cycle for smart_store only."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from .local_llm import complete
from .pm import effective_capacity, status as pm_status
from .state import CONTROL_DIR, read_json, write_json

RUN_PATH = CONTROL_DIR / "pm_run.json"
ARTIFACT_DIR = CONTROL_DIR / "artifacts" / "pm"
_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def current() -> dict:
    return read_json(RUN_PATH, {"status": "idle", "updated_at": None})


def _prompt() -> str:
    state = pm_status()
    milestones = read_json(CONTROL_DIR / "milestones.json", {})
    return (
        "You are the local LLM PM for the smart_store project only. "
        "The separate AIOS project is read-only and must not be changed. "
        "Recommend the next 1-3 small, testable tasks. Do not execute code, do not call Codex, "
        "and do not make marketplace or payment calls. Return concise Korean Markdown with "
        "priority, task title, scope, files, tests, dependencies, and risks.\n\n"
        f"MILESTONES:\n{json.dumps(milestones, ensure_ascii=False)}\n\n"
        f"PM STATE:\n{json.dumps(state, ensure_ascii=False)}"
    )


def _run(endpoint: str) -> None:
    try:
        response = complete(_prompt(), endpoint=endpoint, model="qwen3.6-35b-a3b")
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        artifact = ARTIFACT_DIR / f"pm-{stamp}.md"
        artifact.write_text(response, encoding="utf-8")
        _write({"status": "completed", "artifact": str(artifact), "updated_at": _now()})
    except Exception as exc:
        _write({"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:300], "updated_at": _now()})
    finally:
        _LOCK.release()


def _write(value: dict) -> None:
    write_json(RUN_PATH, value)


def start() -> dict:
    if not _LOCK.acquire(blocking=False):
        return {"status": "running", "message": "이미 로컬 LLM PM이 실행 중입니다."}
    cap = effective_capacity("local-impl")
    state = pm_status()
    if cap["effective"] < 1:
        _LOCK.release()
        return {"status": "blocked", "message": "AIOS 우선 슬롯 또는 smart_store 핸드오프가 준비되지 않았습니다.", "capacity": cap}
    if any(t.get("status") == "in_progress" for t in state["tasks"]):
        _LOCK.release()
        return {"status": "blocked", "message": "실행 중인 smart_store 워커가 있어 PM 호출을 겹치지 않습니다."}
    endpoint = read_json(CONTROL_DIR / "runtime.json", {}).get("local_llm", {}).get("endpoint", "http://127.0.0.1:8081")
    _write({"status": "running", "started_at": _now(), "updated_at": _now()})
    threading.Thread(target=_run, args=(endpoint,), daemon=True).start()
    return {"status": "running", "message": "로컬 LLM PM 호출을 시작했습니다."}
