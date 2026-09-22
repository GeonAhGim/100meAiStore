"""Whole-file exchange for small local models.

A 35B local model reliably emits complete files but not valid unified diffs:
its hunk headers drift and it edits files it never saw. So the worker runs
two short calls instead of one:

1. plan: the model names the existing files it will change (JSON list),
   chosen from the real file list;
2. write: the model receives those files in full and returns the complete new
   content of each changed or added file in fenced blocks.

The worker then writes the files into a scratch git worktree and lets git
produce the diff, so the patch always applies. A whole-file rewrite that
deletes more than 80% of a file with 40+ lines is rejected (AIOS lesson #6).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

MAX_PLAN_FILES = 4
MAX_FILE_CHARS = 12000
REWRITE_MIN_LINES = 40
REWRITE_MAX_RATIO = 0.8
_BLOCK = re.compile(r"<<<FILE\s+(.+?)>>>\s*\n(.*?)\n?<<<END>>>", re.DOTALL)
# Small models often ignore the requested wrapper and answer with a fenced
# code block preceded by the path, or a fence whose info string carries the
# path ("```python path/to/file.py" or "```python:path/to/file.py").
_FENCE = re.compile(
    r"(?:^|\n)(?:#+\s*|\*\*|File:\s*|FILE:\s*|`)?(?P<lead>[\w./-]+\.[\w]+)`?\*{0,2}:?\s*\n```[\w+-]*[ :]*(?P<lead_path>[\w./-]*)\s*\n(?P<body>.*?)\n```",
    re.DOTALL)
_FENCE_INFO = re.compile(r"(?:^|\n)```[\w+-]*[ :]+(?P<path>[\w./-]+\.[\w]+)\s*\n(?P<body>.*?)\n```", re.DOTALL)
_JSON = re.compile(r"\[[^\[\]]*\]", re.DOTALL)


CREATE_PREFIXES = ("packages/", "smart_store_aios/", "smart_store_control/", "tests/", "docs/implementation/", "scripts/")


def _clean(path: str) -> str:
    return path.strip().replace("\\", "/").removeprefix("./")


def parse_plan(text: str, existing: set[str]) -> tuple[list[str], list[str]]:
    """(modify, create) from the model's plan.

    Accepts ``{"modify": [...], "create": [...]}`` or a bare JSON list (treated
    as modify). ``modify`` keeps only existing paths; ``create`` keeps only new
    paths under project prefixes. A task that adds files without touching any
    existing one is a legitimate plan.
    """
    raw = text or ""
    obj = re.search(r"\{.*\}", raw, re.DOTALL)
    items: dict = {}
    try:
        if obj:
            loaded = json.loads(obj.group(0))
            if isinstance(loaded, dict):
                items = loaded
        if not items:
            match = _JSON.search(raw)
            items = {"modify": json.loads(match.group(0))} if match else {}
    except json.JSONDecodeError:
        items = {}
    modify: list[str] = []
    for item in items.get("modify", []) or []:
        if isinstance(item, str):
            path = _clean(item)
            if path in existing and path not in modify:
                modify.append(path)
    create: list[str] = []
    for item in items.get("create", []) or []:
        if isinstance(item, str):
            path = _clean(item)
            if (path not in existing and path.startswith(CREATE_PREFIXES) and ".." not in path.split("/")
                    and path not in create):
                create.append(path)
    return modify[:MAX_PLAN_FILES], create[:MAX_PLAN_FILES]


def parse_files(text: str) -> dict[str, str]:
    """``<<<FILE path>>> ... <<<END>>>`` blocks to {path: content}."""
    out: dict[str, str] = {}
    text = text or ""

    def _add(raw_path: str, body: str) -> None:
        path = raw_path.strip().replace("\\", "/").removeprefix("./")
        if path and "/" in path and ".." not in path.split("/") and path not in out:
            out[path] = body.rstrip("\n") + "\n"

    for match in _BLOCK.finditer(text):
        _add(match.group(1), match.group(2))
    if out:
        return out
    for match in _FENCE_INFO.finditer(text):          # ```python path/to/file.py
        _add(match.group("path"), match.group("body"))
    for match in _FENCE.finditer(text):               # path line, then a fence
        _add(match.group("lead_path") or match.group("lead"), match.group("body"))
    return out


def allowed_target(path: str, planned: list[str]) -> bool:
    """``planned`` holds both the modify and create lists; tests are always allowed."""
    return path in planned or (path.startswith("tests/") and path.endswith(".py"))


def build_patch(root: Path, files: dict[str, str], planned: list[str], out_path: Path) -> str | None:
    """Write files into a scratch worktree, diff with git, return an error or None."""
    if not files:
        return "no <<<FILE path>>> blocks in the response"
    bad = [p for p in files if not allowed_target(p, planned)]
    if bad:
        return "files outside the plan or tests/: " + ", ".join(bad)
    scratch = root / "data" / "control" / "scratch" / out_path.stem
    if scratch.exists():
        subprocess.run(["git", "worktree", "remove", "--force", str(scratch)], cwd=str(root), capture_output=True)
        shutil.rmtree(scratch, ignore_errors=True)
    subprocess.run(["git", "worktree", "prune"], cwd=str(root), capture_output=True)
    added = subprocess.run(["git", "worktree", "add", "-q", "--detach", str(scratch), "HEAD"], cwd=str(root),
                           capture_output=True, text=True)
    if added.returncode:
        return "scratch worktree failed: " + (added.stderr or added.stdout).strip()[:300]
    try:
        for rel, content in files.items():
            target = scratch / rel
            original = target.read_text(encoding="utf-8", errors="replace") if target.exists() else None
            if original is not None:
                old_lines = original.count("\n")
                kept = sum(1 for line in set(content.splitlines()) if line in original.splitlines())
                deleted = max(old_lines - kept, 0)
                if old_lines >= REWRITE_MIN_LINES and deleted / max(old_lines, 1) > REWRITE_MAX_RATIO:
                    return f"wholesale rewrite rejected: {rel}"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(scratch), capture_output=True)
        diff = subprocess.run(["git", "diff", "--cached", "--binary"], cwd=str(scratch), capture_output=True, text=True)
        if not diff.stdout.strip():
            return "response reproduced the existing files unchanged"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(diff.stdout, encoding="utf-8")
        return None
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(scratch)], cwd=str(root), capture_output=True)
        shutil.rmtree(scratch, ignore_errors=True)


def plan_prompt(task_prompt: str, tree: list[str], feedback: str | None) -> str:
    return (
        "You are planning a small change in the smart_store repository. Task:\n" + task_prompt +
        "\n\nREPOSITORY FILES:\n" + "\n".join(tree) +
        ("\n\nPREVIOUS ATTEMPT FAILED:\n" + feedback[:1500] if feedback else "") +
        f"\n\nReply with ONLY a JSON object: {{\"modify\": [existing paths from the list above you will change], "
        f"\"create\": [new paths you will add]}}. At most {MAX_PLAN_FILES} of each. New paths must start with one of: "
        + ", ".join(CREATE_PREFIXES) + "."
    )


def write_prompt(task_prompt: str, contents: dict[str, str], feedback: str | None,
                 create: list[str] | None = None) -> str:
    shown = "".join(f"\n\n<<<FILE {p}>>>\n{c[:MAX_FILE_CHARS]}\n<<<END>>>" for p, c in contents.items())
    planned_new = ("\n\nNew files you planned to create: " + ", ".join(create)) if create else ""
    return (
        "You are the smart_store local implementation worker. Do not use Codex, do not modify any other "
        "project, preserve dry_run and safety gates. Task:\n" + task_prompt +
        "\n\nCurrent contents of the files you planned to change:" + (shown or " (none)") + planned_new +
        ("\n\nPREVIOUS ATTEMPT FAILED:\n" + feedback[:1500] if feedback else "") +
        "\n\nReturn the COMPLETE new content of every file you change, and of any new test file under tests/, "
        "each wrapped exactly as:\n<<<FILE path/from/repo/root>>>\n...full file...\n<<<END>>>\n"
        "Make targeted edits: keep everything you do not need to change. No prose outside the blocks."
    )
