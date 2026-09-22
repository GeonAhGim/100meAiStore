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

자동 시작 해제는 `-Uninstall` 옵션을 사용한다.

대시보드는 상태 조회를 10초마다 갱신한다. 로컬 LLM 사용은 대시보드의 명시적 핸드오프 승인 뒤에만 가능하다.
