"""Doctor: diagnose every failed attempt before the task runs again.

The retry path used to hand the next attempt only the one-line note ("gate
failed: touched tests (1): rc=1"); the agent never saw that the test died on
``ImportError: cannot import name 'StoreService'`` and wrote the same import
again. The doctor is a worker of its own. For each task whose last attempt
failed and was not diagnosed yet it:

1. pulls the concrete error out of the note and the review artifact
   (exception lines, failing test ids, the file and line that raised);
2. runs cheap, exact probes against the current checkout (the names a module
   really defines, the modules a package really has, a function's real
   signature) and turns a hit into a concrete fix;
3. only when no probe explains the failure, asks the local model once for a
   short root cause and fix, and falls back to the loop guard's generic
   instruction when the model is busy.

The result is saved on the task as ``diagnosis`` and on the failed attempt as
``key``, a precise signature of the error. The next prompt carries the
diagnosis (agent_engine.task_prompt), claims wait until it exists
(``loopguard.awaiting_diagnosis``), and the loop guard compares keys, so "the
same failure" means the same error, not the same generic gate note. When a
task fails again with the key it was already diagnosed for, the fix did not
work and the doctor says so; the loop guard then hands the task off instead of
repeating it.
"""
from __future__ import annotations

import ast
import difflib
import json
import logging
import re
from pathlib import Path
from typing import Any

from . import loopguard
from .local_llm import LOCAL_LLM_GATE, complete
from .pm import _CLAIM_LOCK, _load, _save, now
from .state import CONTROL_DIR, ROOT, read_json

LOG = logging.getLogger(__name__)
EXCERPT_CHARS = 1800
LLM_TIMEOUT_SECONDS = 180
# causes a diagnosis cannot improve: the lane or the machine, not the patch
SKIP_CAUSES = {"orphaned", "infra"}

_ERROR_LINE = re.compile(r"^\s*((?:[\w.]+Error|[\w.]+Exception|AssertionError|Failed)\b[^\n]*)$", re.MULTILINE)
_FAILED_TEST = re.compile(r"^(?:FAIL|ERROR): (\S+) \(([\w.]+)\)", re.MULTILINE)
_RAISED_AT = re.compile(r'File "([^"]+)", line (\d+)')
_CANNOT_IMPORT = re.compile(r"cannot import name '(\w+)' from '([\w.]+)'")
_NO_MODULE = re.compile(r"No module named '([\w.]+)'")
_MODULE_ATTR = re.compile(r"module '([\w.]+)' has no attribute '(\w+)'")
_BAD_CALL = re.compile(r"(\w+)\(\) (?:got an unexpected keyword argument '(\w+)'|missing \d+ required \w+ argument)")


def failure_text(task: dict[str, Any]) -> str:
    """The failed attempt's own words: its note, then the review artifact's gate output."""
    # Claims and requeues overwrite note and last_error; the attempt keeps its own.
    attempt = (task.get("attempts") or [{}])[-1]
    parts = list(dict.fromkeys(str(v or "") for v in (attempt.get("note"), task.get("note"), task.get("last_error"))))
    review = task.get("review_artifact")
    if review and (attempt.get("cause") == "gate_tests" or any("gate failed" in p for p in parts)):
        try:
            parts.append(Path(str(review)).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return "\n".join(p for p in parts if p)


def _repo_path(path: str) -> str:
    """'C:\\...\\scratch\\review-20\\packages\\x.py' -> 'packages/x.py' (worktree prefixes removed)."""
    norm = path.replace("\\", "/")
    for anchor in ("/packages/", "/tests/", "/smart_store_control/", "/smart_store_aios/", "/scripts/"):
        if anchor in norm:
            return anchor.lstrip("/") + norm.split(anchor, 1)[1]
    return norm


def excerpt(text: str) -> dict[str, Any]:
    errors = list(dict.fromkeys(line.strip() for line in _ERROR_LINE.findall(text)))
    tests = sorted({f"{cls}.{name}" for name, cls in _FAILED_TEST.findall(text)})
    raised = [f"{_repo_path(f)}:{n}" for f, n in _RAISED_AT.findall(text)
              if "site-packages" not in f and "\\lib\\" not in f and "/lib/" not in f]
    return {"errors": errors[:6], "tests": tests[:10], "raised_at": list(dict.fromkeys(raised))[-3:]}


def _module_file(root: Path, dotted: str) -> Path | None:
    base = root.joinpath(*dotted.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def public_names(root: Path, dotted: str) -> list[str] | None:
    """Top-level classes, functions and constants a module defines, or None when it is not in the repo."""
    path = _module_file(root, dotted)
    if path is None:
        return None
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return None
    names = []
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            names += [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names += [(a.asname or a.name).split(".")[0] for a in node.names if a.name != "*"]
    return sorted({n for n in names if not n.startswith("_")})


def _signature(root: Path, name: str) -> str | None:
    """'def name(a, b, *, c=...)' for the first repo definition of ``name``."""
    for folder in ("packages", "smart_store_control", "smart_store_aios"):
        for path in sorted((root / folder).rglob("*.py")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if f"def {name}(" not in text:
                continue
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                    return f"{path.relative_to(root).as_posix()}: def {name}({ast.unparse(node.args)})"
    return None


def probe(text: str, root: Path = ROOT) -> dict[str, Any] | None:
    """An exact explanation of the failure from the current checkout, or None."""
    match = _CANNOT_IMPORT.search(text)
    if match:
        name, module = match.groups()
        names = public_names(root, module)
        if names is not None:
            close = ([n for n in names if n.lower() in name.lower() or name.lower() in n.lower()]
                     or difflib.get_close_matches(name, names, n=3, cutoff=0.6))
            return {"key": f"import:{module}.{name}",
                    "cause": f"`{module}`에는 `{name}`이 없다(존재하지 않는 이름을 import).",
                    "fix": f"`{name}`을 import하지 마라. `{module}`이 실제로 정의하는 이름은: {', '.join(names[:40])}."
                           + (f" 의도한 것은 아마 {', '.join(close)}." if close else "")
                           + " 필요한 것이 없으면 그 모듈을 먼저 읽고 기존 함수를 써라."}
    match = _NO_MODULE.search(text)
    if match:
        module = match.group(1)
        parent = module.rsplit(".", 1)[0] if "." in module else ""
        folder = root.joinpath(*parent.split(".")) if parent else root
        siblings = sorted(p.stem for p in folder.glob("*.py") if not p.stem.startswith("_")) if folder.is_dir() else []
        return {"key": f"nomodule:{module}",
                "cause": f"모듈 `{module}`이 저장소에 없다.",
                "fix": (f"`{module}`을 import하지 마라. `{parent}`에 있는 모듈은: {', '.join(siblings[:40])}."
                        if siblings else f"`{module}`을 import하지 마라. 새 모듈이 필요하면 그 파일을 이번 패치에서 만들어라.")}
    match = _MODULE_ATTR.search(text)
    if match:
        module, name = match.groups()
        names = public_names(root, module)
        if names is not None:
            return {"key": f"attr:{module}.{name}",
                    "cause": f"`{module}`에 속성 `{name}`이 없다.",
                    "fix": f"`{module}.{name}`을 쓰지 마라. 실제 이름은: {', '.join(names[:40])}."}
    match = _BAD_CALL.search(text)
    if match:
        func, keyword = match.groups()
        sig = _signature(root, func)
        if sig:
            return {"key": f"call:{func}:{keyword or 'missing'}",
                    "cause": f"`{func}` 호출 인자가 실제 시그니처와 다르다.",
                    "fix": f"실제 정의는 `{sig}` 이다. 이 시그니처대로 호출하고, 기존 함수의 시그니처는 바꾸지 마라."}
    return None


def _key_of(found: dict[str, Any]) -> str:
    """A precise failure signature: the first error line with ids and paths removed."""
    head = (found["errors"] or [""])[0]
    return loopguard.signature(head)[:100] if head else ""


def _ask_model(task: dict[str, Any], text: str, found: dict[str, Any]) -> dict[str, Any] | None:
    # With one reserved stream the gate is held for a whole agent run; a
    # diagnosis must not queue behind it (the rule fallback is used instead).
    if not LOCAL_LLM_GATE.acquire(blocking=False):
        return None
    LOCAL_LLM_GATE.release()
    endpoint = read_json(CONTROL_DIR / "runtime.json", {}).get("local_llm", {}).get("endpoint", "http://127.0.0.1:8081")
    prompt = ("smart_store 작업이 실패했다. 원인과 다음 시도가 할 조치를 한국어로 짧게 답하라. "
              'JSON 한 줄만 출력: {"cause": "근본 원인 한 문장", "fix": "다음 시도가 할 구체적 조치 1~3문장"}\n\n'
              f"## 작업\n{str(task.get('prompt') or '')[:1200]}\n\n"
              f"## 실패 요약\n{json.dumps(found, ensure_ascii=False)}\n\n## 실패 출력\n{text[-EXCERPT_CHARS:]}")
    try:
        reply = complete(prompt, endpoint=endpoint, model="qwen3.6-35b-a3b", timeout=LLM_TIMEOUT_SECONDS, max_tokens=700)
    except Exception as exc:  # noqa: BLE001 - diagnosis must never block the pool
        LOG.info("doctor: model unavailable for task %s: %s", task.get("id"), exc)
        return None
    match = re.search(r"\{.*\}", reply, re.DOTALL)
    try:
        parsed = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        parsed = {}
    if not parsed.get("fix"):
        return None
    return {"cause": str(parsed.get("cause") or "")[:400], "fix": str(parsed["fix"])[:800]}


def diagnose(task: dict[str, Any], root: Path = ROOT, *, use_model: bool = True) -> dict[str, Any]:
    text = failure_text(task)
    found = excerpt(text)
    attempt = (task.get("attempts") or [{}])[-1]
    cause = str(attempt.get("cause") or loopguard.cause_of(text))
    hit = probe(text, root)
    if hit:
        result = {**hit, "source": "probe"}
    else:
        asked = _ask_model(task, text, found) if use_model and cause not in ("stray_edits", "refusal") else None
        generic = loopguard.instruction_for(cause) or "실패 출력을 먼저 읽고, 그 오류를 없애는 가장 작은 변경을 만들어라."
        result = {"key": _key_of(found) or attempt.get("sig") or cause,
                  "cause": (asked or {}).get("cause") or f"{cause}: {(found['errors'] or [str(task.get('note') or '')])[0][:200]}",
                  "fix": (asked or {}).get("fix") or generic,
                  "source": "model" if asked else "rule"}
    previous = task.get("diagnosis") or {}
    # Same error as the one already diagnosed: the prescribed fix did not work.
    result["repeat"] = bool(previous.get("key")) and previous.get("key") == result["key"]
    result.update({"at": now(), "attempt_at": attempt.get("at"), "errors": found["errors"],
                   "tests": found["tests"], "raised_at": found["raised_at"]})
    return result


def pending(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [t for t in tasks if t.get("status") in ("blocked", "ready") and t.get("phase") != "needs_claude"
            and loopguard.needs_diagnosis(t, SKIP_CAUSES)]


def run_once(root: Path = ROOT, *, use_model: bool = True) -> dict[str, Any]:
    """Diagnose one task; the slow part (probes, model) runs outside the ledger lock."""
    with _CLAIM_LOCK:
        todo = pending(_load().get("tasks", []))
    if not todo:
        return {"status": "idle"}
    task = sorted(todo, key=lambda t: (-int(t.get("priority", 0)), int(t["id"])))[0]
    result = diagnose(task, root, use_model=use_model)
    with _CLAIM_LOCK:
        data = _load()
        for row in data.get("tasks", []):
            if row["id"] != task["id"]:
                continue
            attempts = row.get("attempts") or []
            if not attempts or attempts[-1].get("at") != result["attempt_at"]:
                return {"status": "stale", "task": task["id"]}  # failed again meanwhile; next run takes it
            attempts[-1]["key"] = result["key"]
            row["diagnosis"] = result
            row["updated_at"] = now()
            _save(data)
            return {"status": "diagnosed", "task": row["id"], "source": result["source"], "repeat": result["repeat"]}
    return {"status": "gone", "task": task["id"]}


def run_pending(root: Path = ROOT, limit: int = 10) -> list[dict[str, Any]]:
    done = []
    for _ in range(limit):
        result = run_once(root)
        if result["status"] == "idle":
            break
        done.append(result)
    return done
