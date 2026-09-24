"""Local-LLM worker for the independent smart_store queue."""

from __future__ import annotations

import argparse
import re
import subprocess
import threading
import time
from pathlib import Path

from .agent_engine import diagnosis_note, run_agent
from .context import check_patch, file_tree, rewrite_violation
from .filepatch import SYSTEM, build_patch, looks_like_refusal, parse_files, parse_plan, plan_prompt, write_prompt
from contextlib import nullcontext

from .local_llm import LOCAL_LLM_GATE, complete
from .pm import EXTERNAL_ENGINES, POOLS_PATH, claim, finish, heartbeat_loop, lane_fault, lane_of, pause_lane, release, touch
from .state import CONTROL_DIR, read_json

PROJECT_ROOT = CONTROL_DIR.parents[1]


def extract_patch(text: str) -> str:
    match = re.search(r"```(?:diff|patch)?\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    return (match.group(1) if match else text).strip() + "\n"


def run_once(worker: str, apply_patch: bool = False) -> dict:
    lane = lane_of(worker)
    task = claim(worker, lane)
    if not task:
        return {"status": "idle", "reason": "no effective slot or ready task", "lane": lane}
    endpoint = read_json(CONTROL_DIR / "runtime.json", {}).get("local_llm", {}).get("endpoint", "http://127.0.0.1:8081")
    lane_pool = read_json(POOLS_PATH, {}).get("pools", {}).get(lane, {})
    # Feed back the concrete reason the previous attempt was rejected (apply error
    # or review verdict) so a retry is not a blind repeat.
    feedback = None
    if int(task.get("retry_count", 0)) > 0:
        feedback = task.get("last_error") or task.get("note")
        review = task.get("review_artifact")
        if review and Path(str(review)).is_file():
            feedback = (feedback or "") + "\n" + Path(str(review)).read_text(encoding="utf-8", errors="replace")[:1500]
    if diagnosis_note(task):
        feedback = diagnosis_note(task).strip() + "\n\n" + (feedback or "")
    try:
        stop = threading.Event()
        threading.Thread(target=heartbeat_loop, args=(stop, task["id"], worker, "llm_request"), daemon=True).start()
        touch(task["id"], worker, "llm_request")
        artifact = CONTROL_DIR / "artifacts" / f"task-{task['id']}.patch"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        llm = read_json(CONTROL_DIR / "runtime.json", {}).get("local_llm", {})
        engine = str(llm.get("engine", "claude-local"))
        model = str(llm.get("model", "qwen3.6-35b-a3b"))
        if str(lane_pool.get("engine", "")) in EXTERNAL_ENGINES:
            # cursor / gemini / claude lane: same worktree-and-diff flow, its
            # own CLI or account; the pool entry names the engine and model.
            engine, model = str(lane_pool["engine"]), str(lane_pool.get("model") or "")

        if engine in ("claude-local", *EXTERNAL_ENGINES):
            # AIOS lane: the CLI edits real files with tools in a throwaway
            # worktree; we keep only its diff. See agent_engine.py.
            for stale in (artifact, artifact.with_suffix(".agent.json")):
                stale.unlink(missing_ok=True)  # never let an older engine's output be mistaken for this run
            # A claude-local run is one long stream of proxy calls: hold the
            # process-wide gate for its whole duration so reviews and
            # diagnoses wait instead of adding a second stream to the model.
            gate = LOCAL_LLM_GATE if engine == "claude-local" else nullcontext()
            with gate:
                diff, summary = run_agent(PROJECT_ROOT, task, model=model, base_url=endpoint,
                                          max_turns=int(lane_pool.get("max_turns", llm.get("max_turns", 45))),
                                          wall_seconds=int(lane_pool.get("wall_seconds", llm.get("wall_seconds", 1500))),
                                          artifact_dir=artifact.parent, engine=engine)
            if summary.get("stray_edits"):
                stop.set()
                return finish(task["id"], "blocked", note="agent wrote outside its worktree (edits quarantined, checkout restored): "
                              + ", ".join(summary["stray_edits"])[:200])
            if summary.get("timeout"):
                # Throughput, not the task: the shared model could not finish 45
                # turns inside the wall clock. Rest the lane briefly so the same
                # 25 minutes are not burned again immediately; no retry is charged.
                until = pause_lane(lane, f"{engine}: wall clock {summary.get('wall_seconds')}s exceeded (congested)", seconds=10 * 60)
                stop.set()
                return finish(task["id"], "blocked", note=f"lane paused until {until}: {engine} wall clock exceeded, model congested")
            if engine in EXTERNAL_ENGINES and not diff.strip():
                fault = lane_fault(str(summary.get("result", "")) + " " + str(summary.get("stderr", "")))
                if fault:
                    # Provider quota, login or trust problem: the lane, not the task.
                    until = pause_lane(lane, f"{engine}: {fault}")
                    stop.set()
                    return release(task["id"], f"lane paused until {until}: {engine} {fault}; task returned to the queue")
            if not diff.strip():
                stop.set()
                reason = summary.get("result") or summary.get("stderr") or "agent produced no change"
                if summary.get("subtype") == "error_max_turns":
                    # stderr then only carries CLI start-up warnings; name the real outcome.
                    reason = f"hit max turns ({summary.get('num_turns', '?')}) without editing a file"
                if summary.get("denied_writes"):
                    reason = f"{len(summary['denied_writes'])} write(s) to the live checkout denied; " + str(reason)
                return finish(task["id"], "blocked", note="agent produced no change: " + str(reason)[:220])
            # Bytes, not text: Path.write_text would turn the diff's LF into
            # CRLF on Windows and git apply would then reject every hunk.
            artifact.write_bytes(diff.encode("utf-8"))
            rewrite = rewrite_violation(PROJECT_ROOT, artifact)
            if rewrite:
                stop.set()
                return finish(task["id"], "blocked", str(artifact), rewrite)
            apply_error = check_patch(PROJECT_ROOT, artifact)
            if apply_error:
                stop.set()
                return finish(task["id"], "blocked", str(artifact), f"patch does not apply: {apply_error}")
            stop.set()
            return finish(task["id"], "needs_review", str(artifact),
                          f"claude-local agent finished in {summary.get('num_turns', '?')} turns; apply only after review")

        # Two short calls: plan the files, then return whole files. git makes
        # the diff, so it always applies (see filepatch.py for why).
        tree = file_tree(PROJECT_ROOT)
        plan_text = complete(plan_prompt(task["prompt"], tree, feedback), endpoint=endpoint, model="qwen3.6-35b-a3b",
                             system=SYSTEM)
        # Raw model output is kept next to the patch so a format failure can be
        # diagnosed from evidence instead of guessed at.
        artifact.with_suffix(".plan.txt").write_text(plan_text, encoding="utf-8")
        modify, create = parse_plan(plan_text, set(tree))
        if not modify and not create:
            stop.set()
            return finish(task["id"], "blocked", note="plan named no existing file to modify and no new file to create")
        contents = {p: (PROJECT_ROOT / p).read_text(encoding="utf-8", errors="replace") for p in modify}
        touch(task["id"], worker, "llm_request")
        response = complete(write_prompt(task["prompt"], contents, feedback, create), endpoint=endpoint,
                            model="qwen3.6-35b-a3b", system=SYSTEM)
        artifact.with_suffix(".response.txt").write_text(response, encoding="utf-8")
        if looks_like_refusal(response):
            # Recorded as its own reason so an operator can tell a model that
            # declines the task apart from a format or path failure.
            stop.set()
            return finish(task["id"], "blocked", note="model refused the task: " + response.strip()[:200])
        build_error = build_patch(PROJECT_ROOT, parse_files(response), modify + create, artifact)
        if build_error:
            stop.set()
            return finish(task["id"], "blocked", note=f"patch not produced: {build_error}")
        apply_error = check_patch(PROJECT_ROOT, artifact)
        if apply_error:
            stop.set()
            return finish(task["id"], "blocked", str(artifact), f"patch does not apply: {apply_error}")

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
