"""M5.5 · 부족정보 질문 계약 — 질문 기록, 만료, 안전 조치.

Execution 요청에 필수 정보가 없거나 해석이 둘 이상이면 실행하지 않고
QuestionRecord를 생성한다. 기한까지 응답이 없으면 안전 조치(보류/임시정지)
만 적용하고 추측 실행을 하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping, Optional


class QuestionState(str, Enum):
    PENDING = "pending"
    ANSWERED = "answered"
    EXPIRED = "expired"


class SafeAction(str, Enum):
    HOLD = "hold"
    TEMP_SUSPEND = "temp_suspend"


@dataclass
class QuestionRecord:
    """M5.5 질문 레코드.

    실행 요청의 필수 정보가 없거나 해석이 둘 이상일 때 생성되며,
    기한까지 응답이 없으면 안전 조치가 적용된다.
    """
    question_id: str
    tenant_id: str
    command_id: str
    missing_info: list[str]
    impact: str
    options: list[dict[str, Any]]
    recommendation: str
    safe_action: SafeAction
    safe_details: dict[str, Any] = field(default_factory=dict)
    state: QuestionState = QuestionState.PENDING
    expires_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    answered_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        """JSON 직렬화 — gateway protocol 호환."""
        return {
            "question_id": self.question_id,
            "tenant_id": self.tenant_id,
            "command_id": self.command_id,
            "missing_info": self.missing_info,
            "impact": self.impact,
            "options": self.options,
            "recommendation": self.recommendation,
            "safe_action": self.safe_action.value,
            "safe_details": self.safe_details,
            "state": self.state.value,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "created_at": self.created_at.isoformat(),
            "answered_at": self.answered_at.isoformat() if self.answered_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QuestionRecord":
        """JSON 역직렬화."""
        state_map = {
            "pending": QuestionState.PENDING,
            "answered": QuestionState.ANSWERED,
            "expired": QuestionState.EXPIRED,
        }
        action_map = {
            "hold": SafeAction.HOLD,
            "temp_suspend": SafeAction.TEMP_SUSPEND,
        }
        return cls(
            question_id=data["question_id"],
            tenant_id=data["tenant_id"],
            command_id=data["command_id"],
            missing_info=data["missing_info"],
            impact=data["impact"],
            options=data["options"],
            recommendation=data["recommendation"],
            safe_action=action_map.get(data.get("safe_action", "hold"), SafeAction.HOLD),
            safe_details=data.get("safe_details", {}),
            state=state_map.get(data.get("state", "pending"), QuestionState.PENDING),
            expires_at=datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None,
            created_at=datetime.fromisoformat(data["created_at"]),
            answered_at=datetime.fromisoformat(data["answered_at"]) if data.get("answered_at") else None,
            resolved_at=datetime.fromisoformat(data["resolved_at"]) if data.get("resolved_at") else None,
        )


# 기본 만료 시간 (시간 단위)
DEFAULT_QUESTION_TTL_HOURS = 24


class QuestionError(Exception):
    """질문 계약 관련 오류."""


class QuestionExpiredError(QuestionError):
    """질문 기한이 지났으나 응답이 없음."""


class QuestionAlreadyAnsweredError(QuestionError):
    """이미 응답된 질문에 대한 중복 응답."""


class QuestionContract:
    """질문 생성·만료·안전 조치 로직.

    fail-closed: 정보가 부족하거나 모호하면 절대 추측으로 실행하지 않는다.
    """

    def __init__(self, questions: Optional[Mapping[str, QuestionRecord]] = None):
        self._store: dict[str, QuestionRecord] = dict(questions or {})

    # ------------------------------------------------------------------
    # 질문 생성
    # ------------------------------------------------------------------

    def ask(
        self,
        question_id: str,
        tenant_id: str,
        command_id: str,
        missing_info: list[str],
        impact: str,
        options: list[dict[str, Any]],
        recommendation: str,
        safe_action: SafeAction = SafeAction.HOLD,
        safe_details: Optional[dict[str, Any]] = None,
        ttl_hours: float = DEFAULT_QUESTION_TTL_HOURS,
    ) -> QuestionRecord:
        """필수 정보가 부족하거나 해석이 모호할 때 질문 레코드 생성.

        이미 같은 question_id가 있으면 중복 생성 오류.
        """
        if question_id in self._store:
            raise QuestionError(f"duplicate question_id: {question_id}")
        if not missing_info:
            raise QuestionError("missing_info must not be empty")
        if not options:
            raise QuestionError("options must list at least one choice")
        expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
        record = QuestionRecord(
            question_id=question_id,
            tenant_id=tenant_id,
            command_id=command_id,
            missing_info=missing_info,
            impact=impact,
            options=options,
            recommendation=recommendation,
            safe_action=safe_action,
            safe_details=safe_details or {},
            expires_at=expires_at,
        )
        self._store[question_id] = record
        return record

    # ------------------------------------------------------------------
    # 조회
    # ------------------------------------------------------------------

    def get(self, question_id: str) -> Optional[QuestionRecord]:
        return self._store.get(question_id)

    def pending_for_command(self, tenant_id: str, command_id: str) -> Optional[QuestionRecord]:
        """해당 command에 대기 중인 질문이 있으면 반환."""
        for q in self._store.values():
            if q.tenant_id == tenant_id and q.command_id == command_id and q.state == QuestionState.PENDING:
                return q
        return None

    # ------------------------------------------------------------------
    # 응답
    # ------------------------------------------------------------------

    def answer(
        self, question_id: str, choice_index: int, answerer_id: str
    ) -> QuestionRecord:
        """질문에 응답. choice_index는 options의 인덱스 (0-based)."""
        record = self._store.get(question_id)
        if not record:
            raise QuestionError(f"question not found: {question_id}")
        if record.state != QuestionState.PENDING:
            raise QuestionAlreadyAnsweredError(
                f"question {question_id} is {record.state.value}"
            )
        if choice_index < 0 or choice_index >= len(record.options):
            raise QuestionError(f"invalid choice_index {choice_index}")
        record.state = QuestionState.ANSWERED
        record.answered_at = datetime.now(timezone.utc)
        record.resolved_at = record.answered_at
        record.safe_details["answerer"] = answerer_id
        record.safe_details["choice_index"] = choice_index
        return record

    # ------------------------------------------------------------------
    # 만료 처리 — 안전 조치 적용
    # ------------------------------------------------------------------

    def check_expiry(
        self, now: Optional[datetime] = None
    ) -> list[QuestionRecord]:
        """만료된 질문을 찾아 안전 조치를 적용.

        반환값: 안전 조치가 적용된 질문 목록.
        추측 실행은 절대 하지 않는다.
        """
        now = now or datetime.now(timezone.utc)
        expired: list[QuestionRecord] = []
        for record in self._store.values():
            if record.state == QuestionState.PENDING and record.expires_at and record.expires_at <= now:
                self._apply_safe_action(record, now)
                expired.append(record)
        return expired

    def expire_one(self, question_id: str, now: Optional[datetime] = None) -> QuestionRecord:
        """단일 질문 즉시 만료."""
        record = self._store.get(question_id)
        if not record:
            raise QuestionError(f"question not found: {question_id}")
        if record.state != QuestionState.PENDING:
            raise QuestionError(f"question {question_id} is not pending")
        now = now or datetime.now(timezone.utc)
        self._apply_safe_action(record, now)
        return record

    # ------------------------------------------------------------------
    # 안전 조치 실행
    # ------------------------------------------------------------------

    def _apply_safe_action(self, record: QuestionRecord, now: datetime) -> None:
        """안전 조치를 적용 — 보류 또는 임시정지.

        추측 실행을 하지 않는다. 오직 정해진 안전 조치만.
        """
        record.state = QuestionState.EXPIRED
        record.resolved_at = now
        if record.safe_action == SafeAction.HOLD:
            record.safe_details.setdefault("action", "hold")
            record.safe_details.setdefault("reason", "question_expired_no_response")
        elif record.safe_action == SafeAction.TEMP_SUSPEND:
            record.safe_details.setdefault("action", "temp_suspend")
            record.safe_details.setdefault("reason", "question_expired_no_response")
            # 임시정지: 일시적 so expires_at를 7일 뒤로 재설정
            record.safe_details.setdefault("suspend_until", (now + timedelta(days=7)).isoformat())
        else:
            raise QuestionError(f"unknown safe_action: {record.safe_action}")

    # ------------------------------------------------------------------
    # 실행 금지 판정
    # ------------------------------------------------------------------

    @staticmethod
    def must_not_execute(missing_info: list[str], ambiguous: bool) -> bool:
        """필수 정보 부족 또는 해석 모호하면 실행 금지.

        fail-closed: 둘 다 거짓일 때만 False.
        """
        return bool(missing_info) or ambiguous

    @staticmethod
    def detect_missing_fields(
        payload: Mapping[str, Any], required: list[str]
    ) -> list[str]:
        """payload에서 누락된 필수 필드 목록 반환."""
        return [f for f in required if f not in payload]
