# D-11. ADRs, risks, backlog, dependencies, acceptance criteria

## ADR register

| ADR | Decision | Status / condition |
|---|---|---|
| ADR-001 | seller-operations core separate from storefront checkout | accepted; checkout out of scope |
| ADR-002 | source/canonical/channel projection separation | accepted |
| ADR-003 | PostgreSQL ledger + outbox + idempotency + reconciliation | accepted |
| ADR-004 | queue for short jobs; durable workflow when multi-day recovery/timers justify it | conditional; volume/ops gate |
| ADR-005 | adapters isolate vendor schemas; polling/webhook/file/manual contract | accepted; official access TBD |
| ADR-006 | observe-decide-act-verify agent loop with durable run state | accepted |
| ADR-007 | typed tool gateway is sole side-effect authority | accepted |
| ADR-008 | Responses cloud agent primary; Codex/local runtime optional | conditional; provider capability recheck |
| ADR-009 | web + Android PWA + same control API; ChatGPT as auxiliary surface | accepted; surface support recheck |
| ADR-010 | operations and engineering agents use separate identities/allowlists | accepted |
| ADR-011 | tenant isolation, tenant-scoped secrets/cost/audit, membership RBAC | accepted; legal retention TBD |
| ADR-012 | core avoids GPL dependency; SBOM/license/legal review | pending publication strategy |
| ADR-013 | deterministic development orchestrator; scoped task packets/workers | accepted as development architecture |
| ADR-014 | context economy: stable prefix, delta context, budgets, escalation | accepted |
| ADR-015 | immutable guards/control plane and human-only policy changes | accepted |

## Open risks and owners

| Risk | Impact | Mitigation / exit evidence | Owner |
|---|---|---|---|
| official Naver/Coupang/supplier access or terms unknown | blocks LIVE writes | DEC-01 + adapter contract test | product/connector |
| stale windows and volume unknown | unsafe automation/cost | DEC-02 + Shadow evidence | product/ops |
| refund/price/purchase bounds unknown | over-spend or policy violation | DEC-04; approvals remain mandatory | master/product |
| Korean channel semantics change | state mismatch | versioned adapter + reconciliation | connector |
| PII/retention obligations unknown | legal exposure | DEC-07 legal review and purge test | security/legal |
| GPL/fair-code distribution risk | commercial block | DEC-06 counsel/license scan | product/legal |
| initial public-repository license is not selected | distribution ambiguity | DEC-06; keep license file absent until approved, but ship SBOM/NOTICE process | product/legal |
| cloud minimum cost exceeds envelope | service interruption | cost telemetry and optional-work kill switch | platform |
| provider surface/API changes | integration break | implementation-time official-doc check and contract tests | AI/platform |

## Implementation backlog (dependency ordered)

| ID | Slice / acceptance criterion | Depends on |
|---|---|---|
| B01 | tenant, membership, session revocation, policy version, audit | DEC-07, ADR-011 |
| B02 | canonical/source/projection schemas and lineage; source hash retained | B01, ADR-002 |
| B03 | DEMO channel/supplier fixtures and adapter contract harness | B02, DEC-01 |
| B04 | inventory/price calculation and <10% guard | B02, DEC-02/04 |
| B05 | order routing, separate PO, idempotency, restart recovery | B03, ADR-003 |
| B06 | approval inbox and mobile contract; re-check on submit | B01, DEC-04 |
| B07 | claims/settlement import and projected vs realized profit | B05, DEC-03 |
| B08 | tool gateway, agent run ledger, BYOK and budget stop | B01, B04–B07 |
| B09 | notification fallback and incident acknowledgement | B01, B08 |
| B10 | stop/recovery, backup/restore, threat tests, cost cap | B01, B05, B09 |
| B11 | Discovery → Shadow → Assisted → Bounded evidence package | B03–B10 |

## Definition of done for each slice

Schema/contract versioned; tenant isolation tested; state/event/audit evidence present; idempotency and unknown-result behavior tested; retry and reconciliation documented; DEMO fixture exists; no secret/PII in repo/artifacts; acceptance evidence linked; operational rollback/compensation documented.
