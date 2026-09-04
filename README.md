# 100meAiStore

스마트스토어와 쿠팡을 우선 지원하는 AI 기반 무재고 위탁판매 운영 플랫폼입니다. 현재 단계는 **아키텍처 기준선 확정**이며 실제 상품 등록, 발주, 결제는 활성화하지 않습니다.

## 현재 상태

- 확정 요구사항: [docs/100meAiStore-requirements-v1.md](docs/100meAiStore-requirements-v1.md)
- 쉬운 아키텍처 설명: [docs/architecture/00-summary-ko.md](docs/architecture/00-summary-ko.md)
- 구현용 전체 아키텍처: [docs/architecture/README.md](docs/architecture/README.md)
- 기존 `smart_store_aios` Python 코드는 초기 정책·큐 실험용 prototype이며 새 아키텍처의 구현 기준이 아닙니다.

## 제품 목표

`주 500만 원`은 보장 수익이 아니라 **주간 공헌이익 목표**입니다. 시스템은 매출 대신 아래 금액을 추적합니다.

`공헌이익 = 판매가 - 공급가 - 배송비 - 기타 변동비 - 플랫폼 수수료 - 광고비 - 반품충당금`

예시 기본값에서 판매가 50,000원, 공급가 25,000원, 배송비 3,000원이면 주문당 공헌이익은 11,500원이고, 주 500만 원을 위해 약 435건/주가 필요합니다. 고정비·부가세·소득세/법인세 전 수치이므로 실제 목표에는 별도 반영해야 합니다.

## 목표 운영 구조

```text
공급사 피드 -> catalog.scan -> 규정/마진 심사 -> 승인 큐 -> listing.publish
                                              -> inventory.sync
판매채널 주문 -> orders.sync -> 공급사 발주 -> 송장 동기화 -> 클레임/정산
이벤트/정산 ----------------------------------------------> profit.snapshot
OpenAI 관리자 에이전트 ---------------------------> 승인안·예외·운영 판단
Codex CLI 개발 워커 ------------------------------> 코드·테스트·리뷰
```

SQLite 큐는 작업 임대(lease), 재시도, dead 상태, 감사 로그를 제공합니다. 여러 프로세스가 같은 DB를 사용해 작업을 가져갈 수 있습니다.

## 기존 prototype 실행

```powershell
Copy-Item config.example.json config.json
python -m smart_store_aios.cli init
python -m smart_store_aios.cli economics --price 50000 --cost 25000 --shipping 3000
python -m smart_store_aios.cli enqueue catalog.scan '{"source":"demo"}'
python -m smart_store_aios.cli worker --once
python -m smart_store_aios.cli status
python -m unittest discover -v
```

Codex 작업은 `config.json`의 `codex.enabled`를 명시적으로 `true`로 바꾼 뒤에만 실행됩니다. 워커는 `codex exec`를 비대화형으로 호출하고 마지막 메시지를 파일로 보존합니다. 무인 실행에서 `--dangerously-bypass-approvals-and-sandbox`는 사용하지 않습니다.

## 아키텍처 원칙

- 사업자별 데이터·API 키·비용을 분리한다.
- AI는 데이터베이스, 채널 키, 결제수단에 직접 접근하지 않는다.
- 외부 실행은 typed command, 정책, 승인, 멱등성, 감사, 결과 검증을 거친다.
- 발주·신규 상품·환불·공급처 교체는 사람의 승인을 받는다.
- 정보가 불명확하면 추측하지 않고 질문하며 손실 위험은 먼저 임시 정지한다.
- API 키, 개인정보, 사업자 문서와 운영 데이터는 공개 저장소에 저장하지 않는다.

상세 조사와 단계별 로드맵은 [docs/research-report.md](docs/research-report.md)에 있습니다.
