"""Loop guard: a task must not fail the same way forever.

Several paths requeue a task without spending its retry budget (transient
errors, a congested lane, a rebase, a holder that died with the server), so
the retry budget alone does not bound how often one task runs. Every failed
attempt is therefore recorded on the task under ``attempts`` with a cause and
a normalised signature of its note, whatever path requeues it afterwards.

The ten-minute triage asks :func:`verdict` about every blocked or ready task:

- no loop: leave it to the normal flow.
- first loop (the same signature ``REPEAT_LIMIT`` times in a row, or
  ``MAX_ATTEMPTS`` attempts): ``remediate``. The diagnosis becomes an
  instruction on the task's next prompt and the task gets one fresh budget.
- loop again after remediation, a cause no instruction can fix, or more than
  ``TIME_BUDGET_SECONDS`` of worker time: ``escalate``. The task is handed to
  Codex or Claude Code (see escalation.py) and the pool moves on.

Nothing here calls a model or touches the ledger; triage applies the verdict.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_ATTEMPTS = 6
REPEAT_LIMIT = 3
AFTER_REMEDY_ATTEMPTS = 3
TIME_BUDGET_SECONDS = 3 * 3600
HISTORY = 20
# How long a claim waits for the doctor (doctor.py) to diagnose a failed
# attempt. Past this the task runs undiagnosed rather than stalling the pool.
DIAGNOSIS_WAIT_SECONDS = 15 * 60

# (cause, markers in the lowered note, instruction for the next attempt or None when no instruction helps)
CAUSES: tuple[tuple[str, tuple[str, ...], str | None], ...] = (
    ("stray_edits", ("outside its worktree", "live checkout denied"),
     "이전 시도가 작업 worktree 밖(C:\\smart_store 본 체크아웃)의 파일을 수정했다. 현재 작업 디렉터리 안의 "
     "상대 경로 파일만 편집하고 절대 경로로 쓰지 마라."),
    # A run that changed nothing because it judged the work already done. Checked
    # before max_turns, whose "produced no change" marker it also carries.
    ("noop_claim", ("already complete", "already satisfied", "already implemented", "report-only patch",
                    "이미 완료", "이미 만족", "이미 충족", "이미 구현", "이미 완성"),
     "이전 시도는 완료조건이 이미 충족됐다고 판단하고 아무것도 바꾸지 않았다. 완료 주장은 증거로만 인정된다. "
     "완료조건을 검증하는 명령(감사 함수, 관련 테스트)을 직접 실행해 그 출력을 확인하고, 실패하는 항목을 고쳐라. "
     "정말 이미 충족된다면 그 사실을 검증하는 테스트를 추가하라. 저장소 루트에 보고서 파일만 추가하는 패치는 거부된다."),
    ("done_check", ("done check failed",),
     "이전 패치는 main에 병합됐지만 마일스톤 완료 확인 명령이 아직 실패한다. 피드백의 남은 항목 목록을 이어서 처리하고, "
     "제출 전에 그 확인 명령을 직접 실행해 줄어든 것을 확인하라."),
    ("max_turns", ("error_max_turns", "max turns", "produced no change"),
     "이전 시도가 턴을 다 쓸 때까지 파일을 하나도 바꾸지 못했다. 저장소 탐색을 줄이고, 완료조건 중 하나만 만족하는 "
     "가장 작은 변경을 처음 10턴 안에 파일로 써라. 나머지 완료조건은 다음 작업으로 남겨도 된다."),
    ("wall_clock", ("wall clock",),
     "이전 시도가 시간 제한을 넘겼다. 범위를 완료조건 하나로 줄이고 테스트는 관련 모듈만 실행하라."),
    ("patch_apply", ("does not apply", "patch not produced", "no longer applies", "base moved"),
     "이전 패치가 현재 HEAD에 적용되지 않았다. 현재 파일 내용을 다시 읽고 그 위에서 변경을 만들어라."),
    ("gate_tests", ("gate failed",),
     "이전 패치가 객관 게이트 테스트를 깨뜨렸다(피드백의 FAIL/ERROR 목록 참조). 기존 함수 시그니처와 "
     "호출부를 바꾸지 말고, 그 테스트들이 통과하도록 고친 뒤 전체 스위트를 확인하라."),
    ("refusal", ("refused",),
     "이 작업은 smart_store 저장소 안의 오프라인 코드·테스트 변경이며 외부 쓰기가 없다. 거절하지 말고 "
     "가장 작은 안전한 변경을 만들어라."),
    ("no_plan", ("plan named no",),
     "수정할 기존 파일이나 새로 만들 파일을 저장소 경로로 명시하라."),
    ("orphaned", ("holder process is gone", "supervisor requeued stale"), None),
    ("infra", ("local llm error", "review error", "timeouterror", "connection", "lane paused", "throttled",
               "deadline", "scratch worktree failed"), None),
)
UNFIXABLE = {"orphaned", "infra", "baseline_red"}
_NOISE = re.compile(r"[0-9a-f]{7,40}|\d+|[a-z]:\\[^\s]*|/[^\s]*\.py|\s+", re.IGNORECASE)
_FAILED_TEST = re.compile(r"^(?:FAIL|ERROR): (\S+) \(([\w.]+)\)", re.MULTILINE)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def signature(note: str) -> str:
    """The failure with its ids, counts, hashes and paths removed, so repeats compare equal."""
    return _NOISE.sub(" ", str(note or "").lower()).strip()[:120]


def cause_of(note: str) -> str:
    lowered = str(note or "").lower()
    for cause, markers, _ in CAUSES:
        if any(marker in lowered for marker in markers):
            return cause
    return "other"


def instruction_for(cause: str) -> str | None:
    return next((text for name, _, text in CAUSES if name == cause), None)


def record(task: dict[str, Any], note: str, kind: str, *, until: float | None = None, key: str | None = None) -> None:
    """Append one failed attempt to ``task['attempts']`` (in place; the caller saves).

    ``until`` ends the charged time early: an orphaned run stopped working at
    its last heartbeat, not when a restart found it hours later.
    """
    started = _ts(task.get("started_at"))
    last_at = _ts((task.get("attempts") or [{}])[-1].get("at")) if task.get("attempts") else None
    # Time is charged once per run: a review failure after an implementation
    # run does not count the implementation's minutes again.
    begin = max(filter(None, (started, last_at)), default=None)
    seconds = int((until if until is not None else _now().timestamp()) - begin) if begin else 0
    entry = {"at": _now().isoformat().replace("+00:00", "Z"), "kind": kind, "cause": cause_of(note),
             "sig": signature(note), "seconds": max(0, seconds),
             # the attempt's own words: claims and requeues overwrite note and last_error
             "note": str(note or "")[:400]}
    if key:
        entry["key"] = key  # a caller that knows the exact failure (e.g. the remaining gaps) names it
    task["attempts"] = ((task.get("attempts") or []) + [entry])[-HISTORY:]


def _key(attempt: dict[str, Any]) -> Any:
    """The doctor's precise error key when there is one, else the note's signature."""
    return attempt.get("key") or attempt.get("sig")


def needs_diagnosis(task: dict[str, Any], skip: set[str] | frozenset[str] = frozenset({"orphaned", "infra"})) -> bool:
    """The last attempt failed and the doctor has not diagnosed it yet."""
    attempts = task.get("attempts") or []
    if not attempts or attempts[-1].get("cause") in skip:
        return False
    return (task.get("diagnosis") or {}).get("attempt_at") != attempts[-1].get("at")


def awaiting_diagnosis(task: dict[str, Any]) -> bool:
    """Hold the next run until the doctor has spoken, but never longer than DIAGNOSIS_WAIT_SECONDS."""
    if not needs_diagnosis(task):
        return False
    at = _ts((task.get("attempts") or [{}])[-1].get("at"))
    return at is not None and _now().timestamp() - at < DIAGNOSIS_WAIT_SECONDS


def failing_tests(task: dict[str, Any]) -> set[str]:
    review = task.get("review_artifact")
    try:
        text = Path(str(review)).read_text(encoding="utf-8", errors="replace") if review else ""
    except OSError:
        return set()
    if "objective gate" not in text:
        return set()
    return {f"{cls}.{name}" for name, cls in _FAILED_TEST.findall(text) if "test_feature" not in cls}


def baseline_red(task: dict[str, Any], others: list[dict[str, Any]]) -> set[str]:
    """Tests that fail in this task's gate and in another task's gate too: the base, not the patch."""
    mine = failing_tests(task)
    if not mine:
        return set()
    shared: set[str] = set()
    for other in others:
        if other.get("id") != task.get("id") and cause_of(str(other.get("note") or "")) == "gate_tests":
            shared |= mine & failing_tests(other)
    return shared


def verdict(task: dict[str, Any], others: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """None, or {'action': 'remediate'|'escalate', 'cause', 'reason', 'instruction'} for this task."""
    reset = _ts(task.get("guard_reset_at")) if task.get("guard_reset_at") else None
    # An operator retry starts the guard over; earlier attempts stay as history.
    attempts = [a for a in task.get("attempts") or [] if reset is None or (_ts(a.get("at")) or 0) > reset]
    if not attempts:
        return None
    guard = task.get("loop_guard") or {}
    since = _ts(guard.get("at")) if guard else None
    recent = [a for a in attempts if since is None or (_ts(a.get("at")) or 0) > since]
    last = attempts[-1]
    cause = str(last.get("cause") or "other")
    spent = sum(int(a.get("seconds") or 0) for a in attempts)
    tail = [_key(a) for a in attempts[-REPEAT_LIMIT:]]
    repeated = len(tail) == REPEAT_LIMIT and len(set(tail)) == 1
    if spent > TIME_BUDGET_SECONDS:
        return {"action": "escalate", "cause": cause, "instruction": None,
                "reason": f"{spent // 60} minutes of worker time over {len(attempts)} attempts; the local pool is not efficient here"}
    diagnosis = task.get("diagnosis") or {}
    if guard and diagnosis.get("repeat") and diagnosis.get("attempt_at") == last.get("at") and len(recent) >= 1:
        # The doctor prescribed a fix for this exact error and it came back unchanged.
        return {"action": "escalate", "cause": cause, "instruction": None,
                "reason": f"the diagnosed fix did not work: the same error again after the remedy ({diagnosis.get('key')})"}
    if guard:
        same_again = len(recent) >= 2 and len({_key(a) for a in recent[-2:]}) == 1
        if same_again or len(recent) >= AFTER_REMEDY_ATTEMPTS:
            return {"action": "escalate", "cause": cause, "instruction": None,
                    "reason": f"still failing after the loop-guard remedy ({len(recent)} more attempts, last: {cause})"}
        return None
    if repeated or len(attempts) >= MAX_ATTEMPTS:
        why = (f"the same failure {REPEAT_LIMIT} times in a row" if repeated
               else f"{len(attempts)} attempts without success")
        # Judged only once the task loops: two fresh patches can break the same
        # test by coincidence, but a gate that keeps failing on tests another
        # task's gate also fails points at the base, which no retry here fixes.
        shared = baseline_red(task, others or []) if cause == "gate_tests" else set()
        if shared:
            return {"action": "escalate", "cause": "baseline_red", "instruction": None,
                    "reason": f"{why}; the same tests fail in other tasks' gates too, so HEAD or the environment "
                              "is red: " + ", ".join(sorted(shared))[:300]}
        diagnosis = task.get("diagnosis") or {}
        # The doctor's concrete fix beats the generic per-cause instruction.
        fresh = diagnosis.get("attempt_at") == last.get("at")
        instruction = (diagnosis.get("fix") if fresh else None) or instruction_for(cause)
        if cause in UNFIXABLE or not instruction:
            return {"action": "escalate", "cause": cause, "instruction": None,
                    "reason": f"{why}; cause '{cause}' is not something the implementer can fix"}
        return {"action": "remediate", "cause": cause, "instruction": instruction, "reason": why}
    return None


def summary(task: dict[str, Any]) -> str:
    attempts = task.get("attempts") or []
    if not attempts:
        return "no recorded attempts"
    causes: dict[str, int] = {}
    for a in attempts:
        causes[str(a.get("cause"))] = causes.get(str(a.get("cause")), 0) + 1
    minutes = sum(int(a.get("seconds") or 0) for a in attempts) // 60
    return f"{len(attempts)} attempts, {minutes} min: " + ", ".join(f"{k}×{v}" for k, v in causes.items())
