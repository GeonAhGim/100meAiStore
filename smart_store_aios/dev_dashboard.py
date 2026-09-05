"""Zero-token local Codex CLI development-worker observability.

This module reads Codex session JSONL and git state.  It does not read the
commerce database, create heartbeats, invoke an LLM, or start a worker.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_CODEX_HOME = Path.home() / ".codex"
STALE_AFTER_SECONDS = 30
PROJECT = r"C:\smart_store"
_EMAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_SECRET = re.compile(r"(?i)(api[_-]?key|authorization|bearer|password|secret|token)(\s*[:=]\s*)[^\s,;]+")
_WINDOWS_PATH = re.compile(r"(?i)(?:[A-Za-z]:\\|\\\\)[^\s\"']+")


def _redact(value: Any, limit: int = 220) -> str:
    text = str(value or "")
    text = _SECRET.sub(r"\1\2[REDACTED]", text)
    text = _EMAIL.sub("[EMAIL]", text)
    text = _WINDOWS_PATH.sub("[PATH]", text)
    return text[:limit]


def _safe_message(content: Any) -> str:
    """Extract visible assistant text; never stringify reasoning/raw blocks."""
    if isinstance(content, str):
        return _redact(content)
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and str(block.get("type", "")).lower() in {"text", "output_text"}:
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return _redact(" ".join(parts))
    return ""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _timestamp(value: Any) -> datetime | None:
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000 if value > 10_000_000_000 else value, timezone.utc)
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError, OverflowError):
        return None
    return None


def _safe_kind(meta: dict[str, Any]) -> tuple[str, str]:
    source = meta.get("source", "")
    originator = str(meta.get("originator", "")).lower()
    source_text = json.dumps(source, ensure_ascii=False).lower()
    if "cli" in originator or source_text == '"cli"' or "codex exec" in source_text:
        return "codex_cli_worker", "cli worker"
    source_meta = source.get("subagent", {}) if isinstance(source, dict) else {}
    spawn = source_meta.get("thread_spawn", {}) if isinstance(source_meta, dict) else {}
    agent_path = str(spawn.get("agent_path", meta.get("agent_path", "")))
    leaf = agent_path.rsplit("/", 1)[-1].lower()
    if leaf.startswith("codex_pm") or leaf == "pm":
        return "app_subagent_pm", "PM subagent"
    if agent_path:
        return "app_subagent_worker", "app subagent"
    if str(meta.get("thread_source", "")) == "user":
        return "codex_app_root", "root app"
    return "codex_app", "Codex app"


class DevDashboardCollector:
    """Collect facts from exact-project Codex sessions without side effects."""

    def __init__(self, project_root: str | Path = PROJECT, codex_home: str | Path | None = None,
                 *, now: datetime | None = None) -> None:
        self.project_root = Path(project_root).resolve()
        self.codex_home = Path(codex_home).expanduser() if codex_home else DEFAULT_CODEX_HOME
        self.now = now or datetime.now(timezone.utc)
        self._session_cache_key: tuple[tuple[str, int, int], ...] | None = None
        self._session_cache: list[dict[str, Any]] | None = None

    def collect(self) -> dict[str, Any]:
        sessions = self._sessions()
        sessions.sort(key=lambda item: (item["last_event_at"] or "", item["session_id"]))
        cli = [s for s in sessions if s["kind"] == "codex_cli_worker"]
        active_cli = [s for s in cli if s["state"] == "running"]
        if active_cli:
            cli_status = {"state": "observed", "count": len(active_cli)}
        elif cli:
            cli_status = {"state": "historical_only", "count": len(cli), "evidence": "CLI sessions are terminal; no current worker observed"}
        else:
            cli_status = {"state": "execution_not_observed", "evidence": "no exact-cwd Codex CLI session"}
        return {
            "generated_at": self.now.isoformat(),
            "source": {"codex_sessions": str(self.codex_home / "sessions"), "cwd_exact_match": str(self.project_root),
                        "llm_calls": 0, "raw_prompts_exposed": False, "tool_arguments_exposed": False},
            "agents": sessions,
            "cli_worker": cli_status,
            "repository": self._repository_state(),
            "progress": self._development_progress(),
            "status_contract": {"running": "active recent session", "signal_lost": f"non-terminal session older than {STALE_AFTER_SECONDS}s",
                                "execution_not_observed": "no matching CLI session", "usage_limited": "session terminal error/rate limit evidence"},
        }

    def _development_progress(self) -> dict[str, Any]:
        """Read explicit evidence-backed work state; do not infer it from activity."""
        path = self.project_root / "docs" / "implementation" / "development-progress.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            items = value.get("items", [])
            if not isinstance(items, list):
                raise ValueError("items must be a list")
        except (OSError, UnicodeError, ValueError, TypeError):
            return {"available": False, "total": None, "completed": None, "in_progress": None,
                    "pending": None, "blocked": None, "percent": None, "items": []}
        allowed = {"completed", "in_progress", "pending", "blocked"}
        safe_items = [{"id": _redact(x.get("id"), 20), "title": _redact(x.get("title"), 120),
                       "status": x["status"], "evidence": _redact(x.get("evidence"), 180) or None}
                      for x in items if isinstance(x, dict) and x.get("status") in allowed]
        total = len(safe_items)
        completed = sum(x["status"] == "completed" for x in safe_items)
        return {"available": bool(total), "basis": _redact(value.get("basis"), 180),
                "updated_at": value.get("updated_at"), "total": total, "completed": completed,
                "in_progress": sum(x["status"] == "in_progress" for x in safe_items),
                "pending": sum(x["status"] == "pending" for x in safe_items),
                "blocked": sum(x["status"] == "blocked" for x in safe_items),
                "percent": round(completed * 100 / total) if total else None, "items": safe_items}

    def _session_files(self) -> list[Path]:
        sessions_root = self.codex_home / "sessions"
        if not sessions_root.exists():
            return []
        return sorted(sessions_root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)

    def _sessions(self) -> list[dict[str, Any]]:
        files = self._session_files()
        signature = tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in files if path.exists())
        if signature == self._session_cache_key and self._session_cache is not None:
            return copy.deepcopy(self._session_cache)
        grouped: dict[str, dict[str, Any]] = {}
        records_by_session: dict[str, list[tuple[datetime, str, dict[str, Any]]]] = {}
        for path in files:
            meta = self._read_meta(path)
            if not meta or str(meta.get("cwd", "")) != str(self.project_root):
                continue
            records: list[tuple[datetime, str, dict[str, Any]]] = []
            try:
                with path.open(encoding="utf-8") as stream:
                    for raw in stream:
                        try:
                            record = json.loads(raw)
                        except (TypeError, ValueError):
                            continue
                        payload = record.get("payload") or {}
                        stamp = _timestamp(record.get("timestamp"))
                        if stamp and isinstance(payload, dict):
                            records.append((stamp, str(payload.get("type") or record.get("type", "event")), payload))
            except (OSError, UnicodeError):
                continue
            session_id = str(meta.get("id") or meta.get("session_id") or path.name)
            grouped.setdefault(session_id, self._new_session(meta, session_id))
            records_by_session.setdefault(session_id, []).extend(records)
        result = []
        for session_id, item in grouped.items():
            unique: dict[str, tuple[datetime, str, dict[str, Any]]] = {}
            for record in records_by_session.get(session_id, []):
                stamp, kind, payload = record
                key = f"{stamp.isoformat()}|{kind}|{json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)}"
                unique[key] = record
            self._merge_records(item, list(unique.values()))
            result.append(item)
        self._session_cache_key, self._session_cache = signature, copy.deepcopy(result)
        return result

    @staticmethod
    def _read_meta(path: Path) -> dict[str, Any] | None:
        """Read only the leading session metadata before scanning event lines."""
        try:
            with path.open(encoding="utf-8") as stream:
                for _ in range(8):
                    raw = stream.readline()
                    if not raw:
                        break
                    try:
                        record = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if record.get("type") == "session_meta" and isinstance(record.get("payload"), dict):
                        return record["payload"]
        except (OSError, UnicodeError):
            return None
        return None

    @staticmethod
    def _new_session(meta: dict[str, Any], session_id: str) -> dict[str, Any]:
        kind, label = _safe_kind(meta)
        source = meta.get("source", {})
        spawn = source.get("subagent", {}).get("thread_spawn", {}) if isinstance(source, dict) else {}
        return {"session_id": session_id, "thread_id": session_id, "kind": kind, "label": label,
                "model": meta.get("model") or None, "cwd": str(meta.get("cwd")), "started_at": meta.get("timestamp"),
                "last_event_at": None, "ended_at": None, "state": "waiting", "current_stage": "not_observed",
                "current_task": "not_observed", "recent_events": [], "recent_messages": [], "changed_files": [],
                "commits": [], "tests": {"last_result": "not_observed"}, "blocker": None,
                "next_task": "not_observed", "parent_thread_id": spawn.get("parent_thread_id"),
                "agent_path": _redact(spawn.get("agent_path")), "token_usage": {"input": None, "output": None, "total": None,
                "rate_limit_percent": None, "measured": False}}

    def _merge_records(self, item: dict[str, Any], records: list[tuple[datetime, str, dict[str, Any]]]) -> None:
        # Stable ordering keeps the JSONL sequence when timestamps coincide.
        # Sorting by event name can replay task_started after its messages.
        for stamp, kind, payload in sorted(records, key=lambda record: record[0]):
            item["last_event_at"] = _iso(stamp)
            event = {"at": stamp.isoformat(), "type": kind}
            if kind == "task_started":
                item["state"], item["current_stage"] = "running", "task_started"
                item["started_at"] = item["started_at"] or stamp.isoformat()
                item["ended_at"], item["current_task"], item["blocker"] = None, "Codex 작업 실행", None
            elif kind == "task_complete":
                error = payload.get("error")
                if error:
                    text = _redact(error)
                    usage = any(word in text.lower() for word in ("rate limit", "usage limit", "quota", "limit reached"))
                    item["state"] = "usage_limited" if usage else "failed"
                    item["blocker"] = "usage_limit" if usage else "terminal_error"
                    event["summary"] = "사용량 제한" if usage else "실패"
                else:
                    item["state"], event["summary"] = "completed", "완료"
                item["ended_at"] = _iso(_timestamp(payload.get("completed_at")) or stamp)
                item["current_stage"] = "terminal"
            elif kind == "token_count":
                usage = payload.get("last_token_usage") or (payload.get("info") or {}).get("total_token_usage") or {}
                item["token_usage"]["input"] = int(usage.get("input_tokens", 0)) if str(usage.get("input_tokens", "")).isdigit() else None
                item["token_usage"]["output"] = int(usage.get("output_tokens", 0)) if str(usage.get("output_tokens", "")).isdigit() else None
                item["token_usage"]["total"] = int(usage.get("total_tokens", 0)) if str(usage.get("total_tokens", "")).isdigit() else None
                item["token_usage"]["measured"] = True
                limits = payload.get("rate_limits") or {}
                try: item["token_usage"]["rate_limit_percent"] = limits.get("primary", {}).get("used_percent")
                except AttributeError: pass
                event["summary"] = "토큰 사용량 기록"
            elif kind == "agent_message":
                message = _safe_message(payload.get("message"))
                if message:
                    item["recent_messages"].append(message)
                    item["current_task"], item["current_stage"] = message, "agent update"
                    event["summary"] = message
            elif kind == "item_completed":
                event_item = payload.get("item") or {}
                item_type = str(event_item.get("type", ""))
                if item_type == "AgentMessage":
                    message = _safe_message(event_item.get("content"))
                    if message:
                        item["recent_messages"].append(message)
                        item["current_task"], item["current_stage"] = message, str(event_item.get("phase") or "agent update")
                        event["summary"] = message
                    else:
                        event["summary"] = "assistant 작업 메시지 관측"
                else:
                    event["summary"] = "명령 실행 완료" if item_type == "CommandExecution" else "작업 이벤트 완료"
                self._test_from_item(item, event_item)
            elif kind == "thread_settings_applied":
                settings = payload.get("thread_settings") or {}
                item["model"] = settings.get("model") or item["model"]
                event["summary"] = "모델 설정 확인"
            elif kind == "turn_context":
                item["model"] = payload.get("model") or item["model"]
                event["summary"] = "실행 컨텍스트 확인"
            elif kind == "token_usage_record":
                usage = payload.get("thread_token_usage") or payload.get("usage") or {}
                if usage:
                    item["token_usage"]["input"] = usage.get("input_tokens")
                    item["token_usage"]["output"] = usage.get("output_tokens")
                    item["token_usage"]["total"] = usage.get("total_tokens")
                    item["token_usage"]["measured"] = True
                event["summary"] = "토큰 사용량 기록"
            if "summary" not in event:
                event["summary"] = kind
            item["recent_events"].append(event)
        item["recent_events"] = item["recent_events"][-10:]
        item["recent_messages"] = item["recent_messages"][-5:]
        if item["state"] not in {"completed", "failed", "usage_limited"}:
            last = _timestamp(item["last_event_at"])
            if last and (self.now - last).total_seconds() > STALE_AFTER_SECONDS:
                item["state"] = "signal_lost"
            elif last:
                item["state"] = "running"

    @staticmethod
    def _test_from_item(item: dict[str, Any], event_item: dict[str, Any]) -> None:
        command = " ".join(str(x) for x in (event_item.get("command") or []))
        if "pytest" not in command.lower() and "unittest" not in command.lower():
            return
        output = _redact(event_item.get("stdout") or event_item.get("aggregated_output") or "")
        passed = re.search(r"(\d+)\s+passed", output)
        failed = re.search(r"(\d+)\s+failed", output)
        item["tests"] = {"last_result": "failed" if failed else "passed" if passed else "observed",
                          "passed": int(passed.group(1)) if passed else None,
                          "failed": int(failed.group(1)) if failed else None}

    def _repository_state(self) -> dict[str, Any]:
        def run(args: list[str]) -> str:
            try:
                return subprocess.run(["git", *args], cwd=self.project_root, capture_output=True, text=True,
                                      timeout=2, check=True).stdout
            except (OSError, subprocess.SubprocessError):
                return ""
        changed = [line[3:].strip() for line in run(["status", "--short", "--untracked-files=all"]).splitlines() if len(line) >= 4]
        commits = []
        for line in run(["log", "-5", "--format=%h%x09%s"]).splitlines():
            ident, _, subject = line.partition("\t")
            if ident: commits.append({"id": ident[:12], "subject": _redact(subject)})
        return {"changed_files": changed[:100], "recent_commits": commits, "git_observed": bool(commits or changed)}


INDEX_HTML = Path(__file__).with_name("dev_dashboard.html").read_text(encoding="utf-8")


class DevDashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, "text/html; charset=utf-8", INDEX_HTML.encode())
        elif parsed.path == "/api/dev-dashboard":
            collector = self.server.collector_factory()  # type: ignore[attr-defined]
            self._send_json(200, collector.collect())
        else:
            self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        self._send_json(405, {"error": "development dashboard is read-only"})

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json(self, status: int, value: dict[str, Any]) -> None:
        self._send(status, "application/json; charset=utf-8", json.dumps(value, ensure_ascii=False).encode())

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)


class DevDashboardServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], project_root: str | Path, codex_home: str | Path | None = None) -> None:
        super().__init__(address, DevDashboardHandler)
        self.collector_factory = lambda: DevDashboardCollector(project_root, codex_home)


def main() -> None:
    parser = argparse.ArgumentParser(description="serve the local Codex development-worker dashboard")
    parser.add_argument("--project-root", default=PROJECT); parser.add_argument("--codex-home", default=None)
    parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args(); server = DevDashboardServer((args.host, args.port), args.project_root, args.codex_home)
    print(f"dev dashboard listening on http://{args.host}:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
