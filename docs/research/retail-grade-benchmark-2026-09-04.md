# 100meAiStore 리테일급 벤치마크 및 갭 분석

- 확인일: 2026-09-04 (Asia/Seoul)
- 범위: 아키텍처·운영 통제 조사. 법률 자문이나 수익 보장이 아니다.
- 근거 원칙: 판매채널·클라우드·OpenAI·정부·법령의 공식/1차 자료를 우선했다. 공개 문서에서 확인되지 않은 기능은 `미확인`으로 표시하고 LIVE 연결 전 Discovery Gate에서 실제 계정과 샘플로 재검증한다.

## 1. 결론

현재 아키텍처의 큰 방향(멀티테넌트 격리, 승인 원장, typed Tool Gateway, outbox, 멱등성, 대사, BYOK, fail-closed)은 상용 리테일 통합 제품의 기반과 일치한다. 한 단계 높은 수준이 되려면 다음 다섯 항목을 구현의 P0 게이트로 승격해야 한다.

1. **채널별 capability manifest**: 네이버·쿠팡을 공통 인터페이스에 억지로 맞추지 않고, 인증·페이지네이션·필드·상태·호출 제한·비동기 처리 여부를 버전별 데이터로 관리한다.
2. **수신과 실행을 분리한 inbox/outbox**: 수신 원문을 먼저 영속화하고 빠르게 ACK한 뒤 처리한다. 모든 외부 쓰기는 `intent 저장 → 실행 → 외부 재조회/대사`로 끝낸다.
3. **상태가 아니라 상태+근거**: 내부 상태, 외부 상태, 마지막 확인시각, 외부 버전/변경시각, 원문 해시, 매핑 버전을 함께 저장한다. `UNKNOWN`은 실패와 구분한다.
4. **승인 시점 조건 재검증**: 승인 토큰은 대상 digest·정책 버전·금액 상한·만료를 묶고, 실행 직전에 재고·원가·마진·권한·정지상태를 다시 검증한다.
5. **법정 기록과 운영 데이터 분리**: 후보 콘텐츠 10일 삭제 규칙과 전자상거래 법정 보존(광고 6개월, 계약/결제/공급 5년, 불만/분쟁 3년)을 별도 보존 클래스로 구현한다.

주 순이익 500만원은 제품 요구의 검증 KPI일 뿐 보장값으로 표현하면 안 된다. 상품 수, 승인 수, 예상 마진도 실제 반품·취소·광고·정산 데이터로 보정되지 않으면 사업 의사결정 근거가 아니다.

## 2. 국내 채널 조사

### 2.1 네이버 커머스API / 스마트스토어

| 항목 | 공식 문서에서 확인한 사실 | 설계 의미 |
|---|---|---|
| 인증 | 서버 간 OAuth 2.0 Client Credentials이며 scope를 제공하지 않는다. 토큰 요청 시 client secret 원문 대신 전자서명을 사용한다. 401 `GW.AUTHN`은 토큰 만료 가능성을 포함한다. | 사업자/연결별 client ID·secret 참조와 토큰 캐시를 분리한다. scope 기반 권한 축소를 가정하지 말고 자체 Tool Gateway 권한을 강제한다. 서명 생성 입력과 시간 동기화를 계약 테스트한다. |
| 상품 | 원상품과 채널상품이 분리되며 등록/조회/수정/삭제, 판매상태, 옵션 재고·가격, 다중 상품 변경 기능이 공개돼 있다. v2 BAD_REQUEST는 구조화 필드만으로 원인 판별이 어려울 수 있어 message 확인을 권고한다. | `Product`와 `Offer/Listing`을 분리한다. 오류 message는 비밀 제거 후 원문 보존하되 문자열에만 의존하지 않고 오류 분류의 `UNKNOWN`을 허용한다. 등록 응답만 믿지 말고 재조회한다. |
| 주문 | 변경일시 기준 변경 상품주문 조회가 핵심이다. 종료시각 생략 시 시작부터 24시간, 한 응답 최대 300개이며 `moreFrom`·`moreSequence`로 이어 조회한다. | 공개 문서 기준 폴링 커서가 필수다. `(lastChangedAt, sequence/productOrderId)` 복합 워터마크, 겹침 구간 재조회, 중복 제거, 300개 경계 테스트가 필요하다. |
| 발주/발송·클레임 | 발주 확인, 발송 및 주문·클레임 처리 API 묶음이 제공된다. | 구매자 주문의 “발주 확인”과 공급처에 보내는 내부 `PurchaseOrder`를 용어·ID로 분리한다. 취소/반품/교환은 주문과 별도 claim aggregate로 둔다. |
| 웹훅 | 이번에 검토한 공개 커머스API 문서에서 주문 전반의 범용 웹훅 계약은 확인하지 못했다. | `webhook_support=true`로 추정 금지. LIVE 전 실제 앱 권한/문서에서 확인하고, 미확인 상태에서는 폴링+대사가 정본이다. |

주요 근거: [인증](https://apicenter.commerce.naver.com/docs/auth), [최신 커머스API 목차](https://apicenter.commerce.naver.com/docs/commerce-api/current), [상품 API](https://apicenter.commerce.naver.com/docs/commerce-api/current/%EC%83%81%ED%92%88), [변경 상품 주문 내역 조회](https://apicenter.commerce.naver.com/docs/commerce-api/current/seller-get-last-changed-status-pay-order-seller), [발주/발송 처리](https://apicenter.commerce.naver.com/docs/commerce-api/current/%EB%B0%9C%EC%A3%BC-%EB%B0%9C%EC%86%A1-%EC%B2%98%EB%A6%AC) (모두 2026-09-04 확인).

### 2.2 쿠팡 Open API

| 항목 | 공식 문서에서 확인한 사실 | 설계 의미 |
|---|---|---|
| 인증 | WING에서 Access Key/Secret Key를 발급하고 요청별 HMAC 서명을 사용한다. 서버 시간 오차, 재사용 서명, HTTP method별 경로 조합 오류가 401 원인이 될 수 있다. 키 재발급/유효기간 운영도 존재한다. | 연결별 secret reference, NTP 감시, 요청 직전 서명, 키 만료 알림·회전 상태기계를 둔다. 서명 문자열은 로그 금지한다. |
| 상품 | 상품 생성에 배송/반품지, 카테고리, 고시, 옵션 등 채널 메타데이터가 요구된다. 상품/옵션 승인 전후 ID와 가능한 변경 경로가 달라질 수 있고 일부 속성은 생성 후 변경 불가다. 2026년에는 브랜드·식별자·필수 옵션 정책 변경 공지가 이어졌다. | 카테고리 메타 스냅샷과 `schema_version/effective_at`을 보존한다. `draft → approval_requested → approved/listed`를 분리하고, 수정 불가 필드는 교체/재등록 saga로 처리한다. |
| 가격/재고 | 승인 후 단품 변경 API가 사용되며 2026년 자동가격 필드가 추가됐다. | 플랫폼 자동가격 기능은 기본 비활성. 채널 자동가격과 자체 가격엔진이 동시에 쓰지 못하게 mutual exclusion하고 최저가·마진 guard를 둔다. |
| 주문/배송/반품/교환/CS/정산 | 개발자 포털에 각각 API 영역이 공개되어 있다. | 주문 line 단위 ID, 배송/클레임 별도 aggregate, 정산 대사 모델이 필요하다. 실제 권한·응답 샘플 없이는 LIVE-ready로 표시하지 않는다. |
| 호출 제한·이벤트 | 공식 포털은 속도제한 정책/최적화 공지를 제공한다. 이번 공개문서 검토에서는 범용 주문 웹훅 계약을 확인하지 못했다. | endpoint별 rate-limit profile과 adaptive backoff를 Discovery 결과로 저장한다. 폴링을 기본으로 하고 겹침 재수집·주기 대사를 강제한다. |

주요 근거: [쿠팡 개발자 포털](https://developers.coupangcorp.com/hc/ko), [API Key 발급](https://developers.coupangcorp.com/hc/ko/articles/360022939194-API-Key-AccessKey-SecretKey-%EB%8A%94-%EC%96%B4%EB%94%94%EC%84%9C-%EB%B0%9C%EA%B8%89-%ED%99%95%EC%9D%B8-%ED%95%A0-%EC%88%98-%EC%9E%88%EB%82%98%EC%9A%94), [HMAC 오류 점검](https://developers.coupangcorp.com/hc/en-us/articles/360022935394-I-get-a-401-Unauthorized-error-What-do-I-do), [상품 생성](https://developers.coupangcorp.com/hc/en-us/articles/360033877853-Product-Creation), [상품 수정(승인 필요)](https://developers.coupangcorp.com/hc/ko/articles/360034156073-%EC%83%81%ED%92%88-%EC%88%98%EC%A0%95-%EC%8A%B9%EC%9D%B8%ED%95%84%EC%9A%94), [자동 가격 업데이트](https://developers.coupangcorp.com/hc/ko/articles/58265657840025-Open-API-%EC%97%85%EB%8D%B0%EC%9D%B4%ED%8A%B8-%EC%9E%90%EB%8F%99-%EA%B0%80%EA%B2%A9-%EA%B8%B0%EB%8A%A5-2026%EB%85%84-5%EC%9B%94-22%EC%9D%BC-%EA%B2%8C%EC%8B%9C) (2026-09-04 확인).

### 2.3 국내 어댑터에 즉시 적용할 규칙

- 채널 연결은 `UNVERIFIED → AUTHENTICATED → DISCOVERED → SHADOW_VERIFIED → LIVE_READY`로 승격한다.
- capability manifest 필수 필드: API/문서 버전, 인증/회전, endpoint별 권한, pagination, 최대 기간/건수, rate limit, webhook 여부, 외부 ID 안정성, nullable/삭제 의미, 비동기 처리 상태, 재조회 방법.
- 주문 폴링은 마지막 성공시각보다 과거부터 겹쳐 읽고 `(channel, seller, external_line_id)` unique constraint로 중복 제거한다.
- 타임아웃 이후 외부 쓰기는 새 키로 재실행하지 않는다. `UNKNOWN`으로 두고 조회·대사 후에만 재시도/보상한다.
- 네이버의 “발주 확인”과 공급처 `PurchaseOrder`를 UI와 코드에서 혼용하지 않는다.

## 3. 상용 플랫폼 패턴

### 3.1 Shopify

- 독립/외부 앱은 authorization code grant, 백그라운드 작업은 offline access token, 사용자 귀속 작업은 online token을 쓸 수 있다. 승인된 access scope가 권한 경계다.
- 웹훅은 HMAC을 검증하고 `X-Shopify-Webhook-Id`로 중복을 제거해야 한다. 순서는 보장되지 않으며 누락 가능성을 전제로 정기 reconciliation을 권장한다.
- 수신 엔드포인트는 빠르게 200을 반환하고 큐로 넘겨야 한다. 공식 문서는 연결 1초, 전체 5초 제한과 실패 재시도를 설명한다.
- GraphQL의 지원 mutation은 `@idempotent`와 UUID 키를 사용하고 24시간 보호한다. 모든 mutation이 이를 지원한다고 가정할 수 없다.

근거: [앱 인증](https://shopify.dev/docs/apps/build/authentication-authorization), [웹훅 개요](https://shopify.dev/docs/apps/build/webhooks), [웹훅 검증/재시도](https://shopify.dev/docs/apps/build/webhooks/verify-deliveries), [멱등성](https://shopify.dev/docs/api/usage/implementing-idempotency) (2026-09-04 확인).

### 3.2 Amazon SP-API

- 공개 앱은 판매자별 OAuth 2.0(LWA) 권한을 받고 역할/제한 역할로 접근을 통제한다. 제한 역할은 PII 접근에 강화된 보안을 요구한다.
- 개별·batch·Feeds/Reports 같은 bulk 경로가 분리된다. 대량 listing은 JSON Listings Feed 등 비동기 처리 결과를 추적해야 한다.
- Notifications API는 SQS/EventBridge로 주문 변경·listing·feed 처리 이벤트를 보낼 수 있지만, 이벤트 소비자는 중복·지연과 후속 재조회를 견뎌야 한다.
- feed는 처리 지연이 길 수 있으며 같은 유형의 연속 feed가 순차 처리될 수 있다. 제출 완료가 상품 반영 완료가 아니다.

근거: [SP-API 온보딩](https://developer-docs.amazon.com/sp-api/docs/onboarding-overview), [Notification types](https://developer-docs.amazon.com/sp-api/docs/notification-type-values), [상품 listing 관리](https://developer-docs.amazon.com/sp-api/lang-en_EN/docs/manage-product-listings-guide), [Feeds 모범사례](https://developer-docs.amazon.com/sp-api/lang-US/docs/feeds-api-best-practices) (2026-09-04 확인).

### 3.3 eBay

- seller 대행은 OAuth user authorization을 사용한다.
- Inventory Item → Offer → Publish의 단계형 모델이며 location과 payment/fulfillment/return business policy가 선행한다. SKU는 판매자 범위에서 유일해야 한다.
- Fulfillment API는 checkout 완료 주문을 대상으로 배송·환불을 관리하며, pending payment는 조회 범위가 다르다.
- Notification API는 구독/목적지/필터/검증 수명주기를 제공한다. 구형 Platform Notifications 문서조차 알림만 믿지 말고 API 폴링으로 확인하라고 명시한다.

근거: [OAuth](https://developer.ebay.com/api-docs/static/oauth-details.html), [Inventory 개요](https://developer.ebay.com/api-docs/sell/inventory/static/overview.html), [Fulfillment API](https://developer.ebay.com/develop/api/sell/fulfillment_api), [Notification API](https://developer.ebay.com/develop/api/buy/notification_api), [알림과 폴링 확인](https://developer.ebay.com/api-docs/static/platform-notifications-landing.html) (2026-09-04 확인).

### 3.4 100meAiStore가 가져올 공통 원칙

| 리테일 패턴 | 100meAiStore 목표 |
|---|---|
| merchant별 설치·동의·scope | 사업자별 연결과 자격증명 격리, least privilege, 재인증/회전 UX |
| Product/Inventory/Offer 분리 | 정규화 Product, SupplierOffer, ChannelOffer, ListingAttempt 분리 |
| 이벤트는 빠른 ACK 후 비동기 | durable inbox → 검증 → 정규화 → domain event |
| 알림 순서·전달 불완전 | version/changed-at guard + periodic reconciliation |
| 비동기 feed/listing | command가 `SUCCEEDED`가 아니라 `ACCEPTED/PROCESSING/VERIFIED/REJECTED/UNKNOWN`을 가짐 |
| API별 멱등 지원 차이 | 내부 멱등 원장은 항상 적용, 외부 native key는 capability가 있을 때 추가 적용 |
| 사용자별/앱별 권한 | 사용자 RBAC와 channel credential 권한을 둘 다 만족해야 실행 |

## 4. OMS/PIM/Marketplace 리테일급 기준

### 4.1 데이터·멀티테넌시

- 모든 업무 테이블, unique key, cache key, object path, queue message에 tenant를 포함한다.
- PostgreSQL RLS 또는 동등한 DB 방어를 애플리케이션 필터의 보조선으로 사용하고, connection/session tenant 누락은 deny한다.
- 외부 ID의 유일성 범위는 `(tenant, channel, connection, external_type, external_id)`로 둔다.
- PIM canonical data와 채널별 projection을 분리한다. 채널 오류 문구나 필드를 canonical model에 직접 누출시키지 않는다.
- 원문 payload는 암호화 저장소에 hash/수신시각/schema/parser version과 보관하며, 정규화 레코드로 추적 가능해야 한다.

### 4.2 승인·감사

- 승인 요청은 단순 boolean이 아니라 immutable intent snapshot이다: command, target, before/after digest, 금액/수량, 근거, 정책 버전, requester, eligible approvers, expiry.
- 승인과 실행 사이 조건 변화는 `STALE`로 만들고 재승인한다. 한 명 승인 정책도 자기 요청 승인, 권한 회수, 중복 클릭을 방어한다.
- 감사 로그 hash chain은 변조 탐지 수단이지 삭제 금지/백업/접근통제의 대체물이 아니다. 별도 보관과 검증 job이 필요하다.
- 관리자 override는 원키 열람이 아니라 저장된 키 폐기/재등록 유도만 허용한다. 모든 지원자 접근은 break-glass 사유·시간제한·감사 대상이다.

### 4.3 outbox/saga/대사

- DB 상태 변경과 outbox event를 같은 트랜잭션에 기록한다. relay는 at-least-once이므로 consumer inbox가 event ID를 기록해 멱등 처리한다.
- 주문→사용자 발주승인→수동 결제→공급처 주문→운송장→채널 발송의 장기 흐름은 중앙 orchestrated saga로 관리한다.
- 보상은 “DB rollback”이 아니다. 예: 외부 상품등록 성공 후 내부 실패는 외부 재조회→adopt 또는 판매중지 승인으로 수렴한다.
- 일일 증분 대사와 주기적 전체/표본 대사를 분리한다. 주문, listing, 재고, 배송, claim, 정산 각각 워터마크와 예외 큐를 가진다.

공식 근거: AWS는 transactional outbox가 DB 쓰기와 메시지 발행의 dual-write 불일치를 해결하며 중복 때문에 소비자가 멱등이어야 한다고 설명한다. 여러 서비스의 장기 트랜잭션은 saga continuation/compensation으로 다룬다. [Transactional outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html), [Saga patterns](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/saga-patterns.html) (2026-09-04 확인).

### 4.4 SLO/DR 최소선

LIVE 전 숫자로 확정해야 할 SLI/SLO:

- 주문 수집 지연 p95/p99 및 누락률
- 중복 부작용 0건(관측기간과 분모 명시)
- 재고/가격 freshness와 품절 안전중지 시간
- 승인 요청 생성/알림 지연
- 외부 write `UNKNOWN`의 최대 체류시간
- 대사 예외 해결시간
- API/worker 가용성보다 **주문·발주 업무 성공률**

DR 증거는 백업 설정 화면이 아니라 격리 환경 복구 시험이다. RPO/RTO, 백업 보존, 키/secret 복구, outbox replay, hash-chain 검증, DNS/채널 callback 전환, 담당자와 마지막 성공일을 기록한다. 월 3만원 상한으로 다중리전 상시 대기가 불가능하면 이를 숨기지 말고 단일리전+검증된 백업의 위험 수용으로 명시한다.

## 5. AI 관리자 에이전트 기준

### 5.1 안전 경계

- 상품 설명, 공급처 XML/Excel, 고객 문의, 웹 검색 결과, 이미지 OCR은 모두 **신뢰하지 않는 데이터**다. 그 안의 “지시”는 실행 지침이 될 수 없다.
- 모델은 raw credential, DB, 범용 HTTP, shell, 결제 수단에 접근하지 않는다. tenant·목적·스키마가 고정된 최소 tool만 호출한다.
- tool risk를 `READ / REVERSIBLE_WRITE / BUSINESS_WRITE / FINANCIAL_OR_LEGAL`로 분류한다. 모든 발주·상품등록/변경·환불/보상·판매중지는 현재 요구대로 사람 승인이다.
- 승인 화면은 모델의 요약만 보여주지 않고 가격·수량·대상·배송지/공급처·마진·근거 원문 링크·변경점·만료를 결정론적으로 표시한다.
- 모델 출력은 JSON schema로 검증하고 정책 엔진이 다시 판단한다. 자연어 “승인함”이나 높은 confidence는 권한이 아니다.

OpenAI도 guardrail을 인증·인가·접근통제와 함께 계층적으로 적용하고, 결제·대규모 환불·주문 취소 같은 고위험 작업에 human intervention을 두도록 권고한다. [A practical guide to building agents](https://openai.com/business/guides-and-resources/a-practical-guide-to-building-ai-agents/), [prompt injection 저항 설계](https://openai.com/index/designing-agents-to-resist-prompt-injection/) (2026-09-04 확인).

### 5.2 BYOK·비용 통제

- API key 유효성 검사는 서버에서 최소 호출로 하고 원문은 즉시 secret store에 저장한다. 앱 DB에는 secret reference, fingerprint, 상태, 마지막 검증일만 둔다.
- master/플랫폼 관리자가 키 원문을 볼 수 없어야 한다. “리셋”은 기존 secret 폐기 및 소유자의 재등록이다.
- 예산은 `예약(estimated) → 실제 사용 확정 → 차액 환입` ledger로 관리해 동시 run이 한도를 초과하지 않게 한다.
- 일/월 금액, input/output token, tool call, run, 이미지/영상 생성 수, wall-clock, retry 상한을 각각 둔다.
- `economy/balanced/quality`는 마케팅 이름이 아니라 허용 모델·최대 토큰·승격 조건이 버전된 정책이어야 한다.
- 키 실패/AI 예산 소진은 AI만 차단하고 주문 수집·대사·승인·안전중지는 계속한다.
- cache에는 tenant, model, prompt version, policy version, input digest, locale를 포함하고 PII/주문 CS는 보수적으로 cache하지 않는다.

## 6. 한국 운영·법규 체크리스트

다음은 공식 근거를 설계 요구로 옮긴 체크리스트이며 개별 상품의 적법성 확정판단은 전문가/관할기관 확인 대상이다.

### 6.1 공통 전자상거래·개인정보

- [ ] 사업자등록·통신판매업 신고/면제 해당 여부와 채널 입점 요건 확인. 면제여도 거래기록·정보제공·공급·청약철회 의무가 사라지지 않는다.
- [ ] 판매자 신원, 가격/배송/반품·교환 조건, 상품정보고시 필드를 채널 게시 전 검증한다.
- [ ] 소비자 청약철회와 표시·광고/계약 불일치 예외 기간을 claim workflow와 SLA에 반영한다.
- [ ] 법정 보존 class: 표시·광고 6개월, 계약/청약철회 5년, 결제/공급 5년, 불만/분쟁 3년. 철회 고객 기록은 일반 운영 데이터와 별도 보존한다.
- [ ] 배송을 공급처가 수행하면 주문자 이름·연락처·주소 처리 관계(업무위탁/제3자 제공)를 법적 역할에 맞게 문서화하고 처리방침에 공개한다. 공급처별 계약, 목적 외 처리 금지, 보호조치, 재위탁, 파기와 감독 증거를 둔다.
- [ ] 탈퇴/사업자 삭제 시 즉시 삭제 대상과 법정 분리보관 대상을 구분하고, 복구기간 종료 후 crypto-shred/파기 job과 증적을 둔다.
- [ ] 공개 저장소·로그·AI prompt·analytics에 주문 PII, 키, 사업자 서류가 들어가지 않도록 DLP 테스트한다.

근거: [전자상거래법 제6조](https://www.law.go.kr/LSW/lsLawLinkInfo.do?chrClsCd=010202&lsJoLnkSeq=900617247), [시행령 제6조 보존기간](https://law.go.kr/lumLsLinkPop.do?lspttninfSeq=63460), [전자상거래법 제17조 청약철회](https://www.law.go.kr/LSW/lsInfoP.do?ancYnChk=0&chrClsCd=010202&efYd=20240807&lsiSeq=260023&urlMode=lsInfoP), [개인정보보호법 제26조 업무위탁](https://law.go.kr/lsLinkCommonInfo.do?chrClsCd=010202&lsJoLnkSeq=1020399009), [공정위 상담사례](https://www.ftc.go.kr/www/selectExmplView.do?dscsnExmplSn=898&key=330&pageIndex=2&pageUnit=10&searchCnd=all) (2026-09-04 확인).

### 6.2 상품군

| 상품군 | 자동 게시 전 체크 | 기본 조치 |
|---|---|---|
| 화장품 | 정식 유통/책임판매 관련 표시, 전성분·사용기한 등 상품고시, 기능성 근거, 의약품 오인·질병 치료·발모·체중감량 등 금지 표현, 이미지 사용권 | 효능/의학 표현 탐지 시 게시 차단 후 사람 검토. 단순 경고 승인만으로 명백한 위반 표현을 통과시키지 않는다. |
| 반려동물 사료·간식 | 제조/수입·성분등록, 표시사항, 원료·성분, 원산지, 소비기한/잔여기한, 보관, 회수정보, 효능 오인 표현 | 등록번호/표시/잔여기한 근거 없으면 판매 후보 제외. 공급처 원문과 배치/기한 snapshot 보존. |
| 일반 식품(추후) | 영업·제품 유형별 신고/허가, 식품 표시·광고, 소비기한, 회수, 건강기능식품 별도 규정 | 초기 scope 밖으로 두고 전용 compliance pack 없이는 활성화 금지. |
| 패션/생활 | 전자상거래 상품정보고시, 소재/치수/제조자·수입자, KC 등 카테고리별 인증, 이미지·디자인 권리 | category metadata 최신성 확인, 필수값 누락 시 차단. |
| 명품/브랜드 | 진정상품·유통경로·세금계산서/매입증빙·serial, 상표/이미지 사용, 병행수입 표시, 플랫폼 진품정책 | MVP 제외 유지. provenance chain과 전문가 검토, 채널별 승인 전에는 enable 불가. |

근거: 식약처는 화장품의 질병 치료·피부 회복·발모·체중감량 등 의약품 오인 광고 사례를 온라인 불법유통 대상으로 안내한다. [식약처 온라인 불법유통 신고 가이드](https://www.mfds.go.kr/wpge/m_661/de010410l001.do). 농식품부는 반려동물 사료도 사료관리법상 제조업 등록·수입사료 성분등록·표시사항 대상이라고 설명한다. [농식품부 설명](https://mafra.go.kr/bbs/home/793/578827/artclView.do). 진정상품이라도 유통계약과 상표 기능 훼손 여부에 따라 판단이 구체적이므로 “정품 주장”만 자동 통과시키지 않는다. [대법원 2018도14446](https://law.go.kr/LSW/precInfoP.do?precSeq=226033) (2026-09-04 확인).

## 7. 현 요구사항/아키텍처 불일치 및 결정

| ID | 발견 | 위험 | 권고 결정 |
|---|---|---|---|
| G-01 | 요구사항의 “이미지 권리 불명확은 경고 후 승인 가능”이 명백한 법/권리 침해와 경계가 모호하다. | 승인자가 불법을 합법화할 수 있다는 오해 | `불명확=보류/증빙요청`, `명백 침해=강제차단`, `확인된 권리=게시 가능` 3단계로 분리. |
| G-02 | “효능 표현은 자동삭제보다 경고 우선”은 명백한 화장품/사료 위반 표현에도 느슨할 수 있다. | 채널 정지·행정 위험 | 명백 금지 표현은 게시 차단, 애매한 표현만 경고+검토. 원문을 몰래 수정하지 말고 diff 승인. |
| G-03 | `24시간 승인 만료`만 있고 승인 시점의 가격·수량·재고 변화 허용범위가 수치화되지 않았다. | stale 승인으로 손실 | approval digest와 max delta를 정책화. 변동 시 무조건 또는 임계치 초과 시 재승인(발주는 보수적으로 항상 재검증). |
| G-04 | “한 명 승인”에 자기 승인/상충 권한 규칙이 없다. | 내부통제 약화 | 초기 3인 소규모에서는 허용하되 요청자=승인자 여부를 설정 가능하게 하고 고위험/한도 초과는 master 전용. |
| G-05 | 국내 채널 웹훅 가능성을 공통 envelope가 암묵적으로 전제할 수 있다. | 주문 누락 | capability가 확인되지 않으면 polling+reconciliation. webhook은 최적화일 뿐 정본 아님. |
| G-06 | 법정 기록만 보관한다는 요구와 후보 콘텐츠 10일/감사 append-only가 충돌 가능하다. | 과잉보관 또는 조기삭제 | retention class 및 legal basis를 엔터티/필드별로 정의. 감사 metadata에서 PII 분리·토큰화. |
| G-07 | 월 3만원 상한과 PostgreSQL PITR·KMS·객체 versioning·다중 알림을 모두 운영하면 비용 충돌 가능성이 크다. | 비용 차단이 핵심 기능까지 영향 | 배포 전 실견적 gate, 단일리전 위험 수용, 무료/저가 DB의 복구·egress 제한 실측. 비용 때문에 감사/백업을 제거하지 않는다. |
| G-08 | `LIVE=사업자정보+채널인증`은 공급처·반품지·법정정보 검증 전 외부 등록을 허용한다. | 실판매 가능한 부적합 listing | LIVE 연결과 LIVE write-ready를 분리. 상품 게시에는 supplier/compliance/return-address discovery가 추가로 필요. |
| G-09 | 공급처에 주문자 배송정보를 전달하는 법적/계약적 역할이 미정이다. | 개인정보 위탁/제공 위반 | 공급처 onboarding에 DPA/위탁 조건·재위탁·파기·보안 검증을 P0로 추가. |
| G-10 | 주 500만원 목표가 추천 점수에 과도하게 반영될 수 있다. | 과장·위험상품/광고 선택 | KPI는 실현 순이익과 confidence interval로 보고. 모델 출력에 보장 문구 금지, cash-flow/반품/정산 지연 stress test. |

## 8. Gap matrix

| 역량 | 현재 설계 | 리테일급 목표 | 우선순위 |
|---|---|---|---|
| tenant/RBAC | tenant context, membership version | DB RLS, cross-tenant fuzz/property test, break-glass 감사 | P0 |
| 승인 | 24h, policy version, audit | immutable intent digest, stale invalidation, execution-time guard, race test | P0 |
| channel adapter | 공통 interface/capability | 버전된 manifest, schema drift, auth rotation, real sample round-trip | P0 |
| 주문 수집 | webhook 또는 일반 프로그램 | overlap polling, durable cursor, inbox dedupe, late/out-of-order handling | P0 |
| 외부 write | idempotency + verify | intent-first ledger, UNKNOWN state, adopt/compensate, native-key capability | P0 |
| workflow | 상태기계 문서 | durable orchestrated saga, timer/deadline, operator repair command | P0 |
| reconciliation | 계약에 존재 | resource별 incremental/full job, exception ownership/SLA | P0 |
| 감사/보존 | hash-chain, TBD 법정기간 | retention class, WORM/별도백업, verifier, PII 분리 | P0 |
| AI 안전 | typed gateway, scrubber, approval | untrusted-content labels, tool risk tier, adversarial eval, evidence UI | P0 |
| BYOK | secret ref, budget | reservation ledger, concurrency cap, rotation/disable drill | P1 |
| 관측성 | metric 목록 | SLI 정의·error budget·synthetic journey·tenant-safe trace | P1 |
| DR | PITR/restore 계획, 수치 TBD | 숫자 RPO/RTO와 분기별 restore/replay 증적 | P0 before LIVE |
| PIM | 정규화 개념 | canonical/channel projection, taxonomy/schema version migration | P1 |
| 공급처 | API/files/manual | provenance, DPA/반품지/정산 신뢰등급, freshness SLA | P0 |
| 해외 | pipeline only | OAuth/scopes, localization/tax/customs/returns compliance packs | P2 |
| AI 콘텐츠 | 승인·버전 | claim library, rights provenance, channel lint, A/B attribution | P1 |

## 9. P0/P1/P2 수용기준

### P0 — 국내 DEMO/SHADOW 및 LIVE 안전 기반

- [ ] PostgreSQL(또는 동일 트랜잭션 보장 저장소)에 tenant-scoped schema, RLS, unique keys, approval/audit/inbox/outbox/workflow persistence가 존재한다.
- [ ] 프로세스 강제 종료 전후 테스트에서 승인·주문·outbox가 복구되고 외부 부작용 모의 호출은 정확히 1회의 효과만 난다.
- [ ] duplicate, out-of-order, delayed, malformed, signature failure, 401/403, 429, 5xx, timeout-after-commit을 채널 contract test가 재현한다.
- [ ] 네이버 300건 경계와 continuation cursor, 겹침 폴링, 같은 변경시각 다건 테스트가 통과한다.
- [ ] 쿠팡 HMAC clock skew/키 만료/회전과 카테고리 schema 변경 fixture가 통과한다.
- [ ] 외부 write timeout은 `UNKNOWN`이며 재조회 없이 자동 재실행되지 않는다.
- [ ] 모든 발주·신규상품·공급처 교체·환불/보상·비정형 CS·판매중지·콘텐츠 변경은 유효 approval 없이는 adapter write가 0건이다.
- [ ] 승인 후 원가/재고/마진/대상 digest가 바뀌면 실행이 차단되고 재승인된다.
- [ ] 공급처 business/반품지 및 주문 PII 처리계약 상태가 미확인인 경우 LIVE 발주가 차단된다.
- [ ] 명백한 화장품/사료 금지 표현과 상표권 침해 징후는 사람 승인만으로 게시할 수 없고 compliance review로 이동한다.
- [ ] 법정 보존 class와 일반 삭제 class가 테스트되고, 탈퇴 후 분리보관 데이터는 운영 조회에서 제외된다.
- [ ] RPO/RTO 숫자, 월 실견적, 실제 restore+outbox replay 결과가 LIVE gate에 첨부된다.
- [ ] 주 수익 목표는 DEMO에서 보장 문구가 없고 수수료·배송·반품·광고·AI비용을 포함한 식과 가정 범위를 표시한다.

### P1 — 운영 품질 및 리테일 동급

- [ ] listing/order/stock/claim/settlement별 증분 대사와 수동 full reconciliation UI가 있고 예외에 owner/deadline이 있다.
- [ ] SLO dashboard가 주문 수집 지연, stale stock, UNKNOWN age, 승인 지연, 대사 차이, 비용을 tenant별로 보여준다.
- [ ] category schema drift 탐지와 adapter certification suite로 새 API 버전을 shadow에서 검증한 뒤 승격한다.
- [ ] agent eval set에 prompt injection, 숨은 지시가 든 공급처 파일, 가격/단위 혼동, PII 유출, tool escalation 공격이 포함되고 release threshold를 충족한다.
- [ ] 비용 reservation ledger가 동시 AI run에서도 일/월 한도를 넘지 않고, 소진 시 결정론적 핵심 기능은 계속된다.
- [ ] Android PWA 승인 화면에서 raw evidence/diff/정책/금액/만료/재검증 상태를 한 화면에 확인하고 중복 탭 race가 안전하다.
- [ ] 콘텐츠와 listing version의 성과 귀속이 가능하지만 허위 후기·기만 광고 자동화는 제공하지 않는다.

### P2 — 한 단계 높은 수준/해외 확장

- [ ] Shopify/Amazon/eBay용 authorization lifecycle, scope diff, webhook/inbox, reconciliation capability pack을 구현한다.
- [ ] 국가/채널별 tax, 통관, 반품, 제품안전, 언어, 금지표현을 versioned compliance pack으로 분리한다.
- [ ] 채널·공급처별 신뢰도와 실제 정산/반품 데이터로 profit forecast를 보정하고 prediction interval·calibration을 공개한다.
- [ ] 재해훈련, credential compromise, 대량 품절, 채널 정지, 공급처 파산을 game day로 반복하고 MTTR를 추적한다.
- [ ] 고객 제공 플랫폼으로 확대할 때 API 약관, 개인정보 역할, n8n 및 모든 OSS 라이선스, 과금/세무를 재심사한다.

## 10. PM 실행 지시 요약

1. 다음 구현은 UI 확장이 아니라 `durable DB + inbox/outbox + approval digest + UNKNOWN/reconciliation` 수직 슬라이스를 우선한다.
2. Naver/Coupang connector는 real credential 없이 LIVE라고 부르지 않는다. fixture 기반 DEMO와 실계정 SHADOW를 분리한다.
3. adapter contract에 `native_idempotency`, `event_delivery`, `poll_cursor`, `write_result_semantics`, `verification_method`, `schema_version`을 명시적으로 추가한다.
4. 법정 보존표와 supplier 개인정보 처리 검증을 backlog가 아닌 release gate로 옮긴다.
5. PM은 각 PR에 위 P0 수용기준 ID와 테스트 증거를 연결하고, 불확실한 외부 사양은 추론하지 말고 `UNVERIFIED capability`로 남긴다.

## 11. 제한 및 재검증 항목

- 네이버/쿠팡은 판매자 유형·계약·승인 상태에 따라 사용 가능한 API가 달라질 수 있다. 실제 사업자/채널 계정 발급 뒤 권한과 샘플을 재확인해야 한다.
- 공개 문서에서 찾지 못한 웹훅이 별도 파트너 계약에 있을 수 있으므로 “없음”이 아니라 “미확인”이다.
- 법령과 플랫폼 정책은 변경된다. LIVE 배포 전 법률 전문가/관할기관 및 최신 채널 정책 검토가 필요하다.
- Shopify/Amazon/eBay는 해외 파이프라인 설계 비교 대상이며 국내 MVP 구현 범위가 아니다.
- 수익성은 공급가·배송·수수료·광고·반품·정산지연의 실제 데이터 없이는 검증할 수 없다.
