"""Land reviewed patches on a per-task branch.

Before this, ``reviewed`` was a dead end: nothing consumed it, so even a
passed review left the task parked forever. A reviewed patch is now applied
in a scratch worktree on branch ``control/task-<id>`` created from HEAD,
committed there, and the task is marked ``done`` with the commit and branch
in its note. Nothing is pushed and main is never touched; merging is the
operator's call, as in the AIOS lane where CI gates the branch.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .context import git
from .pm import _CLAIM_LOCK, _load, _save, now
from .state import CONTROL_DIR

PROJECT_ROOT = CONTROL_DIR.parents[1]


def land_patch(root: Path, task_id: int, patch_path: Path, title: str) -> tuple[str | None, str]:
    """Return (commit_sha, message). sha is None on failure."""
    patch_path = patch_path.resolve()
    branch = f"control/task-{task_id}"
    scratch = root / "data" / "control" / "scratch" / f"land-{task_id}"
    git(["worktree", "remove", "--force", str(scratch)], root)
    shutil.rmtree(scratch, ignore_errors=True)
    git(["worktree", "prune"], root)
    git(["branch", "-D", branch], root)  # a previous landing attempt; the ledger is the record
    added = git(["worktree", "add", "-q", "-b", branch, str(scratch), "HEAD"], root)
    if added.returncode:
        return None, "worktree add failed: " + (added.stderr or added.stdout).strip()[:300]
    try:
        applied = git(["apply", "--index", str(patch_path)], scratch)
        if applied.returncode:
            return None, "patch failed to apply: " + (applied.stderr or applied.stdout).strip()[:500]
        committed = git(["-c", "user.name=smart_store-control", "-c", "user.email=control@smart-store.local",
                         "commit", "-q", "-m", f"control(task-{task_id}): {title}",
                         "-m", "Landed by smart_store_control after objective gate and review."], scratch)
        if committed.returncode:
            return None, "commit failed: " + (committed.stderr or committed.stdout).strip()[:300]
        sha = git(["rev-parse", "--short", "HEAD"], scratch).stdout.strip()
        return sha, f"landed on {branch} at {sha}"
    finally:
        git(["worktree", "remove", "--force", str(scratch)], root)
        shutil.rmtree(scratch, ignore_errors=True)


def land_reviewed(root: Path = PROJECT_ROOT, limit: int = 2) -> list[dict]:
    """Land up to ``limit`` reviewed tasks; returns the ledger rows changed."""
    changed = []
    with _CLAIM_LOCK:
        data = _load()
        for task in data.get("tasks", []):
            if task.get("status") != "reviewed" or len(changed) >= limit:
                continue
            patch = Path(str(task.get("artifact") or ""))
            if not patch.is_file():
                task.update({"status": "blocked", "phase": "error", "note": "landing: patch artifact missing", "updated_at": now()})
                changed.append(dict(task))
                continue
            sha, message = land_patch(root, int(task["id"]), patch, str(task.get("title") or f"task {task['id']}"))
            if sha:
                task.update({"status": "done", "phase": "landed", "commit": sha, "branch": f"control/task-{task['id']}",
                             "note": message, "updated_at": now()})
            else:
                task.update({"status": "blocked", "phase": "error", "note": "landing failed: " + message, "updated_at": now()})
            changed.append(dict(task))
        if changed:
            _save(data)
    return changed
