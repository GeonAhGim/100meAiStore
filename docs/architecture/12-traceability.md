# Traceability ledger

## Requirements to architecture

The source requirements are treated as seven requirement blocks `R-01`–`R-07`, corresponding to the seven titled sections. The numbered architecture deliverables are tracked separately as `D-01`–`D-11`.

| Requirement IDs | Covered by |
|---|---|
| R-01 scope and priority | D-01, D-04, D-05, D-10 |
| R-02 multi-tenancy and permissions | D-02, D-03, D-08 |
| R-03 approvals, schedules, safety, alerts | D-03, D-04, D-06, D-07, D-09 |
| R-04 money and profit | D-02, D-03, D-04, D-07, D-09 |
| R-05 product, content, CS | D-02, D-03, D-04, D-05, D-06 |
| R-06 AI, tokens, external automation | D-05, D-06, D-08, D-09, D-11 |
| R-07 cloud, app, development, DEMO/LIVE | D-01, D-07, D-08, D-09, D-10, D-11 |
| D-01 context/containers/topology | D-01 |
| D-02 data model/ownership/retention | D-02 |
| D-03 RBAC/approval matrix | D-03 |
| D-04 state machines/compensation | D-04 |
| D-05 adapter contracts | D-05 |
| D-06 agents/tools/BYOK/budget/questions | D-06 |
| D-07 PWA UX/approval screen | D-07 |
| D-08 security/privacy/audit/threat/stop | D-08 |
| D-09 cost/observability/backup/DR | D-09 |
| D-10 validation/scenarios/SLO/gates | D-10 |
| D-11 ADR/risk/backlog/acceptance | D-11 |

## Report findings to decisions

| Source ID | Finding used | Artifact |
|---|---|---|
| S-01 | scheduled monitoring has freshness delay | D-02, D-04, D-10 |
| S-02 | staged order verification is safer | D-03, D-10 |
| S-03 | routing by location/cost generalizes to supplier routing | D-04 |
| S-04–S-08 | source/canonical/projection, worker, API-first, PO separation patterns | D-01, D-02, D-04, D-05 |
| S-09–S-12 | observe-decide-act-verify, Responses/Codex/MCP/mobile boundaries | D-01, D-05, D-06, D-07 |
| S-13 | queue vs durable workflow and restart behavior | D-01, D-04, D-09 |
| S-14 | deterministic orchestrator and scoped context packets | D-06, D-11 |
| S-15 | license/SBOM and GPL/fair-code review requirement | D-08, D-11 |

`S-*` labels are package-local aliases for relevant findings in `docs/report-source.md`; no private local research paths are reproduced here.

## Validation coverage

| Gate/scenario | Evidence document |
|---|---|
| Discovery, Shadow, Assisted, Bounded | D-10 |
| duplicate/replay/timeout/restart | D-04, D-05, D-10 |
| cross-tenant, secret, prompt, deletion threats | D-02, D-08, D-10 |
| cost cap, alert fallback, restore | D-09, D-10 |
| unresolved decisions and implementation acceptance | README, D-11 |
