# AI 연동 위탁판매 독립 제품군 아키텍처 사전조사

대상: 한국 온라인 위탁판매 독립 제품군을 기획하는 제품 책임자와 개발팀  
기준일: 2026년 9월 4일  
범위: 국내외 자동 위탁판매 제품, 오픈소스 커머스 및 ERP, 워커 구현 패턴, OpenAI Codex와 ChatGPT 및 모바일 연동 선택지  
제외: 이번 단계의 코드 구현, 특정 공급사와의 계약 평가, 확정 인프라 산정

## 경영진 결론

독립 제품군은 쇼핑몰 엔진을 그대로 포크하기보다 멀티채널 판매 운영체제를 새로 두고 검증된 오픈소스 모듈을 조합하는 방식이 적합하다. 핵심 자산은 스토어프론트가 아니라 공급사별 상품 원본을 표준화하는 카탈로그, 채널별 상품 투영, 재고와 가격의 최신성, 판매주문과 공급사 구매주문의 연결, 클레임, 정산, 정책 승인, 감사 추적이다.

권장 방향은 모듈러 모놀리스로 시작해 이벤트와 장기 워크플로 경계를 먼저 고정하는 것이다. PostgreSQL을 거래 원장으로 삼고 outbox 패턴으로 이벤트를 발행하며, 장기 주문 및 클레임 흐름은 Temporal 계열의 내구성 워크플로를 검토한다. Medusa의 도메인 모듈과 보상 가능한 workflow 개념, Vendure의 API 서버와 별도 worker 및 job queue, Saleor의 API first와 외부 app 및 webhook 모델, ERPNext의 Sales Order와 Purchase Order 연결을 설계 패턴으로 흡수한다.

OpenAI 계층은 보조 기능이 아니라 운영 오케스트레이터다. ChatGPT 또는 Codex 에이전트가 목표와 현재 상태를 읽고 상품 발굴, 후보 심사, 등록, 가격과 재고 조정, 주문 예외, CS, 정산 대사를 연속적으로 계획하고 commerce tools를 호출한다. 다만 AI가 데이터베이스나 채널 API를 직접 자유 호출하게 하지는 않는다. 모든 실행은 typed command, 정책 엔진, 금액 한도, 멱등성, 승인과 감사 로그가 있는 tool gateway를 통과한다. 거래 원장과 상태기계는 결정론적으로 유지하고 AI는 다음 행동의 선택과 예외 해석을 맡는다.

제품은 OpenAI Responses API 기반 cloud operations agent를 기본 자동운영 엔진으로 두고, Codex SDK 기반 local operations agent를 선택형 런타임으로 제공하는 구성이 적합하다. ChatGPT plugin은 같은 MCP tool catalog를 노출해 모바일 ChatGPT에서도 운영 지시, 승인, 조회와 예외처리를 가능하게 한다. 독립 모바일 앱은 동일한 control API를 사용하는 승인함, 경보와 KPI 화면으로 둔다.

## 조사 질문과 판단 기준

이번 조사는 다섯 가지 질문에 답한다. 첫째, 상용 자동 위탁판매 제품이 실제로 자동화하는 구간은 무엇인가. 둘째, 오픈소스 커머스와 ERP는 주문 및 재고 흐름을 어떻게 나누는가. 셋째, 독립 상용 제품에 재사용하기 좋은 라이선스와 경계는 무엇인가. 넷째, Codex와 ChatGPT가 어떻게 실제 운영 결정을 내리고 안전하게 실행하는가. 다섯째, 모바일 ChatGPT와 독립 모바일 앱을 어떻게 동일한 자동운영 제어면에 연결할 것인가.

평가 기준은 멀티채널, 멀티공급사, 한국 채널 어댑터 용이성, 장기 작업 내구성, 실패 복구, 감사 가능성, 사람 승인, AI 교체 가능성, 모바일 연동, 상용 재배포 라이선스다.

## 리테일 및 드롭시핑 제품 조사

### 상용 자동화 제품이 파는 것

AutoDS는 상품 가져오기, 가격과 재고 모니터링, 주문 처리, 송장 갱신을 한 제품 안에서 제공한다. 중요한 구현 단서는 실시간 이벤트가 아니라 하루 여러 번의 scheduled scan을 사용하며 공급가나 재고가 바뀐 시점과 채널 반영 사이에 지연이 존재한다는 점이다. 공급사 품절 시 대체 공급사를 자동 선택하지 않고 수동 전환을 요구한다. 이는 공급사 데이터가 대개 완전한 event source가 아니므로 최신성 등급과 stale window가 제품 모델에 반드시 들어가야 함을 뜻한다. 출처: AutoDS, Product monitoring, 2026년 9월 접근, https://help.autods.com/en/articles/12699906-product-monitoring-configure-price-and-stock-updates-preferences

Inventory Source는 공급사 상품 및 재고 피드 동기화와 판매채널 주문을 공급사로 전달하는 Order Manager를 분리하고, 다중 공급사 split order routing을 제공한다. 자체 도움말은 신규 사용자가 처음 몇 주 동안 주문을 verify and process 방식으로 운영하도록 권한다. 이는 완전자동 발주보다 관찰 및 승인 모드에서 시작해 신뢰 점수를 쌓는 단계적 자동화가 실전 패턴임을 보여준다. 출처: Inventory Source, Order Manager Getting Started, 2024년경, https://help.inventorysource.com/article/123-order-manager-getting-started-guide ; Inventory Source, Start Dropshipping, 2026년 접근, https://www.inventorysource.com/start-dropshipping/

Shopify의 order routing은 여러 location 중 우선순위, 재고 균형, 비용을 기준으로 fulfillment location을 고른다. 드롭시핑에서는 location을 창고가 아니라 공급사 또는 공급사 출고거점으로 일반화하면 같은 패턴을 사용할 수 있다. 출처: Shopify Help Center, Order routing, 2026년 접근, https://help.shopify.com/en/manual/fulfillment/setup/order-routing

국내 생태계는 사방넷, 샵링커, 플레이오토, 셀메이트 같은 통합관리 솔루션과 공급사별 대량등록 도구가 중심이다. 공개 검색으로는 내부 구현을 검증하기 어려웠지만 공급사 자료는 XML/API 또는 파일을 통해 상품 등록, 가격 변동, 주문과 배송 정보를 통합 솔루션에 전달하는 패턴을 확인시킨다. 그러므로 국내 제품의 차별화 지점은 단순 대량등록이 아니라 데이터 권리, 최신성, 손익, 정책 승인과 AI 기반 예외처리가 되어야 한다. 출처: 스마트웰 공급사 연동 안내 PDF, https://hyunbo.smartwel.co.kr/member/downloadEnterPdf/seller.pdf

### 공통 기능 사슬

제품군은 다음 기능 사슬을 빠짐없이 가져야 한다.

1. Supplier onboarding: 계약, API 또는 파일, 이미지 사용권, 배송 및 반품 SLA를 등록한다.
2. Catalog ingestion: 공급사 원본을 수정하지 않고 스냅샷과 해시를 보존한다.
3. Product normalization: 옵션, 단위, 브랜드, 제조자, 원산지, 인증, 카테고리를 표준화한다.
4. Offer and channel projection: 하나의 canonical product에서 채널별 제목, 상세, 가격, 배송정책을 만든다.
5. Inventory and price sync: 데이터 신선도와 safety stock을 반영해 품절과 역마진을 막는다.
6. Order management: 채널 주문을 내부 주문으로 수집하고 멱등성을 보장한다.
7. Supplier purchase order: 주문 항목을 공급사별로 나누고 승인 또는 자동 발주한다.
8. Fulfillment and tracking: 공급사 송장과 배송상태를 채널에 반영한다.
9. Claims and returns: 소비자 환불과 공급사 환입 또는 크레딧을 별도로 연결한다.
10. Settlement and profit: 채널 정산, 공급사 매입, 광고와 반품 비용을 주문 단위로 대사한다.

## 오픈소스 구현 방식 조사

### Medusa

Medusa는 상품, 주문, 고객, fulfillment, inventory 등을 독립 Commerce Module로 나누며 여러 모듈을 workflow로 조합한다. workflow step은 실패 시 보상 동작을 정의할 수 있어 외부 채널 등록 후 내부 저장 실패 같은 부분 성공을 다루는 참고 모델이 된다. inventory 모듈은 location, reservation, availability를 기본 개념으로 제공한다. 코어는 MIT이지만 저장소 내 RBAC와 SSO 등 명시된 enterprise material은 별도 상용 라이선스가 필요하다. 그대로 채택한다면 파일 단위 라이선스 스캔과 enterprise path 차단이 필요하다. 출처: Medusa, Commerce Modules, https://docs.medusajs.com/learn/fundamentals/modules/commerce-modules ; Medusa, Inventory Module, https://docs.medusajs.com/resources/commerce-modules/inventory ; Medusa GitHub, 2026년, https://github.com/medusajs/medusa ; Medusa Enterprise License, https://github.com/medusajs/medusa/blob/develop/ENTERPRISE-LICENSE.md

### Vendure

Vendure는 API 서버에서 느린 작업을 떼어 별도 Node.js worker가 job queue를 소비한다. 기본 worker는 서버와 같은 구성을 쓰지만 network listener가 없는 standalone NestJS application이고, queue 전략을 교체할 수 있다. 여러 worker 또는 concurrency 증가로 처리량을 키운다. 이 패턴은 채널 API rate limit별 queue, 공급사 sync queue, AI queue를 격리하는 데 적합하다. 코어는 GPLv3이므로 독립 폐쇄형 제품의 핵심 기반으로 직접 포크할 경우 배포 방식과 파생저작물 의무를 별도 법률 검토해야 한다. 구현 패턴은 참고하되 permissive 구성요소와 자체 도메인 코드를 우선하는 편이 안전하다. 출처: Vendure, Worker and Job Queue, https://docs.vendure.io/current/core/developer-guide/worker-job-queue ; Vendure GitHub, https://github.com/vendurehq/vendure

### Saleor

Saleor는 GraphQL only, headless, API only, native multichannel을 표방한다. 외부 앱은 synchronous 또는 asynchronous webhook과 API를 통해 동작하며 구성 UI를 dashboard에 iframe으로 넣을 수 있다. 코어와 주요 SDK의 BSD 3 Clause 라이선스는 상용 독립 제품에 비교적 우호적이다. 그러나 Saleor는 storefront checkout 중심의 commerce engine이므로 한국 오픈마켓 seller operation을 위한 supplier purchase order, 정산 대사와 채널 상태기계는 별도 개발이 필요하다. 출처: Saleor GitHub, https://github.com/saleor/saleor ; Saleor App Template, https://github.com/saleor/saleor-app-template ; Saleor repositories and licenses, https://github.com/orgs/saleor/repositories

### ERPNext

ERPNext의 drop ship 흐름은 고객 Sales Order 항목에 supplier delivers to customer를 표시한 뒤 연결된 Purchase Order를 만든다. 재고 입고 없이 supplier delivery 상태를 기록하고 Sales Invoice와 Purchase Invoice를 각각 생성한다. 이는 내부 주문과 공급사 주문을 하나의 상태로 합치지 말아야 한다는 강한 근거다. ERPNext는 회계와 구매까지 넓게 제공하지만 GPLv3이며 Frappe 운영 복잡도와 국내 채널 어댑터 부재를 감수해야 한다. 독립 제품의 transaction model 참고 또는 별도 ERP 연동 대상으로 두는 것이 낫다. 출처: ERPNext, Drop Ship, https://docs.frappe.io/erpnext/drop-shipping-in-erpnext ; ERPNext GitHub, https://github.com/frappe/erpnext ; Frappe License and Trademark, https://docs.frappe.io/legal/others/license-and-trademark

### Spree

Spree는 REST API, TypeScript SDK, Next.js storefront를 제공하는 headless commerce이며 코어는 BSD 3 Clause다. Ruby on Rails 기반 도입 비용을 수용할 수 있고 자체 storefront도 필요하다면 후보가 된다. 그러나 이 제품의 일차 목표가 판매자 운영 자동화라면 Spree 역시 과도한 checkout 기능과 부족한 supplier orchestration 사이의 불균형이 있다. 출처: Spree GitHub, https://github.com/spree/spree

## 플랫폼 비교와 재사용 결정

| 후보 | 강점 | 부족한 부분 | 라이선스 | 권고 |
|---|---|---|---|---|
| Medusa | 모듈, 재고, 보상 workflow | 국내 판매채널과 공급사 PO | 코어 MIT, 일부 enterprise 별도 | 모듈 또는 패턴 우선 검토 |
| Vendure | worker, queue, plugin, channel | supplier PO와 국내 정산 | GPLv3 | 패턴 참고, 직접 포크 신중 |
| Saleor | API first, webhook app, multichannel | seller OMS와 구매 발주 | BSD 3 Clause | API 및 app 모델 재사용 후보 |
| ERPNext | drop ship PO, 구매, 회계 | 채널 자동화와 제품 UX | GPLv3 | 외부 ERP 연동 또는 모델 참고 |
| Spree | 성숙한 headless commerce | seller operations 특화 부족 | BSD 3 Clause | storefront가 필요할 때 후보 |
| 자체 control core | 한국 채널, 정책, AI를 정확히 반영 | 초기 개발량 | 자체 선택 | 권장 중심축 |

결론은 플랫폼 하나를 제품 전체로 포크하지 않는 것이다. canonical catalog와 seller OMS 및 policy engine을 자체 핵심으로 유지하고 필요할 때 Medusa 또는 Saleor를 storefront commerce capability로 연결한다. 이 경계는 향후 라이선스, 업그레이드, 채널 변화와 모델 공급자 변경을 흡수한다.

## OpenAI 기반 자동운영 아키텍처 조사

### 운영 에이전트의 역할

운영 에이전트는 단발성 텍스트 생성기가 아니라 observe, decide, act, verify 루프를 수행한다. observe 단계에서 판매채널, 공급사, 광고, 정산 이벤트와 KPI를 읽는다. decide 단계에서 목표, 정책, 과거 실험과 현재 예외를 바탕으로 다음 command를 구조화한다. act 단계에서 MCP 또는 function tool을 호출한다. verify 단계에서 채널을 재조회해 실행 결과와 기대 상태를 대사한다. 실패 또는 불일치는 incident와 compensating action으로 전환한다.

| 운영 영역 | AI 판단 | 실행 도구 | 기본 자율 수준 |
|---|---|---|---|
| 상품 발굴 | 수요 신호, 경쟁, 예상 마진 | research, supplier search | 범위 내 자동 |
| 상품 정규화 | 카테고리, 속성, 제목, 설명 | catalog draft | 범위 내 자동 |
| 상품 등록 | 규정과 마진, 채널 표현 | publish offer | 첫 등록 승인 |
| 가격 | 목표 마진과 경쟁 신호 | update price | 변경률 내 자동 |
| 재고 | stale 위험과 safety stock | update stock, pause offer | 자동 |
| 주문 및 발주 | 공급사 routing과 예외 | create purchase order | 공급사별 단계화 |
| CS와 클레임 | 의도, 근거, 답변과 보상 | reply, refund request | 답변 자동, 금전 승인 |
| 정산 | 차이 원인과 재처리 | reconcile, open incident | 자동 분석 |

full auto는 무제한 권한을 뜻하지 않는다. 한 번의 tool call 금액, 하루 누적 예산, 대상 tenant, SKU와 공급사, 변경률, 실행시간, 데이터 신선도, confidence와 anomaly 상태를 policy가 제한한다.

### OpenAI Responses API

Responses API는 텍스트, 이미지, 파일 입력, structured output, function tools와 MCP tools를 지원한다. background mode는 비동기 작업을 시작하고 queued 또는 in progress 상태를 조회할 수 있다. 따라서 cloud operations agent가 장기 상품조사뿐 아니라 tool calling으로 운영 command를 실행하는 기본 경로에 맞는다. response id와 commerce workflow id를 연결해 재시작 후에도 실행을 추적해야 한다. API key는 모바일 앱에 넣지 않고 서버 측 AI gateway에만 저장한다. 출처: OpenAI Docs, Create a model response, https://developers.openai.com/api/reference/cli/resources/responses/methods/create ; OpenAI Docs, Background mode, https://developers.openai.com/api/docs/guides/background

### Codex SDK와 App Server

공식 OpenAI 문서는 Codex SDK를 자동화, 내부 workflow와 애플리케이션 통합에 사용할 수 있다고 설명한다. TypeScript SDK는 서버 측 Node.js 18 이상에서 local Codex thread를 시작, 계속, 재개하며 Python SDK는 local app server를 JSON RPC로 제어한다. 따라서 로컬 PC 또는 전용 worker host에서 Codex를 operations agent로 실행하고 우리 MCP commerce server의 도구만 노출하는 구성이 가능하다. Codex는 상품 조사 파일, 공급사 자료와 운영 runbook을 workspace context로 활용하되 write 권한은 tool gateway를 통해서만 행사한다. 별도의 engineering profile은 connector 코드와 테스트 유지보수에 사용한다. 출처: OpenAI Docs, Codex SDK, https://learn.chatgpt.com/docs/codex-sdk

Codex app server는 인증, thread history, approvals와 agent event streaming이 필요한 rich client를 위한 JSON RPC 인터페이스다. 이를 사용하면 자체 운영 콘솔에서 에이전트 계획, 진행 이벤트, tool 승인 요청과 결과를 스트리밍할 수 있다. 다만 WebSocket transport는 공식 문서상 experimental이며 production workload에 지원되지 않으므로 제품 핵심 자동운영 버스가 아니라 제한된 로컬 또는 관리자용 연결로 취급한다. 출처: OpenAI Docs, Codex App Server, https://learn.chatgpt.com/ko-KR/docs/app-server

### ChatGPT와 모바일 ChatGPT

ChatGPT와 Codex의 plugin은 skill, MCP server, optional UI를 묶을 수 있고 두 제품이 universal plugin directory를 공유한다. 우리 제품의 MCP server가 read tools와 command tools를 제공하면 사용자는 ChatGPT 모바일을 포함한 지원 surface에서 목표 변경, 상품 후보 승인, 가격정책 조정, 재고 위험 처리, 발주 승인과 보고서 생성을 대화로 수행할 수 있다. 동일 MCP server를 Codex operations worker도 사용하므로 자연어 채널이 달라도 실행 규칙은 하나다. side effect가 있는 tool은 명확한 annotation, approval과 confirmation summary를 가져야 하며 capability별 surface 지원은 출시 시 다시 확인한다. 출처: OpenAI Developers, Plugin architecture, https://developers.openai.com/plugins/concepts/plugins ; OpenAI Developers, MCP server, https://developers.openai.com/plugins/concepts/mcp-server

### 독립 모바일 앱

독립 모바일 앱은 AI vendor API가 아니라 우리 control API만 호출한다. 초기에는 responsive PWA로 승인, 알림, KPI, 장애 확인에 집중하고 push notification과 biometric unlock이 필요해지면 React Native 또는 native shell로 확장한다. ChatGPT plugin은 운영 앱의 대체물이 아니라 자연어 보조 채널이다. 동일한 backend command가 web, mobile, ChatGPT에서 들어오므로 command policy와 audit log를 채널과 분리해 한곳에서 적용해야 한다.

## 권장 제품군 경계

### Control Plane

Tenant, user, role, secret reference, policy, approval, budget, feature flag, audit를 관리한다. 모바일, 웹, ChatGPT plugin이 모두 이 계층을 통과한다. 사람 승인을 기다리는 command는 만료시간과 승인 근거를 기록한다.

### Catalog and Offer

SupplierProduct는 공급사 원본이고 CanonicalProduct는 내부 표준 상품이며 ChannelOffer는 채널별 판매 표현이다. 이 세 모델을 분리해야 공급사 제목이나 옵션이 바뀌어도 채널에서 수작업 보정한 내용을 잃지 않는다. 원본 snapshot과 transformation lineage를 보존한다.

### Inventory and Pricing

재고 수량뿐 아니라 observed at, expected refresh interval, confidence, safety stock을 저장한다. 가격은 원가 snapshot, 채널 수수료, 광고충당, 반품충당, 최소 공헌이익과 변경 상한을 이용한 결정론적 계산이다. AI가 가격을 제안할 수는 있으나 정책 엔진이 최종 결정을 내린다.

### Order and Procurement

ChannelOrder와 SupplierPurchaseOrder를 분리하고 order line별 routing decision을 둔다. 상태 전이는 event log로 보존하며 create purchase, cancel, refund, resend는 idempotency key를 필수로 한다. 부분주문, 공급사 분할, 부분취소와 송장 다중화를 처음부터 모델링한다.

### Claim and Settlement

소비자 claim, 채널 claim, 공급사 return 또는 credit을 연결하되 상태를 합치지 않는다. SettlementLine은 채널 원장과 공급사 비용, 광고비를 주문 항목에 배분한다. profit projection과 realized profit을 분리한다.

### Agent Runtime and Tool Gateway

Agent runtime은 목표와 정책을 받은 operations run을 생성하고 이벤트를 관찰해 다음 행동을 계획한다. 모든 AI task는 prompt version, input digest, model, structured decision, confidence, tool call, reviewer, cost와 검증 결과를 기록한다. Tool Gateway는 catalog, offer, inventory, purchase, claim, settlement command를 typed schema로 제공하며 권한, 정책, 멱등성과 예산을 검사한다. Codex와 Responses agent는 원장 DB에 직접 접근하지 않고 이 도구를 통해 실제 상거래를 운영한다.

### Engineering Worker

별도 Codex profile은 repository, requested change, sandbox, approval, commit 또는 diff artifact를 기록하며 connector 개발과 테스트 유지보수를 수행한다. Operations agent와 engineering agent는 자격증명과 tool allowlist를 공유하지 않는다.

## 논리 아키텍처 후보

```text
Web Admin     Mobile PWA     ChatGPT Plugin
    |             |              |
    +-------------+--------------+
                  |
          API Gateway and BFF
                  |
        Control Plane and Policy
                  |
  +---------------+------------------+
  | Catalog | Inventory | OMS | Claim |
  | Pricing | Procurement | Settlement|
  +---------------+------------------+
                  |
        PostgreSQL plus Outbox
                  |
       Workflow and Queue Layer
       |        |        |       |
   Channel   Supplier   AI Gateway and Agent Runtime
   Adapters  Adapters   Responses API or Codex SDK
                            |
                      Engineering Codex
```

이 그림은 확정 아키텍처가 아니라 조사에서 도출한 후보 경계다. 다음 단계에서 예상 주문량, 공급사 수, 채널 수, team capability와 배포 환경을 넣어 ADR로 확정해야 한다.

## 워크플로와 큐 선택

단순 상품 동기화와 이메일은 BullMQ 같은 Redis 기반 queue로 충분하다. BullMQ는 distributed job execution, retry와 job state를 제공하며 MIT 라이선스다. 반면 주문에서 발주, 품절 대체, 송장, 부분취소, 환불로 이어지는 수일짜리 프로세스는 process restart 후에도 timer와 state를 복원해야 하므로 Temporal 같은 durable workflow가 유리하다. Temporal server는 MIT 라이선스다. 초기 팀이 작으면 PostgreSQL outbox와 하나의 queue로 시작하되 도메인 event contract를 Temporal로 옮길 수 있게 설계한다. 출처: BullMQ documentation, https://docs.bullmq.io/ ; Temporal GitHub license, https://github.com/temporalio/temporal/blob/main/LICENSE

관측성은 OpenTelemetry Collector를 사용해 trace, metric, log의 vendor lock in을 줄일 수 있고 Apache 2.0 라이선스다. 인증은 처음부터 Keycloak을 도입할 필요는 없지만 다중 tenant, SSO와 세밀한 권한이 요구되면 Apache 2.0 기반 Keycloak이 후보가 된다. 출처: OpenTelemetry Collector GitHub, https://github.com/open-telemetry/opentelemetry-collector ; Keycloak GitHub, https://github.com/keycloak/keycloak

## 필수 설계 원칙

- 모든 외부 write는 idempotency key, timeout, retry policy와 reconciliation job을 가진다.
- API webhook은 사실 통지로 보고 원장을 다시 조회하거나 signature 및 replay를 검증한다.
- supplier 또는 channel adapter는 anti corruption layer로 격리한다.
- 원본 데이터, 정규화 데이터, 채널 투영 데이터를 덮어쓰지 않고 lineage로 잇는다.
- 상태 변경은 현재 행과 append only event를 함께 보존한다.
- AI 출력은 schema validation, deterministic rule, human approval을 통과해야 한다.
- PII와 API secret을 prompt 또는 Codex workspace에 기본 전달하지 않는다.
- 모바일과 ChatGPT는 별도 권한 체계를 만들지 않고 동일 command authorization을 사용한다.
- 자동화 수준은 observe, suggest, approve, bounded auto, full auto 단계로 올린다.

## 저토큰 CLI 개발 팩토리 조사

### 이 PC의 AIOS 방식에서 확인한 구조

분석한 로컬 AIOS 구현은 LLM이 아닌 Python orchestrator가 task queue, dependency, pool capacity, retry, guard, QA와 review chain을 관리한다. 작업 상태는 대화에 누적하지 않고 `tasks/task-<id>.json` 파일에 짧게 보존한다. PM과 worker 사이 메시지는 task id 하나만 전달하고 실제 status, commit, tests, note, decision은 같은 JSON 파일을 덮어써서 교환한다. 프로세스가 재시작되면 in progress task를 assigned로 되돌려 재배정한다.

PM은 고성능 모델, 구현과 QA worker는 상대적으로 저렴한 모델로 분리되어 있다. pool 설정은 backend, frontend, QA, reviewer 역할별 크기와 모델을 지정하고 메모리 여유가 낮으면 spawn을 중단한다. idle 또는 needs decision 및 escalation이 있을 때만 PM cycle을 깨우며 cooldown을 둔다. 이는 PM 모델이 모든 구현 토큰을 소비하지 않고 결정과 분해에만 개입하게 한다.

worker runner는 작업마다 격리된 git worktree와 선택적 테스트 DB를 준비하고, task JSON, role prompt, protocol과 정확한 spec 및 file scope를 stdin prompt로 전달한다. worker 완료 후 task 상태 갱신과 commit의 origin main 포함 여부를 기계적으로 검증한다. 구현 완료 뒤 deterministic guard, QA task, independent review task를 순서대로 자동 생성한다. immutable policy와 architecture 및 security guard는 worker와 PM 밖의 별도 control plane에 두는 구조다.

이 부분은 공개 웹 자료가 아니라 사용자가 지정한 로컬 AIOS의 protocol, pool, orchestrator, worker runner 및 PM 역할 문서를 읽기 전용으로 분석한 결과다. 공개 저장소에는 개인 PC 경로를 포함하지 않는다.

### 이 제품 개발에 적용할 구조

```text
Human Product Owner
        |
Chief Architect and PM CLI
  ADR, backlog, dependency, acceptance
        |
Deterministic Orchestrator
  queue, leases, retry, budget, pool, rate limit
        |
  +-----+---------+---------+----------+
Backend Workers  Connector  Mobile UI  QA and Review
  +-----+---------+---------+----------+
        |
Worktrees plus scoped context packets
        |
Guards, tests, contract replay, license scan
        |
Integration branch and release candidate
```

개발 PM과 worker는 Codex CLI 또는 Codex SDK로 교체 가능하게 runner adapter를 둔다. task schema와 orchestrator는 모델 공급자에 독립적이어야 하며 `runtime: codex | claude`, `model`, `reasoning`, `token_budget`, `max_turns`를 task 또는 pool에서 선택한다. Codex 공식 SDK가 local thread 시작, 계속과 재개를 지원하므로 장기 worker는 thread id만 상태 파일에 남기고 필요한 때 재개할 수 있다. 출처: OpenAI Docs, Codex SDK, https://learn.chatgpt.com/docs/codex-sdk

### 토큰 최소화 규칙

1. PM은 요구사항 분해, ADR 충돌, blocker와 merge 판단 때만 호출한다. queue 관리와 상태 전이는 코드가 한다.
2. worker에게 전체 대화나 전체 문서를 보내지 않고 task JSON, 관련 spec anchor, 허용 파일, acceptance와 실패 증거만 보낸다.
3. repo map, interface contract와 decision summary를 작은 versioned artifact로 유지하고 원문 대신 digest와 변경분을 전달한다.
4. 탐색 worker와 구현 worker를 분리한다. 이미 확보한 조사 결과를 구현 worker가 다시 조사하지 않는다.
5. 역할별 저비용 모델을 기본으로 하고 architecture, security 또는 반복 실패에만 상위 모델로 escalation한다.
6. test output과 logs는 전체를 prompt에 넣지 않고 failing test, 마지막 오류와 artifact path만 전달한다.
7. task 하나는 좁은 file ownership과 한 개의 검증 가능한 결과를 가지며 완료 보고는 commit, tests, note 한 줄로 제한한다.
8. 동일 prefix와 role instruction을 안정적으로 유지해 prompt caching을 활용할 수 있게 한다.
9. worker별 max turns와 token budget을 task 난이도에 따라 다르게 두고 무진행 turn을 감지해 종료한다.
10. QA와 review는 전체 구현 대화를 상속하지 않고 commit diff, spec과 test evidence만 읽는다.

### 현재 AIOS 방식에서 보완할 점

현재 runner는 매 task마다 전체 protocol과 role prompt를 합쳐 전달한다. 이들은 안정 prefix로 유지하거나 저장소 규칙으로 이동하고 task delta만 뒤에 붙여 캐시 효율을 높여야 한다. 모든 worker에 동일한 `max_turns: 150`을 주는 대신 small, medium, large task class별 budget을 둔다. task 파일 수가 커질수록 전체 디렉터리 scan 비용이 증가하므로 active index 또는 SQLite queue를 추가하되 JSON을 human readable projection으로 유지하는 방안이 적합하다.

또한 로컬 Claude runner의 `--dangerously-skip-permissions` 방식은 격리 worktree가 있어도 제품 개발 기본값으로 복제하지 않는다. Codex runner는 workspace write sandbox, command allowlist와 외부 meta guard를 기본으로 하고 위험 명령은 사람 승인으로 남긴다. PM과 worker가 control plane을 수정할 수 없도록 별도 저장소, CODEOWNERS와 CI 권한으로 보호하는 AIOS 원칙은 그대로 유지한다.

## 오픈소스 제품화 원칙

독립 제품의 자체 코어는 Apache 2.0 또는 상업용 친화 라이선스로 배포할 수 있다. 외부 의존성은 SBOM과 license allowlist를 CI에서 검사한다. MIT, BSD 3 Clause, Apache 2.0 구성요소는 NOTICE와 저작권 표시 의무를 이행하며 사용할 수 있지만 각 저장소의 enterprise 경로와 trademark policy를 별도 확인한다. GPLv3 구성요소는 별도 프로세스 또는 외부 연동으로 경계를 두는 것만으로 모든 의무가 자동 해소된다고 단정하면 안 되며 배포 전 법률 검토가 필요하다.

제품군 제안은 다음과 같다.

1. Store Core: canonical catalog, offer, OMS, procurement, claim, settlement.
2. Connector Kit: channel 및 supplier adapter SDK, contract tests, replay fixtures.
3. Ops Console: web 및 mobile PWA, approval inbox, incident timeline.
4. AI Operations: Responses API 또는 Codex SDK 기반 observe decide act verify 자동운영.
5. Commerce MCP: Codex와 ChatGPT가 함께 쓰는 조회 및 command tool gateway.
6. ChatGPT Plugin: 모바일 대화 지시, 승인, 경보 처리와 운영 보고.
7. Engineering Worker: 격리된 Codex profile 기반 connector maintenance와 test automation.

## 단계별 검증 계획

### Discovery Gate

스마트스토어 한 채널과 공급사 한 곳의 실제 API 또는 파일 샘플을 확보한다. 상품 100개, 주문 및 취소 샘플, 정산 파일을 가지고 canonical model이 손실 없이 round trip되는지 검증한다. 통과 조건은 필수 필드 손실 0건, 주문 중복 0건, 모든 외부 ID 역추적 가능이다.

### Shadow Gate

실제 계정을 읽기 전용으로 연결해 기존 운영과 병행한다. 시스템의 재고, 가격, 주문, 예상 정산과 실제 운영 결과를 비교한다. write는 하지 않는다. 통과 조건은 재고 freshness와 주문 수집 SLA 달성, 설명 가능한 차이율, exception queue의 처리 가능성이다.

### Assisted Gate

상품 등록, 가격 변경과 발주를 승인 후 실행한다. 승인 화면에는 변경 전후, 예상 손익, 근거 데이터 시각, rollback 가능성을 표시한다. 통과 조건은 멱등성 오류 0건과 승인부터 실행까지 완전한 audit다.

### Bounded Automation Gate

금액, SKU, 공급사, 시간대와 신뢰 점수 한도 안에서만 자동화한다. 비정상 주문량, 공급가 급등, 데이터 stale, API 오류율 증가 시 circuit breaker로 자동 정지한다.

## 아키텍처 확정 전에 필요한 결정

1. 제품이 단일 사업자용인지 다중 tenant SaaS인지 결정한다.
2. 첫 채널과 공급사의 공식 API 또는 파일 접근 조건을 확정한다.
3. 자사몰 checkout이 범위인지 오픈마켓 seller operation만 범위인지 정한다.
4. 예상 SKU, 주문량, 동기화 주기와 허용 stale window를 수치화한다.
5. 자동발주, 가격변경, 환불의 금액 및 사람 승인 한도를 정한다.
6. ChatGPT plugin을 내부 운영용으로 제한할지 외부 고객에게 공개할지 정한다.
7. 독립 제품의 소스 공개와 상용 라이선스 전략을 정한다.

## 권고 ADR 목록

- ADR 001 Product boundary: seller operations core와 storefront 분리
- ADR 002 Canonical data model: source, canonical, projection 분리
- ADR 003 Transaction and event: PostgreSQL, outbox, idempotency
- ADR 004 Workflow: queue only와 durable workflow 도입 기준
- ADR 005 Connector contract: polling, webhook, rate limit, replay
- ADR 006 Agent loop: observe decide act verify와 run recovery
- ADR 007 Tool authority: MCP command schema, policy, approval와 예산
- ADR 008 Runtime: Responses cloud agent와 Codex local agent 선택
- ADR 009 Client surfaces: web, mobile PWA, ChatGPT MCP
- ADR 010 Operations agent와 engineering agent 격리
- ADR 011 Tenancy and authorization
- ADR 012 Open source license and distribution
- ADR 013 Development factory: CLI PM, deterministic orchestrator와 worker pools
- ADR 014 Context economy: task packet, cache prefix, model routing과 token budget
- ADR 015 Development meta control: immutable guards and human only changes

## 한계와 추가 조사

상용 SaaS의 내부 소스와 정확한 운영 아키텍처는 공개되지 않아 공개 기능, 도움말과 API 설명으로 행동을 추론했다. 국내 통합솔루션은 공개 기술 문서가 제한적이므로 실제 계약 데모, API 명세와 장애 SLA 확인이 필요하다. OpenAI 기능과 지원 surface는 변경될 수 있어 구현 직전 공식 문서를 다시 확인해야 한다. 이 보고서는 오픈소스 라이선스 법률 의견이 아니며 GPL 및 상용 배포 경계는 전문가 검토가 필요하다.

## 조사 종료 기준

상용 드롭시핑 제품의 기능 사슬, 네 개 이상의 오픈소스 구현 패턴, OpenAI의 세 가지 통합 경로, 모바일 경계, 라이선스와 단계별 검증 항목에 1차 출처가 확보되었다. 추가 검색은 특정 공급사와 채널이 정해지기 전에는 같은 일반론을 반복할 가능성이 높아 여기서 멈춘다.

## Claim to source ledger

| Claim family | Primary source | Publisher | Date or access | URL | Confidence |
|---|---|---|---|---|---|
| scheduled inventory monitoring | Product monitoring | AutoDS | 2026-09-04 | https://help.autods.com/en/articles/12699906-product-monitoring-configure-price-and-stock-updates-preferences | High |
| order verification and routing | Order Manager guide | Inventory Source | 2026-09-04 | https://help.inventorysource.com/article/123-order-manager-getting-started-guide | High |
| compensating workflows and inventory | Medusa docs | Medusa | 2026-09-04 | https://docs.medusajs.com/resources/commerce-modules/inventory | High |
| worker and job queue | Vendure docs | Vendure | 2026-09-04 | https://docs.vendure.io/current/core/developer-guide/worker-job-queue | High |
| API first and webhook apps | Saleor repository | Saleor | 2026-09-04 | https://github.com/saleor/saleor | High |
| drop ship sales to purchase order | ERPNext docs | Frappe | 2026-09-04 | https://docs.frappe.io/erpnext/drop-shipping-in-erpnext | High |
| Responses background and tools | Responses API docs | OpenAI | 2026-09-04 | https://developers.openai.com/api/reference/cli/resources/responses/methods/create | High |
| Codex automation boundary | Codex SDK docs | OpenAI | 2026-09-04 | https://learn.chatgpt.com/docs/codex-sdk | High |
| rich client app server | Codex App Server docs | OpenAI | 2026-09-04 | https://learn.chatgpt.com/ko-KR/docs/app-server | High |
| ChatGPT and Codex plugin shape | Plugin architecture | OpenAI | 2026-09-04 | https://developers.openai.com/plugins/concepts/plugins | High |
| workflow license | Temporal license | Temporal | 2026-09-04 | https://github.com/temporalio/temporal/blob/main/LICENSE | High |
| telemetry and identity | OpenTelemetry and Keycloak repos | CNCF and Keycloak | 2026-09-04 | https://github.com/open-telemetry/opentelemetry-collector | High |
