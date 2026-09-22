"""Autonomous development pipeline for the local Codex worker pool.

A ``dev.task`` job drives one bounded implementation packet through fixed
stages, checkpointing after each so a restarted worker resumes instead of
repeating work:

    spec -> implement -> verify -> (fix -> verify)* -> publish -> done

Guards, each traced to a failure observed while operating the AIOS local
worker pool (numbers refer to ``docs/implementation/dev-pipeline.md``):

- The worker decides success by running the tests itself (#5). A model reply
  saying "all tests pass" is never trusted.
- A stage that yields no diff is not progress (#5); identical failure output
  in consecutive fix rounds aborts as a stall instead of looping (#2).
- Existing files may not be rewritten wholesale (#6), the acceptance table in
  the spec is immutable after the spec stage (#7, #18), and an optional file
  allowlist bounds what may change (#8).
- Payload fields are validated before any model call; a bad payload is
  dead-lettered immediately, never retried (#9, #11).
- Throttling from the model backend defers the job without consuming an
  attempt (#12, #13).
- Each task runs in its own git worktree; any change that appears in the main
  checkout during a stage fails the stage (#15).
- Every model call has a wall-clock budget and the lease is extended before
  each stage, so a hung stage cannot be executed twice (#4, #16).
- Publication requires a passing test run and a clean security scan (#17),
  appends evidence to the spec and commits; pushing is off unless enabled.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import Settings
from .db import StoreDB

USAGE_LIMIT_MARKERS = ("usage limit", "rate limit", "429", "quota", "too many requests")
REWRITE_MIN_LINES = 40      # files shorter than this may be rewritten freely
REWRITE_MAX_RATIO = 0.8     # deleted lines / original lines above this = rewrite


class UsageLimited(RuntimeError):
    """The model backend is throttled; retry later without burning an attempt.

    ``retry_at`` is the UTC time the backend said it will accept work again,
    when it said so; the worker defers until then instead of the default delay.
    """

    def __init__(self, message: str, retry_at: datetime | None = None) -> None:
        super().__init__(message)
        self.retry_at = retry_at


def _retry_at(text: str) -> datetime | None:
    """Parse 'try again at Sep 26th, 2026 11:48 PM' style hints. None if absent."""
    match = re.search(r"try again at ([A-Za-z]{3,9} \d{1,2})(?:st|nd|rd|th)?,? (\d{4}) (\d{1,2}:\d{2} ?[AP]M)", text)
    if not match:
        return None
    for fmt in ("%b %d %Y %I:%M %p", "%B %d %Y %I:%M %p", "%b %d %Y %I:%M%p", "%B %d %Y %I:%M%p"):
        try:
            local = datetime.strptime(" ".join(match.groups()), fmt)
            return local.astimezone(timezone.utc)  # hint is in local time
        except ValueError:
            continue
    return None


class PermanentFailure(RuntimeError):
    """Retrying cannot help (bad payload, exhausted fix budget, guard violation)."""


class StageTimeout(RuntimeError):
    pass


@dataclass
class StageResult:
    ok: bool
    output: str
    detail: dict = field(default_factory=dict)


Runner = Callable[[list[str], Path, int], subprocess.CompletedProcess]


def codex_executable() -> str:
    """Resolve the Codex CLI. On Windows the npm shim is ``codex.cmd``, which a
    bare ``codex`` argv cannot find without a shell; resolving it up front turns
    a cryptic WinError 2 into a clear message."""
    found = shutil.which("codex")
    if not found:
        raise RuntimeError("codex CLI not found on PATH")
    return found


def default_runner(command: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    if command and command[0] == "codex":
        command = [codex_executable(), *command[1:]]
    return subprocess.run(command, cwd=str(cwd), check=False, text=True, capture_output=True,
                          timeout=timeout, encoding="utf-8", errors="replace")


def _failure_digest(output: str) -> str:
    """Hash test output with timings, addresses and temp paths removed, so equal failures compare equal."""
    text = re.sub(r"in \d+\.\d+s", "", output)
    text = re.sub(r"0x[0-9a-fA-F]+", "", text)
    text = re.sub(r"[A-Za-z]:\\[^\s'\"]+|/tmp/[^\s'\"]+", "", text)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not value:
        raise PermanentFailure("task_id must contain letters or digits")
    return value[:40]


def validate_payload(payload: dict) -> dict:
    """Return a normalized payload or raise PermanentFailure (#9, #11)."""
    if not isinstance(payload, dict):
        raise PermanentFailure("dev.task payload must be an object")
    out = {"task_id": _slug(str(payload.get("task_id", "")))}
    for key in ("title", "goal"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise PermanentFailure(f"dev.task '{key}' must be a non-empty string")
        out[key] = value.strip()
    acceptance = payload.get("acceptance")
    if not isinstance(acceptance, list) or not acceptance or not all(isinstance(a, str) and a.strip() for a in acceptance):
        raise PermanentFailure("dev.task 'acceptance' must be a non-empty list of strings")
    out["acceptance"] = [a.strip() for a in acceptance]
    files = payload.get("files") or []
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        raise PermanentFailure("dev.task 'files' must be a list of strings")
    out["files"] = [f.replace("\\", "/").strip("/") for f in files]
    rounds = payload.get("max_fix_rounds")
    if rounds is not None and (not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 0):
        raise PermanentFailure("dev.task 'max_fix_rounds' must be a non-negative integer")
    out["max_fix_rounds"] = rounds
    command = payload.get("test_command")
    if command is not None and (not isinstance(command, list) or not all(isinstance(c, str) for c in command)):
        raise PermanentFailure("dev.task 'test_command' must be a list of strings")
    out["test_command"] = command
    return out


class DevPipeline:
    def __init__(self, settings: Settings, db: StoreDB, job: dict, worker_id: str,
                 *, runner: Runner = default_runner, repo_root: Path | None = None) -> None:
        self.settings = settings
        self.db = db
        self.job = job
        self.worker_id = worker_id
        self.runner = runner
        self.repo_root = (repo_root or Path.cwd()).resolve()
        p = validate_payload(job["payload"])
        self.task_id, self.title, self.goal = p["task_id"], p["title"], p["goal"]
        self.acceptance, self.allowed_files = p["acceptance"], p["files"]
        self.max_fix_rounds = settings.dev_max_fix_rounds if p["max_fix_rounds"] is None else p["max_fix_rounds"]
        self.test_command = list(p["test_command"] or settings.dev_test_command)
        self.checkpoint = dict(job.get("checkpoint") or {})
        self.branch = f"worker/{self.task_id}"
        self.worktree = self.repo_root / "data" / "worktrees" / self.task_id
        self.spec_path = Path("docs/implementation") / f"{self.task_id}-l4.md"
        self.acceptance_ids = [f"{self.task_id.upper()}-{i:02d}" for i in range(1, len(self.acceptance) + 1)]

    # ------------------------------------------------------------ lifecycle
    def run(self) -> dict:
        if not self.settings.codex_enabled:
            return {"dry_run": True, "skipped": "codex.enabled=false", "task_id": self.task_id}
        self._ensure_worktree()
        stage = self.checkpoint.get("stage", "spec")
        while stage != "done":
            self._heartbeat()
            stage = getattr(self, f"_stage_{stage}")()
            self._save(stage=stage)
        return {"task_id": self.task_id, "branch": self.branch, **self.checkpoint}

    def _heartbeat(self) -> None:
        self.db.heartbeat(self.job["id"], self.worker_id, self.settings.lease_seconds)

    def _save(self, **updates) -> None:
        self.checkpoint.update(updates)
        self.db.checkpoint(self.job["id"], self.worker_id, self.checkpoint)

    # --------------------------------------------------------------- stages
    def _stage_spec(self) -> str:
        spec_file = self.worktree / self.spec_path
        if not spec_file.exists():
            prompt = (
                f"Write the L4 implementation packet for task '{self.title}' to {self.spec_path.as_posix()}.\n"
                f"Goal: {self.goal}\n"
                "Follow docs/implementation/L4-development-notes.md section 2 (common contract). "
                "Sections: title line '# <title> — L4 packet', 'Status: implementation packet.', "
                "'## Contract' (inputs, outputs, state transitions, failure handling), "
                "'## Acceptance evidence' containing exactly this table:\n"
                f"{self._acceptance_table()}\n"
                "Do not write code in this step. Do not claim anything is implemented."
            )
            self._codex(prompt, "spec")
            if not spec_file.exists():
                raise RuntimeError("spec stage produced no document at " + self.spec_path.as_posix())
        text = spec_file.read_text(encoding="utf-8")
        if not all(aid in text for aid in self.acceptance_ids):
            raise RuntimeError("spec is missing acceptance IDs: " + ", ".join(self.acceptance_ids))
        # Commit only the spec so any implementation already present (manual
        # mode, or a model that got ahead of itself) still shows as the
        # implement stage's diff.
        self._git("add", "--", self.spec_path.as_posix())
        self._git("commit", "-q", "-m", f"spec({self.task_id}): L4 packet", "--", self.spec_path.as_posix())
        self._save(spec_commit=self._head(), acceptance_digest=self._acceptance_digest())
        return "implement"

    def _stage_implement(self) -> str:
        scope = (f"Only change these paths plus tests/ and the spec: {', '.join(self.allowed_files)}. "
                 if self.allowed_files else "")
        prompt = (
            f"Implement task '{self.title}' exactly as specified in {self.spec_path.as_posix()}.\n"
            f"Goal: {self.goal}\n{scope}"
            "Add tests that reference each acceptance ID from the spec table in a test name or docstring. "
            "Do not modify the acceptance table. Do not rewrite existing files wholesale; make targeted edits. "
            "Do not commit; the worker commits after verification."
        )
        self._codex(prompt, "implement")
        self._require_progress("implement")
        return "verify"

    def _stage_verify(self) -> str:
        self._guard_diff()
        result = self._run_tests()
        rounds = int(self.checkpoint.get("fix_rounds", 0))
        missing = self._unreferenced_acceptance()
        self._save(last_test=result.detail, unreferenced_acceptance=missing)
        if result.ok and not missing:
            return "publish"
        reason = "tests failed" if not result.ok else f"acceptance IDs without a test: {', '.join(missing)}"
        digest = _failure_digest(result.output)
        if rounds >= self.max_fix_rounds:
            raise PermanentFailure(f"fix budget exhausted after {rounds} rounds: {reason}\n{result.output[-3000:]}")
        if rounds and self.checkpoint.get("fix_completed") == rounds and digest == self.checkpoint.get("last_failure_digest"):
            raise PermanentFailure(f"stalled: identical failure after fix round {rounds}: {reason}\n{result.output[-3000:]}")
        self._save(fix_rounds=rounds + 1, last_failure_digest=digest)
        prompt = (
            f"The verification for task '{self.title}' did not pass: {reason}.\n"
            f"Spec: {self.spec_path.as_posix()}. Fix the implementation or tests so "
            f"`{' '.join(self.test_command)}` passes and every acceptance ID is referenced by a test. "
            "Do not weaken or delete acceptance criteria. Do not commit.\n\n"
            f"Output (tail):\n{result.output[-6000:]}"
        )
        self._codex(prompt, f"fix-{rounds + 1}")
        self._require_progress(f"fix-{rounds + 1}")
        self._save(fix_completed=rounds + 1)
        return "verify"

    def _stage_publish(self) -> str:
        self._guard_diff()
        scan = self._security_scan()
        if not scan.ok:
            raise PermanentFailure("security scan rejected the change:\n" + scan.output[-3000:])
        self._git("add", "-A")
        self._git("commit", "-q", "-m", f"feat({self.task_id}): {self.title}")
        commit = self._head()
        tests = self.checkpoint.get("last_test", {})
        evidence = (
            "\n## Evidence\n\n| Commit | Tests | Fix rounds | Verified by |\n|---|---|---|---|\n"
            f"| `{commit}` | {tests.get('ran', '?')} ran, {tests.get('status', '?')} | "
            f"{self.checkpoint.get('fix_rounds', 0)} | worker `{self.worker_id}` |\n"
        )
        spec_file = self.worktree / self.spec_path
        spec_file.write_text(spec_file.read_text(encoding="utf-8").rstrip() + "\n" + evidence, encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", f"docs({self.task_id}): record evidence for {commit}")
        self._save(commit=self._head(), spec=self.spec_path.as_posix())
        if self.settings.dev_push:
            self._git("push", "-u", "origin", self.branch)
            self._save(pushed=True)
        return "done"

    # --------------------------------------------------------------- guards
    def _require_progress(self, label: str) -> None:
        """A model stage must leave a diff behind (#5, and the $0 one-turn exits)."""
        status = self._git("status", "--porcelain").stdout.strip()
        if not status:
            raise RuntimeError(f"{label} stage changed nothing in the worktree")

    def _guard_diff(self) -> None:
        if self._acceptance_digest() != self.checkpoint.get("acceptance_digest"):
            raise PermanentFailure("acceptance table in the spec was modified after the spec stage")
        base = self.checkpoint.get("spec_commit", "HEAD")
        numstat = self._git("diff", "--numstat", base, "--").stdout
        for line in numstat.splitlines():
            added, deleted, path = line.split("\t", 2)
            path = path.strip()
            if self.allowed_files and not self._path_allowed(path):
                raise PermanentFailure(f"change outside the allowed file list: {path}")
            if deleted == "-":
                continue  # binary
            original = self._git("show", f"{base}:{path}", check=False)
            if original.returncode:
                continue  # new file
            lines = original.stdout.count("\n")
            if lines >= REWRITE_MIN_LINES and int(deleted) / max(lines, 1) > REWRITE_MAX_RATIO:
                raise PermanentFailure(f"wholesale rewrite of existing file rejected: {path}")
        untracked = [l[3:] for l in self._git("status", "--porcelain").stdout.splitlines() if l.startswith("??")]
        for path in untracked:
            if self.allowed_files and not self._path_allowed(path.replace("\\", "/")):
                raise PermanentFailure(f"new file outside the allowed file list: {path}")

    def _path_allowed(self, path: str) -> bool:
        if path == self.spec_path.as_posix() or path.startswith("tests/") or path.startswith("data/"):
            return True
        return any(path == f or path.startswith(f.rstrip("/") + "/") for f in self.allowed_files)

    def _acceptance_table(self) -> str:
        rows = "\n".join(f"| {aid} | {text} |" for aid, text in zip(self.acceptance_ids, self.acceptance))
        return f"| ID | Acceptance criterion |\n|---|---|\n{rows}"

    def _acceptance_digest(self) -> str:
        text = (self.worktree / self.spec_path).read_text(encoding="utf-8", errors="replace")
        rows = [line.strip() for line in text.splitlines()
                if any(line.strip().startswith(f"| {aid} ") for aid in self.acceptance_ids)]
        return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()

    def _unreferenced_acceptance(self) -> list[str]:
        tests_dir = self.worktree / "tests"
        corpus = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                           for p in tests_dir.rglob("*.py")) if tests_dir.exists() else ""
        return [aid for aid in self.acceptance_ids if aid not in corpus]

    # -------------------------------------------------------------- helpers
    def _ensure_worktree(self) -> None:
        self._run(["git", "worktree", "prune"], self.repo_root, 60)
        if (self.worktree / ".git").exists():
            return
        self.worktree.parent.mkdir(parents=True, exist_ok=True)
        exists = self._run(["git", "branch", "--list", self.branch], self.repo_root, 60).stdout.strip()
        args = ["git", "worktree", "add", "-q"]
        args += [str(self.worktree), self.branch] if exists else ["-b", self.branch, str(self.worktree), "HEAD"]
        completed = self._run(args, self.repo_root, 120)
        if completed.returncode:
            raise RuntimeError(completed.stderr or completed.stdout)

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        completed = self._run(["git", *args], self.worktree, 120)
        if check and completed.returncode:
            raise RuntimeError(f"git {' '.join(args)}: {completed.stderr or completed.stdout}")
        return completed

    def _head(self) -> str:
        return self._git("rev-parse", "--short", "HEAD").stdout.strip()

    def _run(self, command: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess:
        try:
            return self.runner(command, cwd, timeout)
        except subprocess.TimeoutExpired as exc:
            raise StageTimeout(f"{command[0]} exceeded {timeout}s") from exc

    def _main_checkout_state(self) -> set[str]:
        out = self._run(["git", "status", "--porcelain", "--", ".", ":!data"], self.repo_root, 60).stdout
        return {line[3:].strip() for line in out.splitlines() if line.strip()}

    def _codex(self, prompt: str, label: str) -> str:
        output = self.worktree / "data" / "codex" / f"{label}.txt"
        output.parent.mkdir(parents=True, exist_ok=True)
        if self.settings.dev_model == "manual":
            # An operator (or another agent) prepares the worktree by hand; the
            # worker still enforces every guard, verifies and publishes.
            self._save(**{f"manual_{label}": True})
            return output.read_text(encoding="utf-8", errors="replace") if output.exists() else ""
        command = ["codex", "exec", "-C", str(self.worktree), "--sandbox", self.settings.codex_sandbox,
                   "--output-last-message", str(output), prompt]
        before = self._main_checkout_state()
        started = time.monotonic()
        completed = self._run(command, self.worktree, self.settings.dev_stage_timeout_seconds)
        # Only paths that newly appear as modified/untracked count. Paths that
        # vanish were committed by someone else, which is not Codex's doing.
        appeared = self._main_checkout_state() - before
        if appeared:
            raise PermanentFailure(f"codex {label} modified the main checkout outside its worktree: "
                                   + ", ".join(sorted(appeared))[:500])
        text = completed.stdout + "\n" + completed.stderr
        lowered = text.lower()
        # Codex can print a usage-limit error and still exit 0, leaving no output.
        if any(marker in lowered for marker in USAGE_LIMIT_MARKERS) and (completed.returncode or not output.exists()):
            raise UsageLimited(f"codex {label}: backend throttled", retry_at=_retry_at(text))
        if completed.returncode:
            raise RuntimeError(f"codex {label} failed: {(completed.stderr or completed.stdout)[-2000:]}")
        self._save(**{f"seconds_{label}": round(time.monotonic() - started, 1)})
        return output.read_text(encoding="utf-8", errors="replace") if output.exists() else ""

    def _run_tests(self) -> StageResult:
        # Stale bytecode can mask an edit whose size and mtime second match the
        # previous version, producing a false verdict. Always verify from source.
        for cache in self.worktree.rglob("__pycache__"):
            for pyc in cache.glob("*.pyc"):
                pyc.unlink(missing_ok=True)
        completed = self._run(self.test_command, self.worktree, self.settings.dev_test_timeout_seconds)
        output = completed.stdout + "\n" + completed.stderr
        # Take the last summary line: nested test subprocesses print their own.
        counts = re.findall(r"Ran (\d+) tests?", output) or re.findall(r"(\d+) passed", output)
        return StageResult(completed.returncode == 0, output, {
            "returncode": completed.returncode, "ran": int(counts[-1]) if counts else None,
            "status": "OK" if completed.returncode == 0 else "FAILED"})

    def _security_scan(self) -> StageResult:
        script = self.worktree / "scripts" / "ci_security_scan.py"
        if not script.exists():
            return StageResult(True, "no scanner", {})
        completed = self._run(["python", str(script)], self.worktree, 300)
        return StageResult(completed.returncode == 0, completed.stdout + completed.stderr, {})
