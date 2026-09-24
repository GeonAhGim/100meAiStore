# B09 local DEMO notification fallback and health acknowledgement — L4

Notification preferences store an ordered `app_push → email → chatgpt`
fallback list and a tenant-scoped per-notification mute flag. `notify_demo`
uses a deterministic local simulator: configured channel failures produce
durable `FAILED` attempts and the next configured channel is tried. Success is
`DELIVERED`; mute is `MUTED`. All payloads remain local, and no push, email,
ChatGPT, network, or paid-service call is made.

Each delivery is idempotent and tenant-owned. `acknowledge_demo_incident`
records one operator's durable acknowledgement with a reason and idempotency
key. SQLite and InMemory repositories provide restart-safe storage and tenant
boundary checks. Preference writes use version CAS; all successful or blocked
operations append local audit/outbox evidence.

## Acceptance evidence

| ID | Acceptance criterion |
|---|---|
| B09-01 | Notification fallback from app_push to email to chatgpt uses local simulator with deterministic DELIVERED/FAILED/MUTED outcomes and no external calls |
| B09-02 | Delivery is idempotent and tenant-scoped; operator incident acknowledgement with reason and idempotency key survives restart |

Acceptance (prior): covers priority fallback, per-item mute, replay/conflict, incident
acknowledgement replay, invalid channel/note rejection, and no external side
effect.
