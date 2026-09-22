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
        # Identify smart_store to the AIOS proxy so it can route us to the
        # external-client reserved slot instead of contending for AIOS slots.
        "ANTHROPIC_CUSTOM_HEADERS": "X-AIOS-Client: smart_store",
    })
    return env


def build_argv(exe: str, model: str, max_turns: int, settings: Path = SETTINGS_PATH) -> list[str]:
    return [exe, "-p", "--model", model, "--max-turns", str(max_turns), "--dangerously-skip-permissions",
            "--output-format", "json", "--settings", str(settings), "--strict-mcp-config"]


def rule_path(path: Path) -> str:
    """C:/smart_store/packages -> //c/smart_store/packages (Claude Code's absolute-path rule form)."""
    posix = path.resolve().as_posix()
    if len(posix) > 1 and posix[1] == ":":
        posix = "/" + posix[0].lower() + posix[2:]
    return "/" + posix


def checkout_guard_settings(root: Path) -> Path:
    """Worker settings plus Edit/Write denials on every tracked top-level entry of the live checkout.

    The worktree sits under ``data/control/worktrees`` inside the checkout, and
    the local model kept resolving task files against the checkout root
    (``C:/smart_store/packages/...``), which ``--dangerously-skip-permissions``
    allowed; the edits were only quarantined after the run was wasted. Deny
    rules still apply in that mode, so the write now fails at the tool call and
    the model is told to use the worktree path. ``data/`` stays writable
    because the worktree itself lives there.
    """
    settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    entries = git(["ls-tree", "--name-only", "HEAD"], root).stdout.split()
    deny = settings.setdefault("permissions", {}).setdefault("deny", [])
    for entry in entries:
        if entry in ("data", "build"):
            continue
        target = rule_path(root / entry) + ("/**" if (root / entry).is_dir() else "")
        deny += [f"Edit({target})", f"Write({target})", f"NotebookEdit({target})"]
    path = root / "data" / "control" / "worker_settings.generated.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# Spare-capacity lanes that do not consume the shared local llama.cpp slots.
# Both read the prompt from stdin and edit files in the task worktree like the
# claude-local lane; only argv, environment and output parsing differ.
CURSOR_EXE = Path.home() / "AppData" / "Local" / "cursor-agent" / "cursor-agent.cmd"
EXTERNAL_ENGINES = ("cursor", "gemini")


def engine_command(engine: str, model: str, max_turns: int) -> tuple[list[str], dict[str, str], str]:
    """(argv, env, output_kind) for an engine. output_kind: 'json' or 'text'."""
    base = {key: os.environ[key] for key in BASE_ENV_KEYS if key in os.environ}
    if engine == "cursor":
        exe = shutil.which("cursor-agent") or (str(CURSOR_EXE) if CURSOR_EXE.exists() else None)
        if not exe:
            raise RuntimeError("cursor-agent not found")
        argv = [exe, "-p", "--force", "--output-format", "text"] + (["--model", model] if model else [])
        return argv, base, "text"
    if engine == "gemini":
        exe = shutil.which("gemini")
        if not exe:
            raise RuntimeError("gemini CLI not found")
        argv = [exe, "-p", "Follow the task instructions given on stdin.", "--approval-mode", "yolo",
                "-o", "text"] + (["-m", model] if model else [])
        return argv, base, "text"
    raise RuntimeError(f"unknown external engine {engine}")


def task_prompt(task: dict) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    body = {"id": task.get("id"), "title": task.get("title"), "milestone": task.get("milestone"),
            "prompt": task.get("prompt"), "retry_count": task.get("retry_count", 0),
            "last_error": task.get("last_error"), "note": task.get("note")}
    instructions = task.get("operator_instructions") or []
    extra = ""
    if instructions:
        extra = "\n\n## 운영자 추가 지시 (가장 우선한다)\n" + "\n".join(
            f"- ({i.get('at', '')}) {i.get('text', '')}" for i in instructions[-5:])
    return template + "\n\n## task\n```json\n" + json.dumps(body, ensure_ascii=False, indent=2) + "\n```\n" + extra


def worktree_note(root: Path, worktree: Path) -> str:
    return (f"\n\n## 작업 위치\n- 너의 작업 디렉터리(저장소 루트)는 `{worktree}` 이다. 파일은 이 디렉터리 기준 상대 경로"
            f"(예: `packages/store_core/x.py`)로 읽고 써라.\n- `{root}` 바로 아래의 packages·tests 등은 사람의 체크아웃이라 "
            "쓰기가 거부된다. 거부되면 경로를 작업 디렉터리 기준으로 고쳐서 다시 써라.\n")


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


def kill_agent_tree(argv: list[str]) -> None:
    """Kill every process started for this agent run.

    On Windows the CLI is launched through a .cmd shim; killing only the shim
    leaves the real process (claude.exe, node) running, holding the local LLM
    slot and still able to write files. Match by the run's own settings path or
    binary so nothing outside this run is touched.
    """
    marker = next((a for a in argv if a.endswith("worker_settings_local.json") and "smart_store_control" in a), None) or argv[0]
    try:
        listing = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "Get-CimInstance Win32_Process | ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return
    for line in listing.splitlines():
        pid, _, cmdline = line.partition("\t")
        if marker in cmdline and "Win32_Process" not in cmdline and pid.strip().isdigit():
            subprocess.run(["taskkill", "/PID", pid.strip(), "/T", "/F"], capture_output=True, timeout=30)


def main_checkout_state(root: Path) -> set[str]:
    out = git(["status", "--porcelain", "--", ".", ":!data", ":!build"], root).stdout
    return {line[3:].strip() for line in out.splitlines() if line.strip()}


def quarantine_stray_edits(root: Path, before: set[str], task_id: int, artifact_dir: Path | None) -> list[str]:
    """Paths the agent changed in the live checkout: saved as evidence, then restored.

    AIOS lesson #15: a worker that writes to the human's checkout corrupts it
    and blocks every fast-forward. Only paths that were clean before the run
    are touched, so the operator's own uncommitted work is never reverted.
    """
    stray = sorted(main_checkout_state(root) - before)
    if not stray:
        return []
    keep = artifact_dir or (root / "data" / "control" / "artifacts")
    folder = keep / "stray"
    folder.mkdir(parents=True, exist_ok=True)
    tracked = [p for p in stray if git(["ls-files", "--error-unmatch", p], root).returncode == 0]
    untracked = [p for p in stray if p not in tracked]
    if tracked:
        diff = git(["diff", "--binary", "--", *tracked], root).stdout
        (folder / f"task-{task_id}.patch").write_bytes(diff.encode("utf-8"))
        git(["checkout", "--", *tracked], root)
    for rel in untracked:
        src = root / rel
        if src.is_file():
            dst = folder / f"task-{task_id}-untracked" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        elif src.is_dir():
            shutil.rmtree(src, ignore_errors=True)
    return stray


def run_agent(root: Path, task: dict, *, model: str, base_url: str, max_turns: int = DEFAULT_MAX_TURNS,
              wall_seconds: int = DEFAULT_WALL_SECONDS, spawn: Spawn = subprocess.run,
              artifact_dir: Path | None = None, engine: str = "claude-local") -> tuple[str, dict]:
    """Run the CLI in a fresh worktree; return (diff, summary). The worktree is removed afterwards."""
    before = main_checkout_state(root)
    worktree = prepare_worktree(root, int(task["id"]))
    summary: dict = {"engine": engine, "model": model, "max_turns": max_turns}
    try:
        if engine == "claude-local":
            argv = build_argv(claude_executable(), model, max_turns, checkout_guard_settings(root))
            env, kind = local_env(model, base_url), "json"
        else:
            argv, env, kind = engine_command(engine, model, max_turns)
        try:
            completed = spawn(argv, cwd=str(worktree), input=task_prompt(task) + worktree_note(root, worktree),
                              capture_output=True, text=True,
                              timeout=wall_seconds, env=env, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired as exc:
            # The wall clock is a fact about throughput, not a crash: record it as
            # a summary the worker can act on, and make sure the whole process
            # tree is gone (subprocess only kills the .cmd shim on Windows).
            kill_agent_tree(argv)
            stray = quarantine_stray_edits(root, before, int(task["id"]), artifact_dir)  # a timed-out run can have strayed too
            if stray:
                summary["stray_edits"] = stray
            summary.update({"returncode": None, "timeout": True, "wall_seconds": wall_seconds,
                            "result": f"wall clock of {wall_seconds}s exceeded",
                            "stderr": (exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or ""))[-800:]})
            diff = worktree_diff(worktree)
            if artifact_dir is not None:
                artifact_dir.mkdir(parents=True, exist_ok=True)
                (artifact_dir / f"task-{task['id']}.agent.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            return "", summary  # partial edits are never taken as a patch
        summary["returncode"] = completed.returncode
        if kind == "json":
            try:
                result = json.loads(completed.stdout or "{}")
            except json.JSONDecodeError:
                result = {"raw": (completed.stdout or "")[-2000:]}
        else:
            result = {"result": (completed.stdout or "")[-2000:]}
        summary.update({k: result.get(k) for k in ("num_turns", "is_error", "subtype", "duration_ms") if k in result})
        denied = [str((d.get("tool_input") or {}).get("file_path") or d.get("tool_name"))
                  for d in result.get("permission_denials") or [] if isinstance(d, dict)]
        if denied:
            # Writes the checkout guard refused: tells "gave up after being denied"
            # apart from "never tried" when a run ends without a diff.
            summary["denied_writes"] = denied[:20]
        summary["result"] = str(result.get("result") or result.get("raw") or "")[:1500]
        summary["stderr"] = (completed.stderr or "")[-800:]
        stray = quarantine_stray_edits(root, before, int(task["id"]), artifact_dir)
        if stray:
            summary["stray_edits"] = stray
        diff = worktree_diff(worktree)
        if artifact_dir is not None:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / f"task-{task['id']}.agent.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return diff, summary
    finally:
        remove_worktree(root, worktree)
