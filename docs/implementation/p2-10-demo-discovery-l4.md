# P2-10 DEMO Discovery on synthetic channel fixtures — L4 packet

Status: implementation packet. Scope is DEMO only under the approval records in
`docs/implementation/approvals/` (G1/G2/G3, mode DEMO). No network access, no
credentials, no real channel or supplier call. LIVE remains unapproved.

## Contract

- Input: the approvals directory, the synthetic channel order fixture
  (`tests/fixtures/channel_orders.json`), a synthetic supplier CSV in the
  local schema, and the Naver product-order IDs the run expects to see.
- Gate check: the run first verifies that every required gate (G1, G2, G3) has
  a record whose mode is DEMO. A missing record or a non-DEMO mode raises
  `DiscoveryBlocked` before any fixture is read. Nothing else is inferred from
  the records.
- Coverage: for each source (naver, coupang, supplier) the report lists the
  required sample fields, how many rows were observed, which required fields
  were present on every row, and which were absent. A source with zero rows
  or an absent required field is `covered=false`.
- External-ID roundtrip: every snapshot's external IDs (order, line, product,
  shipment) are projected into the canonical `(provider, kind, value)` form and
  back; a mismatch is reported as `field_loss` with the offending key. IDs are
  compared as strings so numeric IDs above 2^53 are preserved.
- Permission report: fields that the DEMO records do not authorize (receiver
  name and address) are listed as `permission_gaps`, never echoed into the
  report body.
- Output: a frozen `DiscoveryReport` with `mode="DEMO"`, a canonical JSON
  payload and a SHA-256 digest of it. The report never contains fixture
  private names or addresses.
- Failure handling: contract quarantines from the existing parsers propagate
  unchanged. The run never writes outside the report object.

## Acceptance evidence

| ID | Acceptance criterion |
|---|---|
| P2-10-DEMO-DISCOVERY-01 | A discovery run over synthetic fixtures reports required sample coverage per channel and supplier |
| P2-10-DEMO-DISCOVERY-02 | External IDs survive a normalize-and-project roundtrip without loss |
| P2-10-DEMO-DISCOVERY-03 | The report lists missing permissions or lost fields and the run is fail-closed when no DEMO approval record exists |
