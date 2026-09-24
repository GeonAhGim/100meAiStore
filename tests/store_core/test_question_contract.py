"""M5.5 · 부족정보 질문 계약 — 테스트.

검증 항목:
1. 필수 정보 부족 시 질문 레코드 생성 + 실행 금지
2. 모호한 해석 시 질문 레코드 생성 + 실행 금지
3. 질문 응답 시 ANSWERED 상태로 전환
4. 질문 만료 시 안전 조치(HOLD/TEMP_SUSPEND) 적용, 추측 실행 금지
5. 기한 지남 무응답 → check_expiry가 expired 목록 반환
6. 중복 응답 오류
7. 직렬화/역직렬화 round-trip
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from packages.store_core.question_contract import (
    DEFAULT_QUESTION_TTL_HOURS,
    QuestionAlreadyAnsweredError,
    QuestionContract,
    QuestionExpiredError,
    QuestionRecord,
    QuestionState,
    QuestionError,
    SafeAction,
)


class TestQuestionContract(unittest.TestCase):
    """질문 계약 기본 로직."""

    def setUp(self) -> None:
        self.contract = QuestionContract()
        self.now = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)
        self.qid = "q1"
        self.tid = "tenant-1"
        self.cid = "cmd-1"

    def _base_kwargs(self, **extra) -> dict:
        kw = {
            "question_id": self.qid,
            "tenant_id": self.tid,
            "command_id": self.cid,
            "missing_info": ["destination"],
            "impact": "배송 경로를 알 수 없습니다",
            "options": [
                {"index": 0, "label": "서울로 배송", "action": "ship_to_seoul"},
                {"index": 1, "label": "부산으로 배송", "action": "ship_to_busan"},
            ],
            "recommendation": "서울로 배송 권장",
            "safe_action": SafeAction.HOLD,
            "safe_details": {"reason": "default_hold"},
        }
        kw.update(extra)
        return kw

    # ------------------------------------------------------------------
    # Q1: 질문 생성
    # ------------------------------------------------------------------

    def test_ask_creates_record(self) -> None:
        record = self.contract.ask(**self._base_kwargs())
        self.assertEqual(record.question_id, self.qid)
        self.assertEqual(record.state, QuestionState.PENDING)
        self.assertIsNotNone(record.expires_at)

    def test_ask_sets_expiry(self) -> None:
        with patch(
            "packages.store_core.question_contract.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = self.now
            mock_dt.timezone = timezone
            mock_dt.timedelta = timedelta
            self.contract.ask(**self._base_kwargs(), ttl_hours=1)
            record = self.contract.get(self.qid)
            tolerance = timedelta(seconds=2)
            self.assertGreaterEqual(
                record.expires_at, self.now - timedelta(hours=1) - tolerance
            )
            self.assertLessEqual(
                record.expires_at, self.now + timedelta(hours=1) + tolerance
            )

    def test_ask_rejects_empty_missing_info(self) -> None:
        kw = self._base_kwargs()
        kw["missing_info"] = []
        with self.assertRaises(QuestionError):
            self.contract.ask(**kw)

    def test_ask_rejects_empty_options(self) -> None:
        kw = self._base_kwargs()
        kw["options"] = []
        with self.assertRaises(QuestionError):
            self.contract.ask(**kw)

    def test_ask_rejects_duplicate_id(self) -> None:
        self.contract.ask(**self._base_kwargs())
        with self.assertRaises(QuestionError):
            self.contract.ask(**self._base_kwargs())

    # ------------------------------------------------------------------
    # Q2: 조회
    # ------------------------------------------------------------------

    def test_get_returns_record(self) -> None:
        self.contract.ask(**self._base_kwargs())
        record = self.contract.get(self.qid)
        self.assertIsNotNone(record)
        self.assertEqual(record.question_id, self.qid)

    def test_get_missing_returns_none(self) -> None:
        self.assertIsNone(self.contract.get("no-such"))

    def test_pending_for_command_finds_pending(self) -> None:
        self.contract.ask(**self._base_kwargs())
        found = self.contract.pending_for_command(self.tid, self.cid)
        self.assertIsNotNone(found)
        self.assertEqual(found.state, QuestionState.PENDING)

    def test_pending_for_command_ignores_answered(self) -> None:
        self.contract.ask(**self._base_kwargs())
        self.contract.answer(self.qid, 0, "user-1")
        found = self.contract.pending_for_command(self.tid, self.cid)
        self.assertIsNone(found)

    # ------------------------------------------------------------------
    # Q3: 응답
    # ------------------------------------------------------------------

    def test_answer_sets_answered_state(self) -> None:
        self.contract.ask(**self._base_kwargs())
        record = self.contract.answer(self.qid, 0, "user-1")
        self.assertEqual(record.state, QuestionState.ANSWERED)
        self.assertIsNotNone(record.answered_at)
        self.assertIsNotNone(record.resolved_at)

    def test_answer_stores_choice(self) -> None:
        self.contract.ask(**self._base_kwargs())
        record = self.contract.answer(self.qid, 1, "user-1")
        self.assertEqual(record.safe_details["choice_index"], 1)
        self.assertEqual(record.safe_details["answerer"], "user-1")

    def test_answer_invalid_index_raises(self) -> None:
        self.contract.ask(**self._base_kwargs())
        with self.assertRaises(QuestionError):
            self.contract.answer(self.qid, 99, "user-1")

    def test_answer_negative_index_raises(self) -> None:
        self.contract.ask(**self._base_kwargs())
        with self.assertRaises(QuestionError):
            self.contract.answer(self.qid, -1, "user-1")

    def test_answer_missing_question_raises(self) -> None:
        with self.assertRaises(QuestionError):
            self.contract.answer("no-such", 0, "user-1")

    def test_answer_duplicate_raises(self) -> None:
        self.contract.ask(**self._base_kwargs())
        self.contract.answer(self.qid, 0, "user-1")
        with self.assertRaises(QuestionAlreadyAnsweredError):
            self.contract.answer(self.qid, 1, "user-2")

    # ------------------------------------------------------------------
    # Q4: 만료 → 안전 조치
    # ------------------------------------------------------------------

    def test_check_expiry_applies_hold(self) -> None:
        past = self.now - timedelta(hours=2)
        far_future = self.now + timedelta(hours=24)
        with patch(
            "packages.store_core.question_contract.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = self.now
            mock_dt.timezone = timezone
            mock_dt.timedelta = timedelta
            self.contract.ask(**self._base_kwargs(safe_action=SafeAction.HOLD))
            # manually set expires_at to past so check_expiry fires
            record = self.contract.get(self.qid)
            record.expires_at = past
        expired = self.contract.check_expiry(now=far_future)
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0].state, QuestionState.EXPIRED)
        self.assertEqual(expired[0].safe_details.get("action"), "hold")

    def test_check_expiry_applies_temp_suspend(self) -> None:
        with patch(
            "packages.store_core.question_contract.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = self.now
            mock_dt.timezone = timezone
            mock_dt.timedelta = timedelta
            self.contract.ask(
                **self._base_kwargs(safe_action=SafeAction.TEMP_SUSPEND),
            )
        record = self.contract.get(self.qid)
        record.expires_at = self.now - timedelta(hours=2)
        expired = self.contract.check_expiry(now=self.now + timedelta(hours=1))
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0].state, QuestionState.EXPIRED)
        self.assertEqual(expired[0].safe_details.get("action"), "temp_suspend")
        self.assertIn("suspend_until", expired[0].safe_details)

    def test_check_expiry_skips_pending_not_yet_expired(self) -> None:
        with patch(
            "packages.store_core.question_contract.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = self.now
            mock_dt.timezone = timezone
            mock_dt.timedelta = timedelta
            self.contract.ask(**self._base_kwargs())
        # expires_at is ~now + 24h, so 1h later it's still pending
        expired = self.contract.check_expiry(now=self.now + timedelta(hours=1))
        self.assertEqual(len(expired), 0)

    def test_check_expiry_skips_answered(self) -> None:
        with patch(
            "packages.store_core.question_contract.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = self.now
            mock_dt.timezone = timezone
            mock_dt.timedelta = timedelta
            self.contract.ask(**self._base_kwargs())
        self.contract.answer(self.qid, 0, "user-1")
        expired = self.contract.check_expiry(now=self.now + timedelta(hours=2))
        self.assertEqual(len(expired), 0)

    def test_check_expiry_no_questions_returns_empty(self) -> None:
        expired = self.contract.check_expiry(now=self.now)
        self.assertEqual(expired, [])

    def test_expire_one_immediate(self) -> None:
        with patch(
            "packages.store_core.question_contract.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = self.now
            mock_dt.timezone = timezone
            mock_dt.timedelta = timedelta
            self.contract.ask(**self._base_kwargs())
        record = self.contract.expire_one(self.qid, now=self.now)
        self.assertEqual(record.state, QuestionState.EXPIRED)
        self.assertEqual(record.safe_details.get("action"), "hold")

    def test_expire_one_non_pending_raises(self) -> None:
        with patch(
            "packages.store_core.question_contract.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = self.now
            mock_dt.timezone = timezone
            mock_dt.timedelta = timedelta
            self.contract.ask(**self._base_kwargs())
        self.contract.answer(self.qid, 0, "user-1")
        with self.assertRaises(QuestionError):
            self.contract.expire_one(self.qid, now=self.now)

    # ------------------------------------------------------------------
    # Q5: 실행 금지 판정
    # ------------------------------------------------------------------

    def test_must_not_execute_missing_info(self) -> None:
        self.assertTrue(
            QuestionContract.must_not_execute(["destination"], False)
        )

    def test_must_not_execute_ambiguous(self) -> None:
        self.assertTrue(
            QuestionContract.must_not_execute([], True)
        )

    def test_must_not_execute_neither_false(self) -> None:
        self.assertFalse(
            QuestionContract.must_not_execute([], False)
        )

    # ------------------------------------------------------------------
    # Q6: 필드 감지
    # ------------------------------------------------------------------

    def test_detect_missing_fields_finds_missing(self) -> None:
        payload = {"action": "ship", "quantity": 5}
        missing = QuestionContract.detect_missing_fields(
            payload, ["action", "destination", "quantity"]
        )
        self.assertEqual(missing, ["destination"])

    def test_detect_missing_fields_all_present(self) -> None:
        payload = {"a": 1, "b": 2}
        missing = QuestionContract.detect_missing_fields(payload, ["a", "b"])
        self.assertEqual(missing, [])

    # ------------------------------------------------------------------
    # Q7: 직렬화/역직렬화
    # ------------------------------------------------------------------

    def test_roundtrip_to_dict(self) -> None:
        self.contract.ask(**self._base_kwargs())
        record = self.contract.get(self.qid)
        data = record.to_dict()
        restored = QuestionRecord.from_dict(data)
        self.assertEqual(restored.question_id, self.qid)
        self.assertEqual(restored.tenant_id, self.tid)
        self.assertEqual(restored.command_id, self.cid)
        self.assertEqual(restored.missing_info, ["destination"])
        self.assertEqual(restored.safe_action, SafeAction.HOLD)
        self.assertEqual(restored.state, QuestionState.PENDING)

    def test_roundtrip_answered(self) -> None:
        self.contract.ask(**self._base_kwargs())
        self.contract.answer(self.qid, 0, "user-1")
        record = self.contract.get(self.qid)
        data = record.to_dict()
        restored = QuestionRecord.from_dict(data)
        self.assertEqual(restored.state, QuestionState.ANSWERED)
        self.assertIsNotNone(restored.answered_at)

    def test_roundtrip_expired(self) -> None:
        self.contract.ask(**self._base_kwargs(), ttl_hours=1)
        self.contract.expire_one(self.qid, now=self.now + timedelta(hours=2))
        record = self.contract.get(self.qid)
        data = record.to_dict()
        restored = QuestionRecord.from_dict(data)
        self.assertEqual(restored.state, QuestionState.EXPIRED)


if __name__ == "__main__":
    unittest.main()
