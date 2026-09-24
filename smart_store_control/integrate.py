"""Integrate landed task branches into main, then mark their milestone done.

Landing (land.py) commits a reviewed patch on ``control/task-<id>`` and stops
there, so finished work piled up outside main: every later task started from
a HEAD without it, and milestones never completed, which left dependent
milestones unschedulable and the pool idle with nothing to claim.

The autopilot now integrates one landed task per tick:

1. merge the branch into the current main in a scratch worktree;
2. run the full suite on the merge result;
3. fast-forward main to it, only if main has not moved meanwhile and the live
   checkout accepts the fast-forward (local edits to the same files refuse it);
4. mark the task ``merged`` and, for a milestone-generated task, the milestone
   ``done`` with the merge commit as evidence.

A merge conflict or a red suite sends the task back to the queue with the
reason as feedback (``rebase_needed``), recorded as an attempt so the loop
guard bounds it. Nothing is pushed.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import loopguard
from .context import git
from .pm import _CLAIM_LOCK, _load, _save, now
from .state import CONTROL_DIR, read_json, write_json

PROJECT_ROOT = CONTROL_DIR.parents[1]
MILESTONES_PATH = CONTROL_DIR / "milestones.json"
SUITE = ["python", "-m", "unittest", "discover", "-s", "tests", "-t", "."]
SUITE_TIMEOUT = 1200
IDENTITY = ["-c", "user.name=smart_store-control", "-c", "user.email=control@smart-store.local"]
BUSY_BACKOFF_SECONDS = 600


def _later(seconds: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def pending(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Landed tasks whose branch is not yet in main, oldest first."""
    rows = [t for t in tasks if t.get("status") == "done" and t.get("phase") == "landed" and t.get("branch")
            and str(t.get("integration_retry_after") or "") <= now()]
    return sorted(rows, key=lambda t: int(t["id"]))


def integrate_branch(root: Path, task_id: int, branch: str, title: str) -> tuple[str, str]:
    """('merged', sha) | ('conflict'|'red'|'busy'|'gone', detail). Leaves no worktree behind."""
    if git(["rev-parse", "--verify", "--quiet", branch], root).returncode:
        return "gone", f"branch {branch} no longer exists"
    base = git(["rev-parse", "main"], root).stdout.strip()
    if git(["merge-base", "--is-ancestor", branch, base], root).returncode == 0:
        return "merged", base  # already contained in main (merged by hand)
    scratch = root / "data" / "control" / "scratch" / f"integrate-{task_id}"
    git(["worktree", "remove", "--force", str(scratch)], root)
    shutil.rmtree(scratch, ignore_errors=True)
    git(["worktree", "prune"], root)
    added = git(["worktree", "add", "-q", "--detach", str(scratch), base], root)
    if added.returncode:
        return "busy", "scratch worktree failed: " + (added.stderr or added.stdout).strip()[:300]
    try:
        merged = git([*IDENTITY, "merge", "--no-ff", "-m", f"control: integrate task-{task_id} ({title})", branch], scratch)
        if merged.returncode:
            conflicts = git(["diff", "--name-only", "--diff-filter=U"], scratch).stdout.split()
            git(["merge", "--abort"], scratch)
            return "conflict", "merge conflict with main in " + (", ".join(conflicts) or (merged.stdout or merged.stderr).strip()[:200])
        try:
            run = subprocess.run(SUITE, cwd=str(scratch), capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=SUITE_TIMEOUT)
        except subprocess.TimeoutExpired:
            return "red", f"full suite timed out after {SUITE_TIMEOUT}s on the merge with main"
        if run.returncode:
            lines = (run.stdout + run.stderr).splitlines()
            failures = [l for l in lines if l.startswith(("FAIL:", "ERROR:")) and "tests.test_feature" not in l]
            return "red", "full suite failed on the merge with main: " + "; ".join(failures[:8])[:600]
        sha = git(["rev-parse", "HEAD"], scratch).stdout.strip()
    finally:
        git(["worktree", "remove", "--force", str(scratch)], root)
        shutil.rmtree(scratch, ignore_errors=True)
    if git(["rev-parse", "main"], root).stdout.strip() != base:
        return "busy", "main moved while the suite ran; retrying on the new main"
    on_main = git(["symbolic-ref", "--short", "-q", "HEAD"], root).stdout.strip() == "main"
    moved = (git(["merge", "--ff-only", "-q", sha], root) if on_main
             else git(["update-ref", "refs/heads/main", sha, base], root))
    if moved.returncode:
        return "busy", "main checkout refused the fast-forward: " + (moved.stderr or moved.stdout).strip()[:300]
    return "merged", sha


DONE_CHECK_TIMEOUT_SECONDS = 600


def done_check(root: Path, task: dict[str, Any]) -> tuple[bool, str] | None:
    """Run the milestone's ``done_check`` argv on main; None when it has none.

    Merging a task is progress, not completion: M4.7 was marked done twice
    while the audit its exit criteria name still failed. A milestone with a
    done_check is done only when that command exits 0.
    """
    if task.get("source") != "milestone-workflow":
        return None
    item = next((m for m in read_json(MILESTONES_PATH, {"milestones": []}).get("milestones", [])
                 if str(m.get("id")) == str(task.get("milestone"))), None)
    argv = list((item or {}).get("done_check") or [])
    if not argv:
        return None
    if argv[0] == "python":
        argv[0] = sys.executable
    try:
        run = subprocess.run(argv, cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=DONE_CHECK_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"done check could not run: {type(exc).__name__}: {exc}"[:300]
    return run.returncode == 0, ((run.stdout or "") + (run.stderr or "")).strip()[-1500:]


def _complete_milestone(task: dict[str, Any], sha: str) -> str | None:
    if task.get("source") != "milestone-workflow":
        return None
    data = read_json(MILESTONES_PATH, {"milestones": []})
    for item in data.get("milestones", []):
        if str(item.get("id")) == str(task.get("milestone")) and item.get("status") != "done":
            item["status"] = "done"
            item["evidence"] = f"{item.get('evidence', '')} (task {task['id']} merged {sha[:7]})".strip()
            write_json(MILESTONES_PATH, data)
            return str(item["id"])
    return None


def integrate_landed(root: Path = PROJECT_ROOT, limit: int = 1) -> list[dict[str, Any]]:
    """Integrate up to ``limit`` landed tasks. The suite runs outside the ledger lock."""
    changed = []
    for candidate in pending(_load().get("tasks", []))[:limit]:
        outcome, detail = integrate_branch(root, int(candidate["id"]), str(candidate["branch"]),
                                           str(candidate.get("title") or f"task {candidate['id']}"))
        # Outside the ledger lock: the check may take a while.
        check = done_check(root, candidate) if outcome == "merged" else None
        with _CLAIM_LOCK:
            data = _load()
            task = next((t for t in data.get("tasks", []) if t["id"] == candidate["id"]), None)
            if task is None or task.get("status") != "done" or task.get("phase") != "landed":
                continue
            if outcome == "busy":
                # Not the task's fault (main moved, or the operator has local edits
                # there). Back off so a refusing checkout does not cost a full
                # suite run every autopilot tick.
                task.update({"integration_retry_after": _later(BUSY_BACKOFF_SECONDS), "integration_note": detail[:300]})
            elif outcome == "merged" and check is not None and not check[0]:
                # Keep the merged progress; the task goes on with what is left.
                note = f"done check failed after merging {detail[:7]}: " + check[1]
                loopguard.record(task, note, "done_check",
                                 key="done_check:" + hashlib.sha256(check[1].encode("utf-8")).hexdigest()[:16])
                task.update({"status": "ready", "worker": "", "reviewer": "", "phase": "done_check_failed",
                             "merged_commit": detail, "last_error": note[:1800], "note": note[:300], "updated_at": now()})
            elif outcome == "merged":
                milestone = _complete_milestone(task, detail)
                task.update({"phase": "merged", "merged_commit": detail, "updated_at": now(),
                             "note": f"merged into main at {detail[:7]}" + (f"; milestone {milestone} done" if milestone else "")})
            elif outcome == "gone":
                task.update({"phase": "merge_lost", "updated_at": now(), "note": f"integration skipped: {detail}"})
            else:
                note = ("integration: base moved, " if outcome == "conflict" else "integration: gate failed, ") + detail
                loopguard.record(task, note, "integrate")
                task.update({"status": "ready", "worker": "", "reviewer": "", "phase": "rebase_needed",
                             "last_error": note, "note": note[:300], "updated_at": now()})
            _save(data)
            changed.append(dict(task))
    return changed
