# smart_store MVP 및 로컬 LLM 관제

## 프로젝트 경계

`C:\aios`는 별도 프로젝트이며 이 프로젝트에서 수정하거나 실행을 제어하지 않는다.
AIOS는 항상 우선순위가 높다. 이 문서와 `data/control/`은 `C:\smart_store`만 관리한다.

## 슬롯 정책

- AIOS 슬롯은 예약값이 아니라 실시간 read-only probe로 확인한다.
- `llama.cpp /props`, `/metrics`와 AIOS `claude-local` task 상태를 읽고 두 값 중 큰 값을 사용한다.
- 기본 상태: `handoff_granted=false`, local LLM 비활성, smart_store 슬롯 0개
- 운영자가 대시보드에서 핸드오프를 승인할 때만 현재 가용 슬롯 중 최대 1개를 활성화
- AIOS 파일은 읽기만 하며 자동으로 추정값을 쓰거나 AIOS를 제어하지 않는다
- 로컬 LLM 엔진만 사용하며 Codex 호출 경로를 추가하지 않는다

## MVP 마일스톤

M0 관제·슬롯 기반 → M1 로컬 DEMO 운영 플로우 → M2 운영 신뢰성 → M3 외부 연동 준비 순서다.
각 단계는 `data/control/milestones.json`의 완료 조건과 테스트 증거를 만족해야 다음 단계로 이동한다.

## 실행

```powershell
python -m smart_store_control.server
Start-Process http://127.0.0.1:8877/
```

Windows 로그온 시 자동 시작을 설치하려면 관리자 권한 없이 다음을 한 번 실행한다.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-control-dashboard-startup.ps1
```

자동 시작 해제는 `-Uninstall` 옵션을 사용한다. 설치 스크립트는 작업 스케줄러 작업
`SmartStore-ControlServer`를 등록해 로그온 시와 5분마다 `scripts/run-control-server-watchdog.ps1`을 창 없이
실행한다(이미 실행 중이면 뮤텍스로 즉시 종료). watchdog은 서버가 종료되면 다시 띄우고, watchdog 자체가
셸 재시작 등으로 죽어도 5분 안에 되살아난다. 서버 출력은 `data/control/server.log`, 종료 기록은
`data/control/server.exit`에 남는다.

## 안착 작업의 main 통합

게이트와 리뷰를 통과한 작업은 `control/task-<id>` 브랜치에 안착한다. autopilot은 한 번에 하나씩 이 브랜치를
임시 worktree에서 현재 main과 병합하고 전체 테스트를 돌린다. 통과하면 main을 fast-forward하고 마일스톤을
`done`으로 표시해 의존 마일스톤이 다음 작업을 만들 수 있게 한다(`smart_store_control/integrate.py`).
충돌이나 테스트 실패는 이유를 피드백으로 붙여 재구현 대기로 돌리고, main 체크아웃이 fast-forward를
거부하면(예: 운영자의 미커밋 수정) 10분 뒤 다시 시도한다. push는 하지 않는다.

## 반복 실패 방지(루프 가드)와 인수인계

로컬 풀은 일시 오류, 혼잡, 재베이스, 서버 재시작으로 인한 고아 작업을 재시도 예산 없이 다시 큐에 넣는다.
이것만으로는 한 작업이 같은 방식으로 끝없이 실패할 수 있으므로, 실패와 고아 발생마다 원인과 정규화된
실패 서명을 작업의 `attempts`에 기록하고 10분 주기 triage가 `smart_store_control/loopguard.py`로 판정한다.

- 같은 서명 3회 연속 또는 시도 6회: 원인별 진단을 다음 프롬프트의 지시로 붙이고 한 번 더 기회를 준다.
- 진단 뒤에도 같은 실패 2회 또는 시도 3회, 누적 작업 시간 3시간 초과: 로컬 풀에서 빼서 넘긴다.
  코드 원인은 Codex `dev.task`로, 고아·인프라·기준선 실패(다른 작업의 게이트에서도 같은 테스트가 실패)는
  Claude Code(`needs_claude`)로 넘긴다. 풀은 다음 작업을 계속 진행한다.
- triage는 매번 `data/control/handoff.md`를 다시 써서 Codex·Claude Code가 원장 없이도 원인, 시도 이력,
  산출물, 원래 지시를 보고 이어받을 수 있게 한다.

### 진단 워커(doctor)

실패한 시도는 다시 실행되기 전에 진단 워커(`smart_store_control/doctor.py`)가 먼저 본다. 실패 노트와 리뷰 게이트 출력에서
실제 예외·실패 테스트·발생 위치를 뽑고, 현재 체크아웃을 직접 조사해(모듈이 실제로 정의하는 이름, 패키지의 실제 모듈,
함수의 실제 시그니처) 구체적 조치를 만든다. 조사로 설명되지 않으면 로컬 모델에 한 번 묻고, 모델이 바쁘면 원인별 기본
지시를 쓴다. 진단은 작업의 `diagnosis`에 저장되어 다음 프롬프트의 "이전 시도 실패 진단" 절로 들어가고, 대시보드 카드와
`handoff.md`에도 보인다. 작업은 진단이 나올 때까지(최대 15분) 다시 claim되지 않는다.

진단은 오류의 정확한 키(예: `import:packages.store_core.service.StoreService`)도 남긴다. 루프 가드는 일반 게이트 노트가
아니라 이 키로 "같은 실패"를 판단하고, 진단된 조치 뒤에도 같은 키로 실패하면 반복하지 않고 바로 넘긴다. cursor·gemini
레인의 할당량·로그인 오류는 레인의 문제이므로 작업의 실패로 기록하지 않고 작업을 대기열로 돌려보낸다.

대시보드는 상태 조회를 10초마다 갱신한다. 로컬 LLM 사용은 대시보드의 명시적 핸드오프 승인 뒤에만 가능하다.
