# B10 local DEMO stop, backup/restore, and safety boundaries — L4

DEMO stop controls are tenant-owned and scoped to `global`, `tenant`, or a
specific `connection`. They use versioned durable rows and are checked by the
typed gateway before accepting any tool command; active stops fail closed with
`blocked/stop_active`. Re-enabling is explicit and audited.

`backup_demo_sqlite` copies a local SQLite ledger only to a new caller-supplied
temporary path, verifies `PRAGMA integrity_check`, and records a schema/digest
manifest. It never deletes or overwrites a path. Restart tests reopen the
copied database and re-check readiness, tenant state, and safety rows.

## Acceptance evidence

| ID | Acceptance criterion |
|---|---|
| B10-01 | DEMO stop (global/tenant/connection scoped) is tenant-owned and durable; active stops fail closed; explicit resume is audited |
| B10-02 | Backup copies SQLite to caller-supplied path, verifies integrity and manifest, never deletes; restore reopens with same tenant/safety state |

Acceptance (prior): covers scope stop gates, explicit resume, tenant isolation,
versioned stop state, backup integrity and restore persistence, invalid input
rejection, and regression checks that raw secrets, network calls, and unsafe
paths are not introduced.
