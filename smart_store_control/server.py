"""Local control dashboard: what is happening, what needs me, and one-click decisions.

Read-only observation of the ledger plus explicit operator actions. No LLM
call is made by any endpoint. The page is ``dashboard.html`` next to this
module; the API is:

GET  /api/control          full snapshot (runtime, capacity, tasks, milestones, autopilot, recovery)
GET  /api/task/<id>        one task with its artifacts (patch head, review, agent summary)
POST /api/task/<id>/<act>  act in retry|approve|reject|rereview|park, JSON body {"reason": "..."} optional
POST /api/handoff/grant|revoke, /api/pm/run, /api/pm/recover   (unchanged)
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .autopilot import start as start_autopilot, status as autopilot_status
from .pm import add_instruction, create_operator_task, operator_action, requeue_stale, status as pm_status
from .pm_cycle import current as pm_cycle_status, start as start_pm_cycle
from .recovery import current as recovery_status, start as start_recovery
from .state import CONTROL_DIR, grant_handoff, revoke_handoff, snapshot
from .triage import run_triage, status as triage_status

HERE = Path(__file__).resolve().parent
PAGE = HERE / "dashboard.html"
ARTIFACT_LIMIT = 12000


def _read(path: str | None, limit: int = ARTIFACT_LIMIT) -> str | None:
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return None


def milestone_progress(view: dict) -> dict:
    items = (view.get("milestones") or {}).get("milestones") or []
    done = sum(1 for m in items if m.get("status") == "done")
    parents: dict[str, dict] = {}
    for m in items:
        key = str(m.get("parent") or m.get("id"))
        row = parents.setdefault(key, {"id": key, "total": 0, "done": 0})
        row["total"] += 1
        row["done"] += 1 if m.get("status") == "done" else 0
    # Build a lookup for dependency resolution
    by_id = {str(m["id"]): m for m in items}

    def _dep_labels(m: dict) -> list[str]:
        deps = m.get("depends_on") or []
        return [str(d) for d in deps if d in by_id and by_id[d].get("status") == "done"]

    enriched = []
    for m in items:
        enriched.append({
            "id": m.get("id", ""),
            "title": m.get("title", ""),
            "status": m.get("status", "planned"),
            "priority": m.get("priority", 50),
            "depends_on": m.get("depends_on") or [],
            "satisfied_deps": _dep_labels(m),
            "exit_criteria": m.get("exit_criteria", ""),
        })
    # Sort by priority (highest first), then by id for stability
    enriched.sort(key=lambda x: (-x["priority"], x["id"]))
    return {
        "total": len(items),
        "done": done,
        "percent": round(done * 100 / len(items)) if items else None,
        "groups": sorted(parents.values(), key=lambda r: r["id"]),
        "items": enriched,
    }


def task_progress(tasks: list[dict]) -> dict:
    total = len(tasks)
    done = sum(1 for t in tasks if t.get("status") == "done")
    return {"total": total, "done": done, "percent": round(done * 100 / total) if total else None}


def control_view() -> dict:
    view = snapshot()
    view["pm"] = pm_status()
    view["pm_cycle"] = pm_cycle_status()
    view["pm_recovery"] = recovery_status()
    view["autopilot"] = autopilot_status()
    view["triage"] = triage_status()
    view["progress"] = {"milestones": milestone_progress(view), "tasks": task_progress(view["pm"].get("tasks", []))}
    view["server_time"] = datetime.now(timezone.utc).isoformat()
    return view


def task_detail(task_id: str) -> dict | None:
    for task in pm_status().get("tasks", []):
        if str(task.get("id")) == str(task_id):
            artifact = task.get("artifact")
            base = Path(str(artifact)) if artifact else None
            return {
                "task": task,
                "patch": _read(artifact),
                "review": _read(task.get("review_artifact")),
                "agent": _read(str(base.with_suffix(".agent.json")) if base else None, 4000),
                "plan": _read(str(base.with_suffix(".plan.txt")) if base else None, 2000),
            }
    return None


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: object) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/":
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/control":
            self._json(200, control_view())
        elif path.startswith("/api/task/"):
            detail = task_detail(path.split("/")[3])
            self._json(200, detail) if detail else self._json(404, {"error": "unknown task"})
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except json.JSONDecodeError:
            payload = {}
        parts = path.split("/")
        try:
            if path == "/api/handoff/grant":
                result = grant_handoff()
            elif path == "/api/handoff/revoke":
                result = revoke_handoff()
            elif path == "/api/pm/run":
                result = start_pm_cycle()
            elif path == "/api/pm/recover":
                result = start_recovery()
            elif path == "/api/triage":
                result = run_triage(force=True)
            elif path == "/api/task/new":
                # Operator-dictated task: enters the same queue, gate and review as any other.
                task = create_operator_task(str(payload.get("title") or ""), str(payload.get("instruction") or ""),
                                            priority=int(payload.get("priority") or 100),
                                            files=[f for f in str(payload.get("files") or "").replace("\n", ",").split(",") if f.strip()],
                                            lane=str(payload.get("lane") or "") or None)
                result = {"task": task, "recovery": start_recovery() if payload.get("run_now", True) else None}
            elif len(parts) == 5 and parts[1:3] == ["api", "task"] and parts[4] == "instruct":
                result = add_instruction(parts[3], str(payload.get("text") or ""))
            elif len(parts) == 5 and parts[1:3] == ["api", "task"]:
                result = operator_action(parts[3], parts[4], str(payload.get("reason") or "")[:200])
            else:
                self.send_error(404)
                return
        except (ValueError, KeyError) as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(200, result)

    def log_message(self, *_args) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="smart_store local control dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8877)
    args = parser.parse_args()
    # Startup self-check: tasks held by the previous process can never
    # heartbeat again. Release them before any worker or autopilot runs.
    orphans = requeue_stale(stale_after_seconds=0, note="requeued at startup: holder process is gone")
    if orphans:
        logging.getLogger(__name__).warning("released %d orphaned task(s) at startup: %s", len(orphans), orphans)
    start_autopilot()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
