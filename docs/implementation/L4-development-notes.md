# 100meAiStore L4 개발노트

기준: 2026-09-05, Git `1ad163b`. 제품 아키텍처는 `../architecture/README.md`, 리테일 비교 근거는 `../research/retail-grade-benchmark-2026-09-04.md`를 참조한다.

## 1. 범위와 완료 판정

L1 제품 → L2 업무 영역 → L3 기능 → L4 구현 단위로 분해한다. L4 단위는 입력/출력, 데이터, 상태전이, 권한, 실패 처리, 테스트, 완료 증거가 있어야 한다. 문서 작성, 구현, 테스트 통과, 커밋은 서로 다른 상태다. 실제 실행 증거 없이 워커 실행 중 또는 기능 완료로 표시하지 않는다.

현재 확인된 기반은 tenant/RBAC/승인 도메인, SQLite v1–v5, 감사/outbox 복구와 개발 관제다. 실제 이메일 로그인, PostgreSQL RLS, 판매채널/공급처 운영 연동은 완료되지 않았다. inbox는 모델과 v4 테이블이 있으나 업무 서비스와 저장소 처리가 완성됐다고 간주하지 않는다. 기존 `tests/test_dev_dashboard.py` 미커밋 변경은 별도 작업 소유로 보존한다.

모든 시간은 UTC 저장/Asia-Seoul 표시, 금액은 통화+최소 화폐단위 정수, 비율은 Decimal로 계산한다. 모든 업무 키는 tenant 범위를 포함한다. 승인 정책과 일정은 버전이 있는 설정으로 분리한다. 과거 답변의 시간표 불일치는 숨기지 않고 설정 화면에서 확인할 수 있게 한다. 24시간 승인 만료, 발주 전 사람 승인, 최소 순마진 10%는 기존 요구를 따른다. 스케줄만으로 결제/발주/등록을 승인 처리하지 않는다.

## 2. 공통 구현 계약

- 입력: 인증된 TenantContext 또는 별도 명시적 ServicePrincipal, correlation_id, idempotency_key, expected_version. tenant_id를 요청 본문만으로 신뢰하지 않는다.
- 응답: entity_id, version, state, replayed, correlation_id. 입력 오류 422, 인증 401, 권한 403, 동일 tenant 내 충돌 409. 다른 tenant 객체의 존재를 노출하지 않는다.
- 트랜잭션: 상태 갱신+감사+outbox는 한 번에 커밋. 외부 네트워크 호출은 DB 트랜잭션 밖. 전송 결과 불명은 UNKNOWN으로 보존한다.
- 재시도: 읽기/확실한 미전송만 제한된 backoff. 외부 쓰기 응답 유실은 조회 대사 전 재전송 금지. lease 만료 후에도 fencing token이 일치해야 완료된다.
- 비밀정보: 키/원문 주문 개인정보/원본 CLI 지시문을 Git, 감사 metadata, 로그, 개발 관제에 기록하지 않는다. 테스트는 합성 데이터만 사용한다.
- 완료: 문서와 코드 계약 일치, 정상·거부·중복·충돌·동시성·재시작 테스트, migration 보존/rollback, 비밀정보 검사, 작은 커밋과 테스트 증거 연결.
- 로컬 구현 및 합성 adapter 테스트부터 진행한다. LIVE 계정 연결·실제 주문/결제/발주·유료 사용·배포는 별도 승인 경계다.

## 3. L4 작업 패키지

### DEV-01 개발 작업 추적

- 데이터: task_id, parent_id, role, execution_source(app/cli), model, acceptance_ids, state, observed_at, latest_commit, test_evidence, blocker. 실제 Codex 세션 로그와 명시적 작업 패킷을 연결한다.
- 상태: planned → assigned → running → verifying → completed 또는 blocked/failed. 마지막 로그 지연은 별도 freshness로 표시하고 완료/실패로 추정하지 않는다.
- 조회: GET /api/dev-dashboard, 10초 polling, AI 호출 0. 진행률은 완료된 수용기준 수/버전 고정된 총 기준 수; 분모가 없으면 미확인.
- 실패/테스트: 중복 rollout 병합, 다른 cwd 제외, 재개 후 과거 blocker 해제, CLI/앱 구분, 개인정보 redaction, 오래된 신호와 재시작 복원. 현재 관제는 기반 완료이며 작업 패킷별 진척 연결은 후속이다.

### CORE-01 durable inbox (첫 구현)

- 상세는 아래 4절. 모델/테이블의 존재와 처리 기능의 완료를 구분한다.
- 선행: SQLite UoW, tenant FK, outbox와 감사. 완료 증거: IN-01–IN-10 테스트.

### CORE-02 승인 의도 고정 및 실행 직전 검사

- 데이터: ApprovalIntent(command_id, tenant_id, canonical_digest, policy_version, target_version, amount, currency, supplier_ref, evidence_hashes, expires_at); ExecutionAttempt(id, command_id, intent_digest, state, fencing_token).
- 명령: request_approval → approve/reject → prepare_execution. 가격/재고/공급처/수량/정책 버전이 바뀌면 이전 digest로 실행 불가, 새 승인 요청.
- 권한: 승인 종류별 capability 재확인. 승인자 멤버십 회수·만료·다른 tenant·미승인 요청은 실행 거부. 1인 유효 승인으로 충분하되 동일 승인 중복 클릭은 멱등.
- 실패: 승인 후 취소/정지와 실행 경쟁은 version/CAS로 하나만 성공. DB intent 준비와 네트워크 호출 사이도 executor에서 다시 검사.
- 테스트: digest 변경, 만료 경계, 권한 회수, 동시 approve/execute, 재실행, 승인 우회, 감사 append 실패 rollback. 승인 없이 외부 adapter가 호출되면 실패.

### CORE-03 UNKNOWN 실행 및 대사

- 데이터: attempt_id, tenant_id, operation_key, adapter_version, provider_reference, request_digest, state, verified_at, next_check_at. operation_key는 논리 작업당 하나.
- 상태: PREPARED → SENT → VERIFIED_SUCCESS/VERIFIED_FAILURE/UNKNOWN; UNKNOWN → RECONCILING → verified 또는 MANUAL_REVIEW.
- 계약: adapter.execute(intent), adapter.lookup(operation_key). 조회 결과는 FOUND/ABSENT/INCONCLUSIVE이며 ABSENT도 provider 일관성 보장 조건을 통과해야 재시도 가능.
- 실패: 전송 직후 프로세스 종료, timeout, 응답 성공 후 DB 커밋 유실은 UNKNOWN으로 복구한다. UNKNOWN을 단순 실패로 치환하지 않는다.
- 테스트: timeout 뒤 실제 성공, 아직 조회되지 않는 성공, 같은 키 동시 전송, 다른 tenant 조회, 부분 성공. 동일 주문에 외부 효과가 2번 발생하면 실패.

### ADAPTER-01 채널/공급처 계약 및 DEMO

- 데이터: Connection(tenant_id, provider, mode, secret_ref, adapter_version, capabilities, cursor, status). secret 자체는 별도 저장소.
- 인터페이스: list_changes(cursor, overlap), get_order, publish_product, get_listing, place_po, lookup_operation, read_tracking; 미지원 capability는 명확한 오류.
- 스마트스토어/쿠팡은 공식 문서 확인 전 webhook 지원을 가정하지 않는다. pagination/겹침조회/주기 대사와 durable inbox를 연결한다.
- 테스트: pagination 경계, 중복/역순, rate limit, 권한 만료, schema 변경, 부분 성공, 조회 지연. DEMO fixture와 실제 adapter 동일 계약 시험 사용.
- 미확정 provider 접근권한·API 제한은 capability 확인 항목으로 남기며 LIVE 완료로 표시하지 않는다.

### CATALOG-01 공급처 파일 수집 및 정규화

- 데이터: Supplier, FeedBatch, SourceProduct, CanonicalProduct, Offer; source_digest와 필드별 provenance/observed_at 보존.
- 입력: CSV/Excel/XML/API/manual. 인코딩·열 매핑·단위·통화·옵션은 명시적 설정. XML 외부 엔티티 금지, 파일 크기/행수 제한, 셀 수식 실행 금지.
- 상태: RECEIVED → VALIDATED → NORMALIZED → CANDIDATE/NEEDS_INFORMATION/BLOCKED. 일부 행 오류는 오류 위치와 재처리 키를 남긴다.
- 테스트: 빈 행/잘못된 인코딩/중복 옵션/가격 누락/XXE/같은 파일 재입력/다른 tenant 피드. 모호한 상품 동일성은 자동 병합하지 않는다.

### CATALOG-02 수익 기반 후보 및 공급처 대체

- 데이터: CostBreakdown(supply, delivery, fees, ads, returns, allocated_ai, tax_assumption), MarginSnapshot, CandidateScore, AlternativeEvidence.
- 계약: calculate_margin(inputs, policy_version) → projected_profit, margin, missing_inputs, rationale. 미확인 비용을 0으로 숨기지 않는다.
- 후보 약 300/승인 약 50은 강제 채우기 수가 아니다. 수익성/품질/공급처 증거 기준. 대체는 모델·브랜드·규격·품질·공식 유통 증거가 일치해야 제안.
- 테스트: rounding, 배송 합산, 부분취소, 원가 변동, 10% 경계, 증거 미충족 대체 거부. 실제 순수익과 추정 공헌이익을 분리 표시.

### CONTENT-01 이미지·글·영상 승인 파이프라인

- 데이터: Asset(original_ref, rights_evidence, sha256), ContentVersion, TransformJob, ReviewDecision, PublishedRevision.
- 상태: DRAFT → GENERATED/EDITED → REVIEW_PENDING → APPROVED → PUBLISHED. 편집하면 승인 digest 갱신, 원본 보존. 영상은 필요성이 확인된 상품만 후보화.
- 계약: 미디어 connector는 자산 생성만, 채널 게시는 typed gateway만. 한국어/영문 번역 키 및 사용자 편집 지원.
- 테스트: 저작권 증거 누락, 사실과 다른 이미지/효능 문구, 수정 후 승인 재사용, asset 만료, 업로드 부분 실패. 명백한 금지사항은 사용자 승인으로 우회 불가.

### ORDER-01 주문 수집·공급처 발주·배송

- 데이터: ChannelOrder, OrderLine, SupplierPO, POLine, Shipment. 채널 주문키와 공급처 발주키 분리; 1주문 여러 PO 허용.
- 상태: 수집/검증 → PO 승인 대기 → 사람 승인 → 발주 결과 확인 → 송장 → 배송 확인. 결제증빙은 별도 기록, 카드 자동결제 없음.
- API: ingest_order, propose_routing, request_po_approval, attach_payment_evidence, reconcile_po, ingest_tracking.
- 테스트: 중복 주문, 부분 발주/취소, 승인 만료, 취소와 발주 경쟁, 운송장 중복/정정, 공급처 응답 유실. 이미 성공한 PO를 다른 PO 실패 때문에 되돌리지 않는다.

### CLAIM-01 CS·클레임·환불

- 데이터: Case, CustomerMessage, DraftReply, Claim, RefundIntent; 채널/고객/공급처 상태를 독립 보존.
- 흐름: 접수 → AI/템플릿 초안 → 사용자 편집/승인 → 전송 확인. 환불 약속·민감 응답·환불 실행은 승인 필수.
- 테스트: 동일 티켓 반복수신, 환불 성공 응답 유실, 주문 불일치, 권한 없는 응답, 승인 후 초안 수정. 배송 등 사실 조회 응답 허용 범위도 정책 버전으로 확인.

### FINANCE-01 정산 및 실현손익

- 데이터: SettlementBatch, SettlementLine, Match, Adjustment, RealizedProfit; 원본 행 digest와 수정 이력 보존.
- 흐름: CSV/Excel 입력 → 금액 검증 → 주문/PO 대사 → 불일치 승인 → 확정. 추정 마진을 덮어쓰지 않는다.
- 테스트: 중복 파일, 반품/수수료 후속 차감, 주문 분할 정산, 통화 불일치, 누락 비용, 사용자 조정 취소. 증빙 없는 수익을 확정으로 표시하지 않는다.

### AI-01 BYOK·관리자 판단·예산

- 데이터: SecretReference, BudgetReservation, UsageRecord, AgentRun, ToolProposal; 실제 키는 secret backend.
- 흐름: 정해진 시간/이벤트에 규칙 판정 → 예산 예약 → 모델 호출 → 구조 검증 → 승인안 → 승인된 gateway 실행. 화면 새로고침은 모델 호출하지 않는다.
- 모델: 직접 선택/절약/균형/고품질. 사용량 소진 시 AI 중단, 규칙형 주문 감시/승인 조회 유지. 재설정은 플랫폼에 저장된 키 연결 교체이며 OpenAI 원격 키 삭제로 오해하지 않는다.
- 테스트: 동시 예산 예약, key invalid/quota 구분, timeout 비용 미확정, prompt injection/tool 권한 우회, tenant 키 격리. 실제 SDK 선택 직전 공식 문서 재검증.

### UX-01 로그인·모바일 승인·설정

- 데이터: LoginChallenge, Session, Membership, ApprovalView, UserPreference. 이메일 코드 expiry/시도 제한/단회 사용; 브라우저 세션은 안전한 쿠키.
- 화면: 오늘 할 일, 상품/발주/CS 승인 분리, 제안 근거/금액/유효기한, 편집/승인/거절, 감사 상세. 사업자 master가 멤버 권한 관리.
- API: challenge/verify/session/revoke; approvals query/decide; settings read/update(expected_version).
- 테스트: OTP 재사용/bruteforce, tenant 전환, 중복 클릭, 오프라인 만료, 세션 회수, 모바일 접근성. 한국어/영문 i18n, Android PWA 우선.

### NOTIFY-01 알림 및 중단·복구

- 데이터: NotificationIntent, DeliveryAttempt, Acknowledgement, StopScope, RecoveryPlan. 건별 mute와 사용자 확인 상태를 분리.
- 흐름: outbox → 앱 푸시 → 설정된 지연 후 이메일 fallback; ChatGPT는 지원 가능 여부 검증한 선택 connector. 알림 누락이 승인으로 이어지지 않는다.
- 복구: 전체/사업자/채널/공급처/상품 정지 범위 표시 → dry-run 계획 → 사용자 승인 → 멱등 재개. 완료된 발주 재실행 금지.
- 테스트: 중복 전송, 수신확인 경쟁, mute 예외, 장애 후 적체, 부분복구, backup restore와 승인/주문 상태 일치.

### PROD-01 PostgreSQL·배포 준비

- SQLite는 로컬 DEMO. PostgreSQL migration과 RLS, least-privilege role, connector credential 분리, encrypted backup/restore, health/readiness, 작업 lease를 로컬 통합시험한다.
- 비용: 월 3만원 한도는 목표/제어 규칙이며 클라우드 청구 차단을 보장한다고 표현하지 않는다. DB/저장소/네트워크 최소비용을 배포 전 견적 검증.
- gate: 사업자/채널 인증, 공급처 정보, 개인정보 처리/보존 정책, 실제 adapter 계약시험, 장애 복구 증거. 준비 완료와 실제 LIVE 전환은 별도.

## 4. CORE-01 구현 명세 및 수용기준

### 데이터 및 저장소

기존 InboxMessage/v4 스키마를 우선 재사용한다: id, tenant_id, provider, connection_id, external_event_id, schema_version, received_at, payload_digest, raw_payload_ref, state, version, processed_at. 유일키는 (tenant_id, provider, connection_id, external_event_id). payload_digest는 64자리 SHA-256 lowercase hex. raw_payload_ref는 임의 URL/파일경로 대신 선택적인 비밀정보 없는 opaque reference로 제한한다. 원문 payload와 고객 개인정보는 저장하지 않는다.

저장소 계약은 receive_inbox(message), get_inbox(tenant_id,id), inbox_for(tenant_id), mark_inbox_processed(tenant_id,id,expected_version,processed_at)로 한다. 반환 객체는 변경해도 저장 상태를 바꾸지 못하도록 복사한다. SQLite와 in-memory DEMO가 같은 계약을 지킨다.

서비스 계약은 register_adapter_manifest(context, manifest), receive_inbound(context, provider, connection_id, external_event_id, schema_version, payload_digest, raw_payload_ref=None), process_inbound(context, inbox_id, expected_version)다. 첫 구현은 master 전용 내부 서비스 계약으로 제한한다. 외부 connector용 ServicePrincipal 인증 전에는 HTTP 수신을 열지 않는다.

manifest는 tenant/provider/connection/adapter_version/capabilities/inbound_schema_versions를 고정하며 INBOUND_EVENTS capability와 schema 지원을 수신 전에 검사한다. 없는 연결/잘못된 schema는 수신 및 outbox 생성 없이 거부한다.

### 트랜잭션 및 결과

1. context membership/권한 검증 → 입력 형식/manifest 검사.
2. UoW 시작 → 유일키 조회. 같은 digest+schema면 기존 id, replayed=true; 내용 다르면 ConflictError. 기존 데이터는 덮어쓰지 않는다.
3. 신규 수신 row, 안전한 감사 이벤트, topic=inbox.process_requested/key=inbox:{id}:process의 outbox를 원자 저장한다. 원문을 audit/outbox metadata에 넣지 않는다. 이 예약이 있어야 재시작 뒤에도 미처리 수신을 발견할 수 있다.
4. 처리: UoW 시작 → tenant-scoped row → state/version 확인. PROCESSED 재호출은 멱등 성공이며 새 outbox를 생성하지 않는다.
5. RECEIVED를 CAS로 PROCESSED/version+1/processed_at 변경 → topic=inbound.accepted, key=inbox:{id}:processed의 outbox 생성 → 감사 append → commit. 여기서 accepted는 내부 후속 처리를 예약했다는 뜻이며 실제 주문/발주 완료가 아니다.
6. 이후 domain router가 inbox id와 digest를 근거로 canonical 이벤트 처리. 외부 네트워크는 이 트랜잭션에서 호출하지 않는다. digest만으로 실제 주문 내용을 복원할 수 없으므로 신뢰된 immutable normalized payload 저장·조회가 마련되기 전에는 PROCESSED를 실제 주문 처리 완료라고 표현하지 않는다.

### 테스트 ID

| ID | 입력/실패 상황 | 기대 결과 |
|---|---|---|
| IN-01 | 유효 manifest+신규 이벤트 | RECEIVED 1행, audit 1건, process_requested outbox 1건 |
| IN-02 | 같은 키/같은 digest 반복 및 재시작 후 반복 | 동일 id, replayed=true, 중복 audit/outbox 없음 |
| IN-03 | 같은 키/다른 digest 또는 schema | 충돌; 원본 유지 |
| IN-04 | 다른 tenant 동일 외부키 | 독립 저장; 교차조회/처리 거부 |
| IN-05 | 없는 manifest/미지원 schema/capability 없음 | 저장 전 거부 |
| IN-06 | 권한 회수/일반 멤버 수신 | 권한 거부 |
| IN-07 | 처리 성공 및 반복 처리 | PROCESSED, process_requested/accepted 각각 정확히 1건 |
| IN-08 | audit/outbox 저장 중 예외 | 상태·outbox·audit 함께 rollback |
| IN-09 | 독립 SQLite 연결 동시 수신/처리 | 유일 수신/유일 후속 효과; lock은 정해진 오류로 처리 |
| IN-10 | 커밋 후 응답 전 중단 및 재시작 | 동일 작업 재조회 가능; 효과 중복 없음 |

기존 v1–v5 migration 테스트와 도메인/개발관제 테스트가 유지되어야 한다. 새 필드가 필요하면 기존 migration을 수정하지 않고 다음 version으로 추가한다.

## 5. 실행 순서와 워커 인계

1. 이 개발노트 검토/커밋 → CORE-01 서비스/저장소/테스트 수직 구현.
2. CORE-02 → CORE-03 → ADAPTER-01 합성 계약 테스트.
3. CATALOG-01/02 → CONTENT-01 → ORDER-01 → CLAIM-01/FINANCE-01.
4. UX-01은 CORE-02 뒤 병행 가능. AI-01은 gateway 승인 경계 후. NOTIFY-01/PROD-01은 외부 실행 없이 준비.

각 작업 패킷은 task_id, parent_id, L4 범위, 읽을 문서, 수정 파일 소유, 선행 커밋, 테스트 ID, 금지 외부 효과, 완료 결과 경로를 가진다. PM은 한 패킷 완료 후 다음 안전 패킷에 진입한다. 개발관제는 워커 로그에서 상태를 읽되 문서 작성/코딩/검증/커밋 단계를 구분한다. 도구의 쓰기 권한이 막히면 실제 거부 결과를 보고하고, 기록 없이 진행했다고 주장하지 않는다.
