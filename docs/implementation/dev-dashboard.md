# Codex 개발 worker 관제

`smart_store_aios/dev_dashboard.py`는 판매 운영 화면과 분리된 로컬 개발
관제입니다. `~/.codex/sessions/**/rollout-*.jsonl`을 읽되 첫
`session_meta.cwd`가 프로젝트 루트와 정확히 일치하는 세션만 수집합니다.
사용자 원문 지시문, reasoning/raw content, tool arguments는 읽기 모델에
넣지 않습니다. assistant `AgentMessage`의 Text 블록만 redaction 후 최근
활동으로 보여 줍니다.

## 실행

```powershell
python -m smart_store_aios.dev_dashboard --project-root C:\smart_store --port 8767
```

`http://127.0.0.1:8767/`은 10초마다 `GET /api/dev-dashboard`를 호출합니다.
백그라운드 탭에서는 polling을 건너뛰고, 다시 활성화되면 즉시 조회합니다.
POST와 상태 변경은 지원하지 않습니다.

## 상태 계약

세션은 `codex_app_root`, `app_subagent_pm`, `app_subagent_worker`,
`codex_cli_worker`로 분류합니다. 실제 CLI 세션이 없으면
`cli_worker.state=execution_not_observed`로 표시합니다. 종료 이벤트가 없는
세션의 마지막 이벤트가 30초보다 오래되면 `signal_lost`; terminal error와
rate-limit 증거는 각각 `failed`와 `usage_limited`; 정상 종료는 `completed`로
표시합니다. 과거에 끝난 CLI 세션은 현재 실행으로 세지 않고
`historical_only`로 표시합니다.

파일 변경과 최근 커밋은 동일 프로젝트의 git read-only 조회입니다. 테스트는
Codex `CommandExecution`의 pytest/unittest 결과가 관측된 경우만 숫자를
표시하고, 없으면 `not_observed`입니다. 토큰은 session의
`token_usage_record`/`token_count`가 실제 존재할 때만 모델별 누적값을
표시하며, collector 자체는 LLM을 호출하지 않습니다.

이 화면은 로컬 개발 상태만 관측하며 Codex 프로세스를 시작하거나 가짜
heartbeat를 만들지 않습니다. 수집기 초기 스캔은 파일 변경 signature를
캐시해 10초 polling에서 변경 없는 JSONL을 재파싱하지 않습니다.
