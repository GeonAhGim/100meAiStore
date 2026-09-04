# Phase 1 prototype assessment

## 결론

기존 `smart_store_aios`는 **운영 제품의 기반으로 승격하지 않고 격리된 참고 prototype으로 유지**한다. 재사용 대상은 순수 계산식의 아이디어와 lease 기반 worker 패턴뿐이며, 데이터베이스·큐·감사·정책·Codex 실행 코드는 새 아키텍처의 보안 및 거래 경계로 재사용하지 않는다.

이 판단은 `docs/100meAiStore-requirements-v1.md`와 `docs/architecture`의 D-01~D-11을 기준으로 했다. 현재 테스트 3개는 통과하지만, 단일 사용자 로컬 실험의 동작만 증명하며 LIVE 적합성을 증명하지 않는다.

## 현재 prototype 범위

| 구성요소 | 현재 역할 | 판정 |
|---|---|---|
| `profit.py` | 비율 비용을 뺀 주문당 공헌이익과 주간 필요 주문 수 계산 | 개념 재사용, 새 money 모델로 재작성 |
| `policy.py` | 상품 필드·재고·이미지 권리·단일 마진율 검사 | 규칙 아이디어만 재사용, 정책 엔진은 폐기 후 재작성 |
| `db.py` | SQLite job lease, 재시도/dead 상태, 단순 audit row | 로컬 DEMO 참고용으로만 격리 |
| `worker.py` | job kind 분기와 선택적 `codex exec` 실행 | 개발 worker 참고용으로 격리; 운영 agent에 사용 금지 |
| `cli.py` | prototype 초기화·enqueue·worker·상태·수익 계산 CLI | prototype 전용 유지 |
| `config.py` | JSON 기반 단일 전역 설정 | 제품 설정으로 재사용 금지 |

## 요구사항 및 아키텍처 불일치

### 멀티테넌시와 권한

- 모든 테이블과 명령에 `tenant_id`가 없고 `User`, `Membership`, 역할, 세션 회수 모델이 없다.
- 하나의 로컬 설정과 SQLite 파일이 모든 상태를 공유한다. 사업자별 자격증명·비용·데이터 격리를 보장할 수 없다.
- 마스터/자금·출납/상품·CS 권한, 사업자당 최대 3명, 한 명 승인 규칙을 표현할 수 없다.
- tenant context를 검증하는 API/DB 경계와 cross-tenant 부정 테스트가 없다.

### 승인과 거래 원장

- `Approval`, `Command`, `DomainEvent`, `Outbox`, 정책 버전, 승인 만료 및 실행 직전 재검사가 없다.
- 상품·발주·환불·공급처 교체·콘텐츠 변경의 분리 승인함과 일정이 없다.
- 외부 쓰기의 멱등성 키, unknown-result 상태, 검증/reconciliation 및 보상 흐름이 없다.
- `audit_log`는 append-only/hash-chain이 아니며 actor, tenant, correlation, policy, approval, 외부 결과가 없다. 일반 SQL로 수정·삭제 가능하다.
- job 완료와 audit 기록은 한 SQLite 트랜잭션이지만, 도메인 상태·event·outbox를 원자적으로 기록하는 거래 원장은 아니다.

### 데이터와 상태 모델

- 공급처 원본 snapshot, canonical product, channel projection과 lineage가 분리되어 있지 않다.
- 주문과 공급처 발주, 부분 라우팅·배송·취소·클레임·정산 상태기계가 없다.
- PostgreSQL 동시성, optimistic version, row-level tenant enforcement, durable outbox 기준을 충족하지 않는다.
- SQLite `BEGIN IMMEDIATE` lease는 단일 호스트 DEMO에는 유용하지만 Cloud Run 다중 인스턴스와 장기 workflow의 기준이 될 수 없다.
- `complete()`는 job의 현재 lease owner/state를 확인하지 않아 만료된 worker가 재임대한 job을 완료 처리할 수 있다. lease fencing token도 없다.
- 실패 시 worker가 예외를 다시 던져 상위 루프가 종료된다. 재시도 레코드는 남지만 계속 처리되는 worker 신뢰성은 없다.

### 수익·상품 정책

- 기본 단일 마진율 18%는 확정 기준인 광고 제외 15%, 광고 포함 10%, 실행 차단 10%를 구분하지 않는다.
- 상품당 예상 순이익 3,000원 하한, 채널별 수수료, 결제비, 예상 반품비, AI/콘텐츠비, 정책 버전이 없다.
- `other_variable_cost`로 일부 비용을 넣을 수 있으나 비용 근거와 snapshot이 보존되지 않는다.
- 이미지 사용권 미확인을 무조건 거절하지만 요구사항은 경고 후 사용자 승인 가능이며, 불법·명백한 상표권 침해만 강제 차단한다.
- 원산지·제조자 필드를 모든 카테고리에 일괄 적용하고, 사료/간식 필수 표시·유통기한·알레르기 정책을 구분하지 않는다.
- `Decision.approved`는 실제 신규상품 사용자 승인을 나타내지 않고 자동 통과를 뜻하므로 용어도 승인 모델과 충돌한다.

### 보안과 AI 경계

- 인증, MFA, Secret Manager/KMS, BYOK key reference, 사용량/예산 ledger가 없다.
- JSON 설정으로 sandbox 문자열과 Codex 실행 여부를 정하며 중앙 allowlist나 immutable guard가 없다.
- `codex.task` payload가 임의 prompt와 절대/상대 output 경로를 지정할 수 있고, 실행 timeout·출력 크기 제한·작업별 허용 도구가 없다.
- 운영 AI가 typed Tool Gateway를 통해 제한된 명령만 내린다는 보장이 없다. 따라서 이 runner를 운영 관리자 agent에 연결해서는 안 된다.
- subprocess stdout/stderr가 오류 메시지로 저장될 수 있어 향후 외부 입력/비밀이 포함되면 로그 노출 위험이 있다.

### 채널, UX와 운영

- 스마트스토어·쿠팡·공급처·알림·ChatGPT/n8n adapter contract가 구현되어 있지 않다.
- 현재 지원 job은 대부분 payload를 그대로 `accepted=True`로 반환하는 stub이다. 실제 검증이나 side effect 성공으로 해석하면 안 된다.
- PWA, DEMO/LIVE/SHADOW 모드, 긴급정지/복구, 비용 상한, 알림 fallback, 백업/restore가 없다.

## 재사용 경계

### 재사용 가능한 개념

1. `UnitEconomics`처럼 입력이 명시된 순수 함수로 계산을 격리하는 패턴.
2. 양수가 아닌 공헌이익에는 목표 주문 수를 산출하지 않는 안전 처리.
3. worker가 job을 claim하고 lease 만료 후 재처리하는 개념.
4. 재시도 횟수와 dead-letter 상태를 분리하는 개념.
5. 기본적으로 `dry_run`이고 Codex 실행이 비활성화된 개발 안전 기본값.

재사용 시 기존 모듈을 import하는 것이 아니라 새 도메인 타입·정수 minor-unit money·정책 snapshot·PostgreSQL/outbox 계약에 맞춰 테스트 우선으로 다시 구현한다.

### 격리 유지 대상

- `smart_store_aios` 전체 package와 `config.example.json`은 `legacy/prototype` 성격으로 취급한다.
- 기존 CLI와 SQLite DB는 계산 예시 및 로컬 worker 실험에만 사용한다.
- prototype job/audit 자료를 제품 DB로 migration하지 않는다.
- 기존 job kind 문자열은 표준 도메인 command/event 이름으로 간주하지 않는다.

### 폐기 대상

- SQLite schema를 운영 schema의 출발점으로 삼는 것.
- `accepted=True` stub을 adapter 성공으로 사용하는 것.
- `Decision.approved`를 사용자 승인으로 사용하는 것.
- 운영 시스템 안에서 범용 `codex exec` prompt runner를 실행하는 것.
- 단일 JSON 파일을 tenant 정책 또는 secret 저장소로 사용하는 것.

## 권장 전환 경로

1. 새 제품 코드를 별도 application/package 경계에 만들고 prototype import를 금지한다.
2. B01부터 PostgreSQL 기반 tenant, membership, session revocation, versioned policy, append-only audit 계약을 세운다.
3. DB 테스트는 서로 다른 두 tenant fixture로 시작하고 모든 repository method에 tenant context를 필수화한다.
4. `Command + Approval + DomainEvent + Outbox`의 단일 트랜잭션과 idempotency/fencing을 먼저 구현한다.
5. 기존 계산 예시를 새 `ProfitCalculation` value object 테스트 케이스로 옮기되, 광고 유무별 기준과 3,000원/10% guard를 분리한다.
6. DEMO supplier/channel adapter contract harness를 만든 뒤 주문 replay, timeout, unknown result, restart 복구를 검증한다.
7. 운영 AI는 범용 CLI runner가 아닌 typed Tool Gateway만 호출하게 하고 BYOK/예산/질문 프로토콜을 후속 수직 슬라이스에서 연결한다.
8. 기존 prototype은 새 기반이 수용기준을 통과할 때까지 비교 자료로 보존하고, 이후 별도 archive/삭제 결정으로 처리한다.

## Phase 1 수용기준

다음 조건이 모두 충족되어야 prototype 평가와 새 기반의 경계가 수용된 것으로 본다.

- [ ] 제품 코드가 `smart_store_aios`를 import하지 않으며 CI에서 dependency 경계를 검사한다.
- [ ] 모든 tenant-owned repository/API 호출은 명시적 tenant context 없이는 실패한다.
- [ ] 서로 다른 두 tenant의 동일 external key가 충돌하지 않고 교차 조회·변경이 거부되며 감사된다.
- [ ] 정책, command, approval, domain event, outbox가 PostgreSQL 트랜잭션 경계로 정의되고 schema migration으로 버전 관리된다.
- [ ] 승인 없는 신규상품·발주·환불·공급처 교체·판매중지·콘텐츠 변경 명령이 실행되지 않는다.
- [ ] 승인 후 입력/가격/재고/마진 변경 시 실행 직전 검사가 실패하고 재승인을 요구한다.
- [ ] 모든 외부 write가 idempotency key와 unknown-result reconciliation 경로를 가지며 replay에서 중복 side effect가 0건이다.
- [ ] 수익 계산은 예상 순이익 3,000원, 광고 제외 15%, 광고 포함 10%, 절대 실행 하한 10%를 독립적으로 검증한다.
- [ ] audit에는 tenant, actor, command, policy version, approval, correlation, 외부 검증 결과가 연결되며 application 경로로 수정/삭제할 수 없다.
- [ ] 운영 agent identity는 DB·vendor secret·범용 shell/Codex CLI에 접근하지 않고 typed Tool Gateway allowlist만 사용한다.
- [ ] worker 재시작·lease 만료·중복 delivery 테스트에서 job 유실과 stale worker 완료가 발생하지 않는다.
- [ ] DEMO stub 응답은 LIVE 성공으로 승격될 수 없고 모드가 UI/API/audit에 명시된다.
- [ ] secret/PII가 저장소, fixture, log, subprocess error에 포함되지 않는 자동 검사가 통과한다.

## 이번 평가의 검증 증거

- 명령: `python -m unittest discover -v`
- 결과: 3 tests passed.
- 검증 범위: 공헌이익 예시, 불완전 상품 거절, 단일 SQLite job의 중복 claim 방지.
- 미검증 범위: 멀티테넌시, 권한, 승인, PostgreSQL, adapter, idempotent external write, restart recovery, 보안, PWA 및 LIVE 운영 전체.

현재 테스트 성공은 prototype 보존 근거일 뿐 제품 구현 착수 게이트 통과를 뜻하지 않는다.
