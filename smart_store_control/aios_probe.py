"""Read-only AIOS capacity probe.

The separate AIOS project is never imported or written to. This module reads
its task snapshots and the local llama.cpp metrics endpoint only.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.request import urlopen


AIOS_PM = Path(os.environ.get("SMART_STORE_AIOS_PM", r"C:\aios\pm"))
LLAMA_URL = os.environ.get("SMART_STORE_LLAMA_URL", "http://127.0.0.1:8080")
PROBE_TTL_SECONDS = 5.0
_CACHE: dict = {"at": 0.0, "value": None}
_CACHE_LOCK = threading.Lock()
METRIC_RE = re.compile(r"^llamacpp:(requests_processing|requests_deferred)\s+([\d.]+)", re.MULTILINE)


def _json(path: str) -> dict:
    try:
        with urlopen(LLAMA_URL + path, timeout=1.5) as response:
            value = json.loads(response.read().decode("utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _metrics() -> dict[str, int]:
    try:
        with urlopen(LLAMA_URL + "/metrics", timeout=1.5) as response:
            body = response.read().decode("utf-8", "replace")
    except OSError:
        return {}
    return {key: int(float(value)) for key, value in METRIC_RE.findall(body)}


def _aios_task_count() -> int:
    count = 0
    for path in (AIOS_PM / "tasks").glob("task-*.json"):
        try:
            task = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        engine = str(task.get("engine") or "")
        worker = str(task.get("worker") or "")
        if task.get("status") == "in_progress" and (
            engine == "claude-local" or worker.startswith("claude-local-")
        ):
            count += 1
    return count


def probe() -> dict:
    """The live probe, reused for ``PROBE_TTL_SECONDS``.

    One dashboard refresh asks for capacity once per lane plus the snapshot,
    and each probe costs about a second while llama.cpp is busy, so the page
    took several seconds and sat on its empty skeleton. Claims happen every
    20 seconds at most, so a five-second-old reading changes no decision.
    """
    with _CACHE_LOCK:
        if _CACHE["value"] is not None and time.monotonic() - _CACHE["at"] < PROBE_TTL_SECONDS:
            return dict(_CACHE["value"])
        value = _probe_now()
        _CACHE.update({"at": time.monotonic(), "value": value})
        return dict(value)


def _probe_now() -> dict:
    props = _json("/props")
    metrics = _metrics()
    total = props.get("total_slots")
    physical = metrics.get("requests_processing")
    task_count = _aios_task_count()
    if not isinstance(total, int) or total < 1:
        return {
            "reachable": False,
            "source": "aios_task_files+llama_metrics",
            "total_slots": None,
            "aios_used_slots": None,
            "available_slots": 0,
            "reason": "AIOS local LLM capacity could not be verified",
        }
    observed = [x for x in (physical, task_count) if isinstance(x, int)]
    used = max(observed, default=0)
    return {
        "reachable": bool(observed),
        "source": "max(aios_claude_local_tasks, llama_requests_processing)",
        "total_slots": total,
        "aios_task_slots": task_count,
        "llama_processing_slots": physical,
        "aios_used_slots": used,
        "available_slots": max(total - used, 0),
        "model_alias": props.get("model_alias"),
        "reason": "live read-only probe" if observed else "no live usage signal",
    }
