"""Local LLM review worker for generated smart_store patches."""

from __future__ import annotations

import re
import threading

from .local_llm import complete
from .pm import claim_review, finish_review, heartbeat_loop, touch
from .state import CONTROL_DIR, read_json


def run_once(worker: str = "local-review-1") -> dict:
    task = claim_review(worker)
    if not task:
        return {"status": "idle", "reason": "no review task or effective slot"}
    source = task.get("artifact")
    try:
        patch_text = __import__("pathlib").Path(source).read_text(encoding="utf-8") if source else "artifact missing"
        prompt = (
            "You are the smart_store local review worker. AIOS is separate and read-only. "
            "Review the candidate patch below for correctness, safety, scope, and tests. "
            "Do not modify files or use Codex. End with exactly REVIEW: PASS or REVIEW: FAIL, "
            "followed by concise reasons.\n\nPATCH:\n" + patch_text
        )
        stop = threading.Event()
        threading.Thread(target=heartbeat_loop, args=(stop, task["id"], worker, "review_llm"), daemon=True).start()
        response = complete(prompt, endpoint=read_json(CONTROL_DIR / "runtime.json", {}).get("local_llm", {}).get("endpoint", "http://127.0.0.1:8081"), model="qwen3.6-35b-a3b")
        stop.set()
        review_dir = CONTROL_DIR / "artifacts" / "reviews"
        review_dir.mkdir(parents=True, exist_ok=True)
        review_path = review_dir / f"task-{task['id']}.md"
        review_path.write_text(response, encoding="utf-8")
        marker = re.search(r"REVIEW\s*:\s*(PASS|FAIL)", response, re.IGNORECASE)
        decision = marker.group(1).lower() if marker else "fail"
        return finish_review(task["id"], decision, str(review_path), "local LLM review completed")
    except Exception as exc:
        return finish_review(task["id"], "fail", note=f"review error: {type(exc).__name__}")
