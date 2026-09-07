# A-003 bounded channel fixture ingestion bridge

Implement one complete synthetic page of Naver PAYED or Coupang INSTRUCT orders
through the existing DEMO adapter manifest, poll checkpoint, payload/inbox and
order projection. Explicit provider identities are fixture-naver/fixture-coupang;
no transport or LIVE capability exists. Require a fresh caller-supplied fixture
observation, exact product-to-SKU mapping, whole quantities, matching full unit
payments, and no claim/discount/shipping/unknown amount or continuation. Coupang
additionally requires a complete empty synthetic claim page. Recheck observation
freshness on each poll and copy sanitized normalized data so input mutation
cannot change approval source evidence. Reject duplicate mapped SKUs rather
than lose distinct channel-line identities by merging them.

Acceptance runs parsed channel fixtures through durable poll/ingestion, supplier
quote routing, delegated mobile purchase approval, submission/readback, local
settlement and restart with sockets denied. Check page quarantine before cursor
advance, replay, tenant isolation, malformed/missing mappings, freshness and
input mutation. This is a local synthetic flow, not channel write contracts or
a proof that live feeds are complete. Exact source-line identity is now durable
through SQLite restart, with nullable compatibility for pre-v19 generic rows.
A-003 remains open for operational write/readback adapters and complete worker/outbox wiring;
A-002 identity/PWA and A-001 PostgreSQL are also still open.

Evidence: four new tests pass, including both provider fixtures through durable
ingestion, mobile FUNDS approval, PO readback, settlement and restart under a
socket-denied context. Stale polls leave checkpoint version unchanged; replay
and cross-tenant payload denial pass. Full suite: 236 tests. compileall,
diff whitespace and common secret-pattern gates passed.
