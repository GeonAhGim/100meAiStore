# Worker queues: which one is authoritative

Two job-processing paths coexist in this repository. This note records which is
the system of record so the two are not extended in parallel.

| Path | Files | Role |
|---|---|---|
| Durable outbox worker | `packages/store_core/tool_worker.py`, `sqlite_repository.py` (`claim_next_outbox`, `checkpoint_outbox`, `fail_outbox`) | **Authoritative.** Every approved typed tool command runs here with fencing tokens, lease ownership checks, idempotent reconciliation, and audit events. |
| Legacy job queue | `smart_store_aios/db.py`, `smart_store_aios/worker.py` | **Local development only.** Runs `codex.task` and dry-run placeholder kinds. Not tenant-scoped and not wired to approvals. Do not add business execution here. |

## Outbox worker loop

`DemoToolCommandWorker.process(context, event_id)` drives one named event and
raises on binding errors. `process_next(context)` claims the next ready
`tool.command` event via `claim_next_outbox`, and any failure while driving it
is recorded with `fail_outbox` (retry with backoff, dead after `max_attempts`)
instead of propagating. A polling loop should call `process_next` and sleep
when it returns `None`.

## Legacy worker semantics

- `store-aios worker` runs as a daemon: it polls every `--poll-seconds` and
  stops cleanly on SIGINT/SIGTERM. `--once` processes one job; `--drain`
  exits when the queue is empty (the previous default behavior).
- A failed job is marked `queued` with exponential backoff in seconds, or
  `dead` after `workers.max_attempts`. The failure never stops the daemon.
- `complete` and `fail` verify that the caller still owns an unexpired lease;
  a stale worker gets `LeaseError` and cannot overwrite another worker's state.
- `codex exec` is bounded by `workers.lease_seconds - 5` so a hung process
  cannot outlive its lease and be executed twice.

## Migration intent

When Phase 3 adds a production API and worker entry point, the legacy queue
should be removed or reduced to a thin adapter that enqueues into the outbox.
See `local-restart-operations.md` for the restart and recovery rules that the
authoritative path must keep satisfying.
