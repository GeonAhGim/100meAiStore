# P2-02 offline channel request/error contract evidence

Verified 2026-09-06. Implementation: `packages/store_core/channel_contracts.py`.
Sources and broader unfinished contracts: [Phase 2 backlog](live-phase2-backlog.md).

`coupang_day_page_plan` creates a KR GET ordersheet plan with tenant/connection
references, day range, state, page size and an optional opaque continuation.
It has no URL opener, HTTP client, auth header, credential parameter, executor,
database binding or channel capability registration. Mode is OFFLINE_CONTRACT
and live_authorized is false. This is not a usable vendor connection.

The documented [day-paging endpoint](https://developers.coupang.com/ko/api/shipments/po-list-query-paging-by-day)
informs the request path, six states, date offset, continuation and page limit.
Client policy conservatively counts at most 31 inclusive calendar dates.
Local identifier grammar and 4096-character cursor limit are implementation
bounds, not claimed vendor schema restrictions. Query encoding round-trips
literal '+', '/', '=', '&' in synthetic cursors; date offsets are encoded once.
Opaque values and tenant references are omitted from the plan's default repr.

`classify_naver_read_error` uses exact status/code pairs from official
[authentication](https://apicenter.commerce.naver.com/docs/auth) and
[restrictions](https://apicenter.commerce.naver.com/docs/restriction): refresh
required, rate-limit review, quota review or manual review. It never refreshes
or retries, and does not retain vendor messages. Even a recognized code paired
with a different HTTP status goes to manual review.

All fixture values are authored synthetic placeholders. Only documented error
codes/request parameter names are modeled; there is no claim of full response
schema compatibility, account permission or end-to-end channel verification.

## Verification

- Targeted contract tests: 5 passed.
- Full suite: 126 passed in 12.80s.
- `python -m compileall -q packages smart_store_aios tests`: passed.
- `git diff --check`: passed.
- Tracked/untracked source filename check: no forbidden files (`.env.example`
  explicitly allowed); repository key-pattern scan: no matches. This is a
  pattern-based check, not a guarantee against every possible sensitive value.
- Tests deny socket construction while creating plans/classifying fixtures;
  malformed values, 31/32-day boundary, page count and no-secret repr checked.

No schema migration or existing DEMO behavior changed. Rollback consists of
removing the unused scaffold; no operational data requires rollback.

P2-02 completion makes tracked overall progress 13/23 (57%); DEMO 11/11 (100%);
Phase 2 2/12 (17%). This is package-count progress, not real-channel readiness.
Next safe work is P2-03 full official order schema inspection and fixture
normalization; P2-05 file/manual work can also proceed without credentials.
P2-04 retains auth vectors, response-header budgets and actual cursor persistence.

Publishing status: the P2-01 push was rejected by automatic approval review
because the external GitHub destination/default-main payload was not explicitly
approved. This packet and code are local until that gate is resolved; no
alternate push path is used.
