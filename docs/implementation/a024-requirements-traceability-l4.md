# A-024 — Atomic requirements traceability (L4)

Status: implemented locally; external effects: none.

## Contract

`docs/100meAiStore-requirements-v1.md` is the canonical product requirement
source. Its first seven sections contain exactly 43 atomic bullets. The checked-in
manifest maps each bullet, without paraphrasing, to architecture, service,
repository, API/UI, worker/outbox, recovery, test, and implementation evidence.

The validator is fail-closed:

- schema version, canonical source, policy, ID sequence, exact source text and
  fixed count must match;
- evidence must be a non-empty list of repository-relative regular files in the
  field's allowed namespace, or an explicit non-empty `N/A:` rationale;
- absolute paths, parent traversal, wrong prefixes, directories, missing files,
  and paths containing symlinks are rejected;
- statuses are limited to `completed`, `partial`, `planned`, and
  `approval_gated`;
- runtime completion requires architecture, code, tests, and implementation
  evidence plus at least one durable/API/worker/recovery seam;
- architecture-only completion must remain internally consistent;
- a requirement with an unresolved external gate cannot be completed.

## Dashboard projection

The dashboard reads the validated manifest on every collector refresh and shows
the 43-item atomic ledger as its product completion source of truth. Only
`completed` contributes to the percentage. Any validation failure makes the
percentage unknown. Any `partial`, `planned`, or `approval_gated` item prevents a
100% completion claim. Delivery-package progress remains visible as supporting
detail and cannot override the atomic ledger.

## Acceptance evidence

`tests/test_traceability_manifest.py` covers the source baseline, exact coverage,
duplicate/missing IDs, metadata, invalid statuses, evidence namespaces, missing
files, symlinks, requirement-type completion, and unresolved external gates.
`tests/test_dev_dashboard.py` verifies that a completed delivery-package ledger
cannot produce a product-complete claim when atomic evidence is incomplete or
unavailable.

Run:

```text
python scripts/validate_traceability.py
python -m unittest tests.test_traceability_manifest tests.test_dev_dashboard -v
```
