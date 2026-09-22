"""claude-local engine: the AIOS way of driving the shared local model.

AIOS gets an 82% merge rate from the same qwen3.6-35b-a3b by running the
Claude Code CLI headless against ``local_llm_proxy.py`` on :8081, so the model
reads and edits real files with tools instead of emitting whole files cold.
Asked for whole files, the same model refused smart_store tasks that mention
orders or purchase orders. This engine reproduces the AIOS lane for
smart_store without touching AIOS or swapping the llama.cpp server:

- same CLI binary, same environment switch (``ANTHROPIC_BASE_URL`` -> proxy),
  same stripped base environment, ``--strict-mcp-config`` so no desktop MCP
  servers load, ``--dangerously-skip-permissions`` inside a throwaway worktree;
- one git worktree per task (``data/control/worktrees/task-<id>``) checked out
  from HEAD, so a concurrent worker or the human checkout is never touched;
- the CLI never commits or pushes; the worker takes ``git diff`` of the
  worktree as the patch artifact and the existing review flow decides.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from .context import git
from typing import Callable

BASE_ENV_KEYS = ("PATH", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "SYSTEMROOT", "TEMP", "TMP",
                 "COMSPEC", "PATHEXT", "USERNAME", "HOMEDRIVE", "HOMEPATH", "PROGRAMFILES", "PROGRAMDATA")
DEFAULT_MAX_TURNS = 45
DEFAULT_WALL_SECONDS = 1500
HERE = Path(__file__).resolve().parent
PROMPT_PATH = HERE / "WORKER_PROMPT_local.md"
SETTINGS_PATH = HERE / "worker_settings_local.json"

Spawn = Callable[..., subprocess.CompletedProcess]


def claude_executable() -> str:
    found = shutil.which("claude")
    if not found:
        raise RuntimeError("claude CLI not found on PATH")
    return found


def local_env(model: str, base_url: str) -> dict[str, str]:
    """Only the allow-listed base variables plus the proxy switch (no inherited ANTHROPIC_*/CLAUDE_*)."""
    env = {key: os.environ[key] for key in BASE_ENV_KEYS if key in os.environ}
    env.update({
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_API_KEY": "local",
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_SMALL_FAST_MODEL": model,
        "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    })
    return env


def build_argv(exe: str, model: str, max_turns: int) -> list[str]:
    return [exe, "-p", "--model", model, "--max-turns", str(max_turns), "--dangerously-skip-permissions",
            "--output-format", "json", "--settings", str(SETTINGS_PATH), "--strict-mcp-config"]


def task_prompt(task: dict) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    body = {"id": task.get("id"), "title": task.get("title"), "milestone": task.get("milestone"),
            "prompt": task.get("prompt"), "retry_count": task.get("retry_count", 0),
            "last_error": task.get("last_error"), "note": task.get("note")}
    return template + "\n\n## task\n```json\n" + json.dumps(body, ensure_ascii=False, indent=2) + "\n```\n"


def prepare_worktree(root: Path, task_id: int) -> Path:
    path = root / "data" / "control" / "worktrees" / f"task-{task_id}"
    git(["worktree", "prune"], root)
    if path.exists():
        git(["worktree", "remove", "--force", str(path)], root)
        shutil.rmtree(path, ignore_errors=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    added = git(["worktree", "add", "-q", "--detach", str(path), "HEAD"], root)
    if added.returncode:
        raise RuntimeError("worktree add failed: " + (added.stderr or added.stdout).strip()[:300])
    return path


def remove_worktree(root: Path, path: Path) -> None:
    git(["worktree", "remove", "--force", str(path)], root)
    shutil.rmtree(path, ignore_errors=True)


def worktree_diff(path: Path) -> str:
    git(["add", "-A"], path)
    diff = git(["diff", "--cached", "--binary"], path)
    return diff.stdout


def run_agent(root: Path, task: dict, *, model: str, base_url: str, max_turns: int = DEFAULT_MAX_TURNS,
              wall_seconds: int = DEFAULT_WALL_SECONDS, spawn: Spawn = subprocess.run,
              artifact_dir: Path | None = None) -> tuple[str, dict]:
    """Run the CLI in a fresh worktree; return (diff, summary). The worktree is removed afterwards."""
    worktree = prepare_worktree(root, int(task["id"]))
    summary: dict = {"engine": "claude-local", "model": model, "max_turns": max_turns}
    try:
        completed = spawn(build_argv(claude_executable(), model, max_turns), cwd=str(worktree), input=task_prompt(task),
                          capture_output=True, text=True, timeout=wall_seconds, env=local_env(model, base_url),
                          encoding="utf-8", errors="replace")
        summary["returncode"] = completed.returncode
        try:
            result = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError:
            result = {"raw": (completed.stdout or "")[-2000:]}
        summary.update({k: result.get(k) for k in ("num_turns", "is_error", "subtype", "duration_ms") if k in result})
        summary["result"] = str(result.get("result") or result.get("raw") or "")[:1500]
        summary["stderr"] = (completed.stderr or "")[-800:]
        diff = worktree_diff(worktree)
        if artifact_dir is not None:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / f"task-{task['id']}.agent.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return diff, summary
    finally:
        remove_worktree(root, worktree)
