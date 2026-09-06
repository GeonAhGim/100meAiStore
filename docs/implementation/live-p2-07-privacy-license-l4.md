# P2-07 L4: privacy/license review packet and deletion-free dry run

Status: offline review packet completed 2026-09-06. Applicable decisions are DEC-06,
DEC-07, ADR-011/012 and D-08. No license or retention policy is being approved.

## Data flow and unresolved policy inventory

| Data class | Intended source → consumer | Sensitive values / required review | Current retention decision |
|---|---|---|---|
| channel_raw | channel read → parser/source vault | purchaser/contact/address; vendor terms and processing role | unresolved; no real sample |
| supplier_raw | permitted file/API → catalog vault | supplier contact/business terms, source reuse rights | unresolved; synthetic files only |
| orders | normalized source → routing/approval | buyer/address separated; exact-ID joins and role masking | unresolved |
| finance | channel/ledger → reconciliation | payment/account information; tax record obligations | unresolved |
| audit | command/outbox → restricted audit reader | actors, evidence links; legal hold and tamper resistance | unresolved |
| sessions | identity provider → control API | session tokens/device metadata; revoke/expiry constraints | unresolved production identity |
| content_candidates | synthetic catalog → draft review | source rights and unapproved publication | D-02 mentions 10-day expiry; no deletion authority |

Official law starting points and effective-date uncertainty are in
[P2-01 evidence register](live-phase2-backlog.md). Owner/legal must verify dated
consolidated text, controller/processor roles, third parties, permitted purpose,
cross-border transfers, retention, holds and incident obligations. This packet
does not decide whether a particular statute or period applies to the business.

## Dry-run contract

Only opaque synthetic record references, class, timestamp and hold flag enter
the planner. Policy durations require an explicit fixture approval digest;
missing/invalid policy returns REVIEW_POLICY. Legal hold always returns RETAIN.
Expired records can only become ELIGIBLE_FOR_REVIEW; deletion_authorized is
always false and no filesystem/database deletion function exists. Masked export
contains only allowlisted metadata and never copies arbitrary payload fields.
Tests cover policy missing, hold, exact age boundaries, invalid/future timestamps,
unknown classes, masked extra fields and a fixture deletion callback that is
never invoked. No operational data or legal record is deleted.

## Dependency/license inventory

The draft BOM records this repository's known direct build dependency and the
separate locally used bcrypt validation dependency. It is not a production lock
or proof of transitive dependency coverage. Metadata was read locally from the
installed distributions: setuptools 80.9.0 (MIT), bcrypt 4.3.0 (Apache-2.0).
Core runtime currently declares no third-party dependencies in pyproject.toml;
bcrypt is injected and used only for a local verification gate, not imported by
production code. Python/OS, deployment images, web UI and future PostgreSQL
drivers require final release inventory. No new package was installed.

`live-phase2-bom.json` is a draft CycloneDX inventory; unresolved/transitive scope
is explicit in properties. `live-phase2-NOTICE.md` records review obligations,
not a product license grant. Do not add a root LICENSE file without DEC-06.

Acceptance: inventory/NOTICE and gate evidence exist; all policies unresolved in
the real project; deterministic dry-run tests, full gates and local commit.
Real legal approval and production-complete SBOM remain G3/G5/P2-09–P2-12.

## Evidence

Five targeted tests and full suite 165 passed; compileall/diff/key-pattern and
forbidden-filename checks passed. Draft BOM JSON parses; full CycloneDX schema
validation and transitive/distribution inventory remain release work, and the
artifact explicitly labels itself incomplete. No root license was added.

`release_review.py` performs only metadata masking and fixture retention review.
Tests patched deletion and socket functions to fail on invocation; none were
called. Holds override expiry; missing policy and malformed fixture approval
stay unresolved. No test approval digest grants legal or operational authority.
Next safe slice: P2-09 local production-readiness infrastructure/PWA gap review.
