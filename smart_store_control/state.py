"""Small, file-backed control plane for smart_store.

The control plane is intentionally conservative: AIOS owns the primary local
LLM capacity. smart_store starts paused and can only claim its own reserved
slot after an explicit operator handoff.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .aios_probe import probe as probe_aios


ROOT = Path(__file__).resolve().parents[1]
CONTROL_DIR = ROOT / "data" / "control"
MILESTONES_PATH = CONTROL_DIR / "milestones.json"
RUNTIME_PATH = CONTROL_DIR / "runtime.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        # Windows refuses to replace a file another process is reading at
        # that instant (the dashboard polls this ledger). A short bounded
        # retry keeps the write atomic instead of letting a heartbeat die.
        for attempt in range(20):
            try:
                os.replace(tmp_name, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def snapshot() -> dict[str, Any]:
    milestones = read_json(MILESTONES_PATH, {"milestones": []})
    runtime = read_json(
        RUNTIME_PATH,
        {
            "aios_priority": True,
              "handoff_granted": False,
              "auto_handoff_blocked": False,
            "local_llm": {"enabled": False, "endpoint": "http://127.0.0.1:8081"},
              "slots": {"aios_reserved": 6, "smart_store_reserved": 2, "active": 0},
            "updated_at": None,
        },
    )
    items = milestones.get("milestones", [])
    counts = {"done": 0, "in_progress": 0, "ready": 0, "blocked": 0, "planned": 0}
    for item in items:
        status = item.get("status", "planned")
        counts[status if status in counts else "planned"] += 1
    live = probe_aios()
    runtime.setdefault("slots", {})["available_now"] = live["available_slots"]
    return {
        "project": "smart_store",
        "generated_at": utc_now(),
        "priority": "after_aios",
        "aios": {
            "priority": True,
            "integration": "read_only_reference",
            "smart_store_must_not_modify_aios": True,
        },
        "runtime": runtime,
        "aios_live": live,
        "milestones": milestones,
        "counts": counts,
    }


def grant_handoff() -> dict[str, Any]:
    """Enable the reserved smart_store slot only by explicit operator action."""
    runtime = read_json(RUNTIME_PATH, {})
    live = probe_aios()
    if live["available_slots"] < 1:
        runtime.update({"handoff_granted": False, "updated_at": utc_now()})
        runtime.setdefault("local_llm", {})["enabled"] = False
        runtime.setdefault("slots", {})["active"] = 0
        runtime["handoff_blocked_reason"] = "AIOS is using all verified local LLM slots"
        write_json(RUNTIME_PATH, runtime)
        return runtime
    runtime.update(
        {
            "aios_priority": True,
            "handoff_granted": True,
            "auto_handoff_blocked": False,
            "updated_at": utc_now(),
        }
    )
    runtime.setdefault("local_llm", {"enabled": False, "endpoint": "http://127.0.0.1:8081"})
    runtime.setdefault("slots", {"aios_reserved": 6, "smart_store_reserved": 2, "active": 0})
    runtime["slots"]["smart_store_reserved"] = max(2, int(runtime["slots"].get("smart_store_reserved", 2)))
    runtime["local_llm"]["enabled"] = True
    runtime["slots"]["active"] = min(
        int(runtime["slots"].get("smart_store_reserved", 2)),
        int(live["available_slots"]),
    )
    runtime["handoff_blocked_reason"] = None
    write_json(RUNTIME_PATH, runtime)
    return runtime


def revoke_handoff() -> dict[str, Any]:
    runtime = read_json(RUNTIME_PATH, {})
    runtime.update({"handoff_granted": False, "auto_handoff_blocked": True, "updated_at": utc_now()})
    runtime.setdefault("local_llm", {})["enabled"] = False
    runtime.setdefault("slots", {})["active"] = 0
    write_json(RUNTIME_PATH, runtime)
    return runtime
