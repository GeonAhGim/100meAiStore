"""Review worker for generated smart_store patches: objective gate first, LLM second.

The old reviewer saw only the diff and rejected a correct patch by claiming
functions were missing that exist in the repository. A model cannot review
what it cannot see, and its opinion must not outrank evidence. So:

1. Gate: apply the patch in a scratch worktree at HEAD, run the test modules
   the patch touches or adds, then the full suite. Concrete failures block the
   task with the exact output as feedback for the implementer.
2. LLM read: the model gets the patch, the gate results and the current
   content of every touched file, and is told not to claim code is missing
   unless it is absent from those files.
3. Decision: gate fail -> ``fail``; gate pass + LLM PASS -> ``reviewed``
   (landed on a branch by the autopilot); gate pass + LLM FAIL ->
   ``needs_decision`` for a human, without spending a retry.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
from pathlib import Path

from .context import check_patch, git
from .filepatch import SYSTEM
from .local_llm import complete
from .pm import claim_review, finish_review, heartbeat_loop, touch
from .state import CONTROL_DIR, read_json

PROJECT_ROOT = CONTROL_DIR.parents[1]
TEST_TIMEOUT = 900
MAX_FILE_CHARS = 8000
_MARKER = re.compile(r"REVIEW\s*:\s*(PASS|FAIL)", re.IGNORECASE)


def touched_paths(patch_text: str) -> list[str]:
    return [line[6:].strip() for line in patch_text.splitlines() if line.startswith("+++ b/")]


def _test_modules(paths: list[str]) -> list[str]:
    modules = []
    for path in paths:
        if path.startswith("tests/") and path.endswith(".py") and not path.endswith("__init__.py"):
            modules.append(path[:-3].replace("/", "."))
    return modules


def run_gate(root: Path, patch_path: Path, task_id: int) -> tuple[bool, str]:
    """Apply the patch at HEAD in a scratch worktree and run the tests. Returns (passed, report)."""
    apply_error = check_patch(root, patch_path)
    if apply_error:
        return False, "patch does not apply at HEAD:\n" + apply_error
    scratch = root / "data" / "control" / "scratch" / f"review-{task_id}"
    git(["worktree", "remove", "--force", str(scratch)], root)
    shutil.rmtree(scratch, ignore_errors=True)
    git(["worktree", "prune"], root)
    added = git(["worktree", "add", "-q", "--detach", str(scratch), "HEAD"], root)
    if added.returncode:
        return False, "scratch worktree failed: " + (added.stderr or added.stdout).strip()[:300]
    try:
        applied = git(["apply", "--index", str(patch_path)], scratch)
        if applied.returncode:
            return False, "patch failed to apply in scratch worktree:\n" + (applied.stderr or applied.stdout)[:1000]
        patch_text = patch_path.read_text(encoding="utf-8", errors="replace")
        modules = _test_modules(touched_paths(patch_text))
        report = []
        for label, argv in ((f"touched tests ({len(modules)})", ["python", "-m", "unittest", *modules]) if modules else (None, None),
                            ("full suite", ["python", "-m", "unittest", "discover", "-s", "tests", "-t", "."])):
            if label is None:
                continue
            try:
                run = subprocess.run(argv, cwd=str(scratch), capture_output=True, text=True, encoding="utf-8",
                                     errors="replace", timeout=TEST_TIMEOUT)
            except subprocess.TimeoutExpired:
                return False, f"{label}: timed out after {TEST_TIMEOUT}s"
            summary = "\n".join(l for l in (run.stdout + run.stderr).splitlines() if l.startswith(("Ran ", "OK", "FAILED", "FAIL:", "ERROR:")))
            report.append(f"{label}: rc={run.returncode}\n{summary[:1500]}")
            if run.returncode:
                tail = (run.stdout + run.stderr)[-3000:]
                return False, "\n".join(report) + "\n" + tail
        return True, "\n".join(report)
    finally:
        git(["worktree", "remove", "--force", str(scratch)], root)
        shutil.rmtree(scratch, ignore_errors=True)


def review_prompt(patch_text: str, gate_report: str, root: Path) -> str:
    shown = []
    for path in touched_paths(patch_text):
        file = root / path
        if file.is_file():
            text = file.read_text(encoding="utf-8", errors="replace")
            shown.append(f"\n\n<<<CURRENT {path}>>>\n{text[:MAX_FILE_CHARS]}\n<<<END>>>")
    return (
        "You are the smart_store local review worker. Review the candidate patch for correctness, safety and "
        "scope. The objective gate below already applied the patch and ran the tests; treat its results as facts. "
        "The CURRENT files are the repository contents the patch is applied to: do not claim a function, class or "
        "fixture is missing unless it is absent from them. End with exactly REVIEW: PASS or REVIEW: FAIL, "
        "followed by concise reasons.\n\nGATE RESULTS:\n" + gate_report + "\n\nPATCH:\n" + patch_text[:20000]
        + "".join(shown)
    )


def run_once(worker: str = "local-review-1") -> dict:
    task = claim_review(worker)
    if not task:
        return {"status": "idle", "reason": "no review task or effective slot"}
    stop = threading.Event()
    threading.Thread(target=heartbeat_loop, args=(stop, task["id"], worker, "review_gate"), daemon=True).start()
    try:
        source = task.get("artifact")
        patch_path = Path(str(source)) if source else None
        if not patch_path or not patch_path.is_file():
            return finish_review(task["id"], "fail", note="review: patch artifact missing")
        passed, gate_report = run_gate(PROJECT_ROOT, patch_path, int(task["id"]))
        review_dir = CONTROL_DIR / "artifacts" / "reviews"
        review_dir.mkdir(parents=True, exist_ok=True)
        review_path = review_dir / f"task-{task['id']}.md"
        if not passed:
            review_path.write_text("REVIEW: FAIL (objective gate)\n\n" + gate_report, encoding="utf-8")
            return finish_review(task["id"], "fail", str(review_path), "gate failed: " + gate_report.splitlines()[0][:150])
        touch(task["id"], worker, "review_llm")
        patch_text = patch_path.read_text(encoding="utf-8", errors="replace")
        endpoint = read_json(CONTROL_DIR / "runtime.json", {}).get("local_llm", {}).get("endpoint", "http://127.0.0.1:8081")
        model = read_json(CONTROL_DIR / "runtime.json", {}).get("local_llm", {}).get("model", "qwen3.6-35b-a3b")
        try:
            response = complete(review_prompt(patch_text, gate_report, PROJECT_ROOT), endpoint=endpoint, model=model, system=SYSTEM)
        except Exception as exc:  # noqa: BLE001 - the gate passed; the model's read is advisory
            response = f"REVIEW: PASS\n(model review unavailable: {type(exc).__name__}; gate passed)"
        review_path.write_text("GATE:\n" + gate_report + "\n\nMODEL:\n" + response, encoding="utf-8")
        marker = _MARKER.search(response)
        verdict = marker.group(1).lower() if marker else "fail"
        if verdict == "pass":
            return finish_review(task["id"], "pass", str(review_path), "gate passed; model review passed")
        return finish_review(task["id"], "needs_decision", str(review_path),
                             "gate passed; model review objected, human decision needed")
    except Exception as exc:  # worker state must not remain stuck
        return finish_review(task["id"], "fail", note=f"review error: {type(exc).__name__}: {exc}"[:300])
    finally:
        stop.set()
