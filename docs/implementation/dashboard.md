# Local operations dashboard

The 100meAiStore dashboard is a local-only, read-only PWA-style page. It is
not the old AIOS application dashboard. A read-only search of the available
AIOS worktree found a React `DashboardPage` and an atomic file heartbeat
utility, but no reusable worker-state dashboard contract. We therefore use a
small project-specific contract and only borrowed the useful stale-heartbeat
and status-badge ideas.

## Run

From the repository root:

```powershell
python -m smart_store_aios.dashboard --database data/store.sqlite3 --project-root . --host 127.0.0.1 --port 8765
```

Open <http://127.0.0.1:8765/>. Enter a tenant and active member ID. The API
does not enumerate tenants and rejects a missing or stale membership.

## API contract

`GET /api/dashboard?tenant_id=<id>&user_id=<id>` returns JSON only from the
tenant-scoped SQLite projection:

- `phase`: phase name, evidence-backed acceptance counts and completion; a
  missing `.codex/phase-progress.json` is `completion_percent: null`, never an
  estimate.
- `agents`: persisted `agent_status_snapshots` with state, current task,
  heartbeat age, last message/commit/test, next task and blocker. A running
  record whose heartbeat is over 30 seconds old is marked `stale`.
- `workers` and `queues`: derived outbox/inbox counts; reconciliation remains
  explicitly zero until its durable projection exists.
- `readiness`, `recent_commits`, `tests`, `approvals_required`,
  `tokens_cost`, and `controls`.

The default UI polls every 10 seconds, pauses polling in a background tab, and
refreshes immediately when the tab becomes visible. The AI summary control is
disabled and `POST` requests are rejected. Dashboard refreshes do not import
or call an LLM client.

`.codex/last-test.json` and `.codex/phase-progress.json` are optional local
checkpoints. Missing files are shown as unknown. Agent status is durable in
SQLite schema v5 and survives process restart. This page does not configure
Windows startup, Docker, cloud deployment, external accounts, or marketplace
writes.
