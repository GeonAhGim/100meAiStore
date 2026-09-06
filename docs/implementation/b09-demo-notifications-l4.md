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

Acceptance covers priority fallback, per-item mute, replay/conflict, incident
acknowledgement replay, invalid channel/note rejection, and no external side
effect.
