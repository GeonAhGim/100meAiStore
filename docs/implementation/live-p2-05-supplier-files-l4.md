# P2-05 L4: supplier CSV and manual fixture contract

Status: offline scope completed 2026-09-06. Source syntax: [RFC 4180](https://www.rfc-editor.org/info/rfc4180/).
Business scope: D-05 supplier adapter. No supplier was selected, contacted or
represented as available. This versioned UTF-8 CSV is our local interchange
format, not a guessed vendor format. Real sample mapping remains G2/P2-10.

## Design

Pure parser accepts bounded CSV bytes (optional UTF-8 BOM), explicit header
names and integer KRW cost/stock with timezone-aware observation. Preserve
supplier SKU strings (including leading zeros), exact identity fields and raw
file SHA-256. Every normalized row has logical record number and source line
range. Reject duplicate columns, duplicate supplier SKUs, malformed quoting,
unsupported currency/types, control characters and spreadsheet-formula cells.
Errors contain fixed codes and row references, never raw cell values.

Return an immutable import report; any error makes the whole report not ready.
No parser mutates the catalog or reinterprets failed rows as successful import.
Reparsing identical bytes yields the same report and row identities. XML and
Excel are explicitly unsupported until a supplier-specific schema/library and
entity/macro/zip-bomb handling are reviewed; do not sniff them into CSV.

Manual tasks are deterministic local instructions identified by scope, request
digest and idempotency key. They start AWAITING_HUMAN. A manual confirmation
requires an explicit evidence digest and expected version; status becomes
HUMAN_REPORTED, not provider-confirmed. Repeating the same evidence is idempotent;
conflicting evidence/version is rejected. No submit/write/network method exists.

Acceptance: quoted commas/newlines, BOM, leading-zero IDs, numeric/time errors,
formula cells, duplicate/oversized/malformed input, masked reports, stable
reparse, manual no-false-success/replay/version checks. Run full gates and local
commit; do not push. Integration of real samples/fulfillment remains G2/G4.

## Evidence and limits

`supplier_file_contracts.py` implements the pure CSV/report and manual-task
contracts. Eight targeted tests pass; full suite 144 passed. compileall, diff
check, key-pattern scan and forbidden-filename check pass. A Python 3.10 test
fixture writer rejected embedded NUL; the test now injects raw NUL bytes and
proves the parser report stays not ready.

Stable reparsing verifies byte/source and row identities; no DB import or
manual-task persistence is claimed. Manual task values are detached immutable
local records; production concurrency/identity/approval adapters remain outside
this slice. Caller approval does not arise from any parser result. CSV identity
fields are exact strings, not fuzzy supplier matching. XML/Excel remain explicit
unsupported formats, not presumed compatible vendor adapters.

Next safe slice: P2-04 offline authentication vectors and bounded retry plans.
