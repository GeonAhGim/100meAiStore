"""Local-LLM worker for the independent smart_store queue."""

from __future__ import annotations

import argparse
import re
import subprocess
import threading
import time
from pathlib import Path

from .local_llm import complete
from .pm import finish, claim, touch
from .state import CONTROL_DIR, read_json


def extract_patch(text: str) -> str:
    match = re.search(r"```(?:diff|patch)?\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    return (match.group(1) if match else text).strip() + "\n"


def run_once(worker: str, apply_patch: bool = False) -> dict:
    task = claim(worker)
    if not task:
        return {"status": "idle", "reason": "no effective slot or ready task"}
    endpoint = read_json(CONTROL_DIR / "runtime.json", {}).get("local_llm", {}).get("endpoint", "http://127.0.0.1:8081")
    prompt = (
        "You are the smart_store local implementation worker. Work only in the smart_store project. "
        "Do not use Codex, do not modify any other project, preserve dry_run and safety gates. "
        + task["prompt"]
    )
    try:
        stop = threading.Event()
        def heartbeat() -> None:
            while not stop.wait(15):
                touch(task["id"], worker, "llm_request")
        threading.Thread(target=heartbeat, daemon=True).start()
        touch(task["id"], worker, "llm_request")
        response = complete(prompt, endpoint=endpoint, model="qwen3.6-35b-a3b")
        artifact = CONTROL_DIR / "artifacts" / f"task-{task['id']}.patch"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(extract_patch(response), encoding="utf-8")
        
        status_name = "needs_review"
        note = "local LLM patch generated; apply only after review"
        
        if apply_patch:
            try:
                # git apply works best when executed from the project root
                # CONTROL_DIR is in c:/smart_store/data/control
                # ROOT is c:/smart_store
                project_root = CONTROL_DIR.parents[1] 
                result = subprocess.run(
                    ["git", "apply", str(artifact.absolute())],
                    cwd=str(project_root),
                    capture_output=True,
                    text=True,
                    check=True
                )
                status_name = "completed"
                note = f"local LLM patch applied automatically: {result.stdout.strip()}"
            except subprocess.CalledProcessError as e:
                status_name = "needs_review"
                note = f"failed to auto-apply patch: {e.stderr.strip()}"
            except Exception as e:
                status_name = "needs_review"
                note = f"error during auto-apply: {str(e)}"
        
        stop.set()
        return finish(task["id"], status_name, str(artifact), note)
    except Exception as exc:  # worker state must not remain stuck
        if "stop" in locals():
            stop.set()
        return finish(task["id"], "blocked", note=f"local LLM error: {type(exc).__name__}")


def main() -> None:
    parser = argparse.ArgumentParser(description="run one smart_store local LLM task")
    parser.add_argument("--once", action="store_true", help="run one claim cycle (default)")
    parser.add_argument("--worker", default="local-impl-1")
    parser.add_argument("--apply-patch", action="store_true", help="record explicit apply intent; review remains required")
    args = parser.parse_args()
    print(run_once(args.worker, args.apply_patch))


if __name__ == "__main__":
    main()
