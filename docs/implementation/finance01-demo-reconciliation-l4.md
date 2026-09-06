# FINANCE-01 local DEMO settlement reconciliation — L4 packet

## Boundary

This slice imports strict local settlement rows and compares them with the
durable channel-order subledger. It records reconciliation evidence only: no
bank connection, payment, refund, supplier, network, or external accounting
write is allowed.

## Contract

`import_demo_settlement(context, channel_id, period, rows, idempotency_key)`
accepts rows with `external_order_key`, `kind` (`SALE`, `FEE`, `REFUND`),
signed integer `amount_minor`, ISO currency, and opaque `source_row_ref`.
Duplicate source rows, malformed values, currency mismatch, and unknown keys
are rejected before writes. A source digest and row references are retained;
raw files are not stored. Replaying the same batch key and digest returns the
same batch; a changed file under the key conflicts.

Rows are matched to internal channel orders by tenant/channel/external key.
Missing orders or total/currency discrepancies produce `EXCEPTION`; only a
complete match reaches `RECONCILED`. Settlement state is independent from PO
submission and never proves payment.

Realized contribution is stored separately from any projected margin and is
never used to overwrite a projection. A missing cost or unresolved match keeps
realized profit unavailable/exceptional. Batch, lines, match status, audit and
outbox commit atomically and survive restart; all reads are tenant scoped.

Acceptance targets: duplicate files, split-order rows, fee/refund adjustments,
currency/missing-order exceptions, idempotent replay, restart recovery,
tenant isolation, rollback on audit/outbox failure, and zero external effects.

Implementation evidence: `tests/store_core/test_finance01.py` covers settlement
reconciliation, fee adjustment, missing/currency exceptions, duplicate source
rows, idempotent replay, and projected/realized separation. Full verification
is `python -B -m pytest -q -p no:cacheprovider` — 97 passed on 2026-09-06;
compileall, `git diff --check`, and repository secret-pattern scan passed.
