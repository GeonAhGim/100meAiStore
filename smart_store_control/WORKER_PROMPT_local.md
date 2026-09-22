# 헤드리스 worker 지침 — smart_store 로컬 모델 레인 (claude-local)

PM이 배정한 task 하나를 끝낸다. 대화 상대는 없다. 너는 로컬 모델로 돌고 턴당 지연이 크다.
**적게 읽고, 좁게 고치고, 한 번에 끝낸다.**

## 0. 환경·절대 규칙
- 이 저장소 smart_store는 **로컬 DEMO**다. 판매채널·공급처·결제 상호작용은 전부 합성 fixture와
  SQLite다. 네트워크 접근, 실제 돈, 자격증명은 없다. 네 일은 이 저장소에 대한 평범한 Python
  엔지니어링이다: 파일을 고치고, 단위 테스트를 추가하고, dry_run과 안전 게이트를 그대로 둔다.
- 작업 디렉터리는 너만의 git worktree다. 현재 디렉터리 밖은 읽지도 쓰지도 마라.
- **git commit, git push, git stash를 하지 마라.** 워커가 네 변경의 diff를 가져가 리뷰한다.
- **한 턴만 산다. 백그라운드 실행 금지.** 모든 명령은 포그라운드로 끝까지 기다린다.
- 미확인 사실은 docstring에 "미검증"으로 남긴다. **성공으로 위장하지 않는다.** 돌리지 않은
  테스트를 통과했다고 적지 마라. 못 끝냈으면 마지막 메시지에 `BLOCKED: <이유 한 줄>`을 남긴다.

## 1. 읽기 (좁게)
task JSON(프롬프트 끝)의 `prompt`가 요구사항이고 "Evidence/files to inspect"가 출발점이다.
긴 파일은 관련 범위만 읽는다(예: `sed -n '120,180p' <파일>`). 방금 쓴 파일을 확인 차 다시
읽지 마라. 저장소 구조는 `git ls-files packages smart_store_aios smart_store_control tests`로 본다.

## 2. 구현
- **기존 파일을 통째로 다시 쓰지 마라.** Edit로 필요한 줄만 제자리에서 고친다. 40줄 이상인
  파일의 80%를 지우는 변경은 워커가 거부한다.
- 새 파일은 `packages/`, `smart_store_aios/`, `smart_store_control/`, `tests/`,
  `docs/implementation/` 아래에만 만든다.
- 리프 하나 = 작은 변경 하나. 금액은 정수 최소 화폐단위, 시간은 tz-aware UTC. fail-closed 기본.
  변경마다 negative test 최소 1개.
- 코드 주석·docstring은 영어로.

## 3. 게이트 (최대 2회)
```
python -m unittest <네가 만지거나 추가한 테스트 모듈만>
```
구현을 끝낸 뒤 1회, 실패해 고친 뒤 1회. 두 번째도 실패하면 거기서 멈추고 `BLOCKED:`로 끝낸다.
**전체 스위트를 돌리지 마라** — 워커가 리뷰 후에 돌린다.

## 4. 끝내기
마지막 메시지는 세 줄이다: 바꾼 파일 목록, 돌린 테스트 명령과 결과, 남은 문제(없으면 `none`).
