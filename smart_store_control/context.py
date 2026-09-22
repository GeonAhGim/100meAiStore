"""Repository context and patch validation for the local-LLM implementation worker.

Why this exists: the first 13 control tasks were all blocked because the local
model was asked for a unified diff with no view of the repository. It invented
paths such as ``smart_store/worker.py``; the reviewer correctly failed every
attempt and the retry cap sealed the task. Two fixes:

- ``repository_context`` gives the model the real file list plus the contents
  of the files the task names, so a diff can target paths that exist.
- ``check_patch`` runs ``git apply --check`` before any review. A diff that
  cannot apply is sent back with the exact git error as feedback for the next
  attempt instead of spending a review round on it.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

TREE_PREFIXES = ("packages/", "smart_store_aios/", "smart_store_control/", "tests/", "scripts/", "docs/implementation/")
MAX_TREE_LINES = 400
MAX_FILES = 4
MAX_FILE_CHARS = 6000
_FILES_RE = re.compile(r"(?:Evidence/)?files to inspect:\s*(.+?)(?:\.(?:\s|$)|\n|$)", re.IGNORECASE)


def named_files(prompt: str) -> list[str]:
    match = _FILES_RE.search(prompt or "")
    if not match:
        return []
    raw = re.split(r"[;,]\s*", match.group(1).strip().rstrip("."))
    return [p.strip().replace("\\", "/") for p in raw if p.strip()][:MAX_FILES]


def file_tree(root: Path) -> list[str]:
    completed = subprocess.run(["git", "ls-files"], cwd=str(root), capture_output=True, text=True, check=False)
    paths = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    keep = [p for p in paths if p.startswith(TREE_PREFIXES) and not p.endswith((".png", ".docx"))]
    return keep[:MAX_TREE_LINES]


def repository_context(root: Path, prompt: str, feedback: str | None = None) -> str:
    parts = ["\n\nREPOSITORY FILES (only these paths exist; new files are allowed under tests/):\n"]
    parts.append("\n".join(file_tree(root)))
    for rel in named_files(prompt):
        path = root / rel
        if not path.is_file():
            parts.append(f"\n\nFILE {rel}: does not exist")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        clipped = text[:MAX_FILE_CHARS] + ("\n... (truncated)" if len(text) > MAX_FILE_CHARS else "")
        parts.append(f"\n\nFILE {rel}:\n```\n{clipped}\n```")
    if feedback:
        parts.append("\n\nPREVIOUS ATTEMPT FAILED. Fix exactly this before anything else:\n" + feedback[:2000])
    parts.append(
        "\n\nRULES: return one unified diff in a ```diff block with a/ and b/ prefixes, paths relative to the "
        "repository root, that applies with `git apply --check`. Modify only paths listed above or add tests. "
        "Include focused tests. No prose outside the diff block."
    )
    return "".join(parts)


REWRITE_MIN_LINES = 40
REWRITE_MAX_RATIO = 0.8


def rewrite_violation(root: Path, patch_path: Path) -> str | None:
    """Name the first pre-existing file the patch rewrites wholesale, else None (AIOS lesson #6)."""
    numstat = subprocess.run(["git", "apply", "--numstat", str(patch_path)], cwd=str(root),
                             capture_output=True, text=True, check=False).stdout
    for line in numstat.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3 or parts[1] == "-":
            continue
        deleted, path = int(parts[1]), parts[2].strip()
        shown = subprocess.run(["git", "show", f"HEAD:{path}"], cwd=str(root), capture_output=True, text=True, check=False)
        if shown.returncode:
            continue  # new file
        lines = shown.stdout.count("\n")
        if lines >= REWRITE_MIN_LINES and deleted / max(lines, 1) > REWRITE_MAX_RATIO:
            return f"wholesale rewrite of existing file rejected: {path}"
    return None


def check_patch(root: Path, patch_path: Path) -> str | None:
    """Return None when the patch applies cleanly, else the git error text."""
    if patch_path.stat().st_size < 20:
        return "patch is empty"
    completed = subprocess.run(["git", "apply", "--check", str(patch_path)], cwd=str(root),
                               capture_output=True, text=True, check=False)
    if completed.returncode == 0:
        return None
    return (completed.stderr or completed.stdout).strip()[:1500] or "git apply --check failed"
