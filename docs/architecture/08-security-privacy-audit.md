# D-08. Security, privacy, audit, threat model, stop/recovery

## Controls

- Tenant isolation: mandatory tenant context, scoped queries, tested cross-tenant denial, separate secret references and cost ledgers.
- Identity: email login plus free email OTP or TOTP; sessions carry membership version. Revocation increments version and invalidates existing sessions immediately.
- Secrets: KMS/secret store, envelope encryption, rotation, no plaintext export/re-display, redacted logs, no mobile/client keys.
- Data: TLS in transit, encrypted storage, field-level protection for PII/credentials, masking by role, retention/purge workflow, legal hold.
- Supply chain: public code may contain no real keys, PII, business documents, contracts, or operational data; fixtures are synthetic/redacted. SBOM/license scan is a release gate.
- AI: prompt allowlist, PII/secret scrubber, input digest, output schema validation, deterministic policy, human approval, tool allowlist, audit correlation.

## Audit event schema

```json
{
  "event_id":"uuid","tenant_id":"uuid","occurred_at":"timestamp",
  "actor_type":"user|agent|workflow|adapter|system","actor_ref":"opaque",
  "action":"string","target_ref":"opaque","outcome":"accepted|blocked|succeeded|failed",
  "correlation_id":"uuid","command_id":"uuid|null","policy_version":"string|null",
  "metadata":{"before_digest":"string","after_digest":"string","reason":"string"},
  "prev_hash":"string|null","event_hash":"string"
}
```

Audit is append-only, hash-chained per tenant, time-stamped, access-logged, and exportable in a masked form. Audit readers cannot edit or delete. Retention duration remains DEC-07.

## Threat model

| Threat | Control | Detection/evidence |
|---|---|---|
| cross-tenant read/write | tenant context + RLS/equivalent + authorization tests | denied-request metric and audit |
| stolen channel/supplier secret | secret refs, KMS, rotation, scoped service identity | unusual calls, rotation log |
| replayed webhook | signature/replay window, external re-read, idempotency | rejected webhook audit |
| duplicate order/refund | external key + command idempotency + reconciliation | duplicate metric, no second side effect |
| prompt injection or hallucinated action | scrubber, typed tools, policy/approval, no direct DB | blocked command/audit |
| stale stock / negative margin | freshness guard, temporary pause, margin ≥10% | safety incident |
| malicious public fixture/ dependency | synthetic fixtures, SBOM, license/secret scan | CI evidence |
| notification takeover | signed links, re-auth for action, no approval by email/ChatGPT | auth audit |
| deletion used to hide activity | cooling-off, immutable audit/legal hold | deletion audit |

## Emergency stop and recovery state

`RUNNING → STOP_REQUESTED → STOPPED → INSPECTING → RECOVERY_PENDING → RECOVERED` or `QUARANTINED`. Stop is fail-closed and can be scoped globally, tenant, channel, supplier, or product. It blocks new registration, mutation, and purchase; in-flight shipment and CS continue. The stop command is immediate and audited; recovery requires health checks, identified impact, one master approval, and an explicit scope plus selected work to resume.
