from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from smart_store_aios.dev_dashboard import DevDashboardCollector, DevDashboardServer, INDEX_HTML


def record(path: Path, payload: dict, stamp: str = "2026-09-05T03:50:00+00:00", kind: str = "event_msg") -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"timestamp": stamp, "type": kind, "payload": payload}, ensure_ascii=False) + "\n")


class DevDashboardCollectorTest(unittest.TestCase):
    def test_progress_counts_only_completed_manifest_items(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            progress = root / "docs" / "implementation" / "development-progress.json"
            progress.parent.mkdir(parents=True)
            progress.write_text(json.dumps({"basis": "backlog", "items": [
                {"id": "B01", "title": "done", "status": "completed"},
                {"id": "B02", "title": "working", "status": "in_progress"},
                {"id": "B03", "title": "later", "status": "pending"}
            ]}), encoding="utf-8")
            result = DevDashboardCollector(root, root / "codex").collect()["progress"]
            self.assertEqual((3, 1, 1, 1, 33), (result["total"], result["completed"],
                                                   result["in_progress"], result["pending"], result["percent"]))

    def test_exact_cwd_merge_classifies_app_pm_and_redacts_messages(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sessions = root / "sessions" / "2026" / "09" / "05"
            sessions.mkdir(parents=True)
            path = sessions / "rollout-root.jsonl"
            record(path, {"session_id": "root-s", "id": "root-thread", "cwd": str(root), "timestamp": "2026-09-05T03:40:00+00:00", "originator": "Codex Desktop", "thread_source": "user"}, "2026-09-05T03:40:00+00:00", "session_meta")
            record(path, {"type": "task_started", "turn_id": "t"})
            record(path, {"type": "agent_message", "message": "진행 중 api_key=hidden user@example.com C:\\private\\x"})
            record(path, {"type": "item_completed", "item": {"type": "AgentMessage", "phase": "implement", "content": [{"type": "Text", "text": "안전한 구현 단계"}, {"type": "Reasoning", "raw_content": "do not expose"}]}}, kind="event_msg")
            record(path, {"thread_token_usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}}, kind="token_usage_record")
            path2 = sessions / "rollout-pm.jsonl"
            record(path2, {"session_id": "pm-s", "id": "pm-thread", "cwd": str(root), "timestamp": "2026-09-05T03:45:00+00:00", "originator": "Codex Desktop", "source": {"subagent": {"thread_spawn": {"agent_path": "/root/codex_pm_luna", "parent_thread_id": "root-thread"}}}}, "2026-09-05T03:45:00+00:00", "session_meta")
            record(path2, {"type": "task_complete", "completed_at": 1788579600})
            wrong = sessions / "rollout-other.jsonl"
            record(wrong, {"session_id": "other", "id": "other", "cwd": str(root / "other"), "timestamp": "2026-09-05T03:49:00+00:00"}, "2026-09-05T03:49:00+00:00", "session_meta")
            data = DevDashboardCollector(root, root, now=datetime(2026, 9, 5, 3, 50, 10, tzinfo=timezone.utc)).collect()
            self.assertEqual(2, len(data["agents"]))
            self.assertEqual({"codex_app_root", "app_subagent_pm"}, {x["kind"] for x in data["agents"]})
            root_session = next(x for x in data["agents"] if x["kind"] == "codex_app_root")
            self.assertEqual("running", root_session["state"])
            self.assertEqual(2, len(root_session["recent_messages"]))
            self.assertEqual("안전한 구현 단계", root_session["recent_messages"][-1])
            serialized = json.dumps(data, ensure_ascii=False)
            self.assertNotIn("do not expose", serialized)
            self.assertNotIn("api_key=hidden", serialized)
            self.assertNotIn("hidden", root_session["recent_messages"][0])
            self.assertNotIn("user@example.com", root_session["recent_messages"][0])
            self.assertNotIn("C:\\private", root_session["recent_messages"][0])
            self.assertEqual("안전한 구현 단계", root_session["current_task"])
            self.assertEqual(14, root_session["token_usage"]["total"])
            self.assertEqual("execution_not_observed", data["cli_worker"]["state"])

    def test_stale_and_usage_limited_states_are_evidence_based(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); sessions = root / "sessions" / "2026"; sessions.mkdir(parents=True)
            stale = sessions / "stale.jsonl"
            record(stale, {"session_id": "s", "id": "s", "cwd": str(root), "timestamp": "2026-09-05T03:00:00+00:00", "originator": "Codex Desktop"}, "2026-09-05T03:00:00+00:00", "session_meta")
            record(stale, {"type": "task_started", "turn_id": "t"}, "2026-09-05T03:00:01+00:00")
            limited = sessions / "limited.jsonl"
            record(limited, {"session_id": "l", "id": "l", "cwd": str(root), "timestamp": "2026-09-05T03:49:00+00:00", "originator": "Codex CLI"}, "2026-09-05T03:49:00+00:00", "session_meta")
            record(limited, {"type": "task_complete", "error": {"message": "rate limit reached"}}, "2026-09-05T03:49:01+00:00")
            data = DevDashboardCollector(root, root, now=datetime(2026, 9, 5, 4, 0, tzinfo=timezone.utc)).collect()
            self.assertEqual({"signal_lost", "usage_limited"}, {x["state"] for x in data["agents"]})
            self.assertEqual("historical_only", data["cli_worker"]["state"])

    def test_read_only_api_and_static_polling_contract(self):
        self.assertIn("setInterval(load,10000)", INDEX_HTML)
        self.assertIn("document.hidden", INDEX_HTML)
        self.assertNotIn("tenant_id", INDEX_HTML)
        with tempfile.TemporaryDirectory() as temp:
            server = DevDashboardServer(("127.0.0.1", 0), temp, temp)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                conn.request("GET", "/api/dev-dashboard"); response = conn.getresponse(); body = json.loads(response.read())
                self.assertEqual(200, response.status); self.assertEqual("execution_not_observed", body["cli_worker"]["state"])
                conn.request("POST", "/api/dev-dashboard"); self.assertEqual(405, conn.getresponse().status)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
