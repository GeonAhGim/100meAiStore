# Autonomous dev pipeline (`dev.task`)

The local Codex worker pool can take one bounded implementation packet from
goal to published L4 spec without a human in the loop. Speed is not a goal;
never reporting an unverified result as done is.

## Enqueue

```powershell
store-aios dev-task d11-expiry-batch --title "D-11 bounded expiry batch" `
  --goal "Expire due approvals in batches of at most 100 per run" `
  --accept "One run expires at most 100 approvals" `
  --accept "A second run continues where the first stopped" `
  --file packages/store_core/approvals.py --file packages/store_core/sqlite_repository.py
```

`--file` is optional. When given, the implementation may only change those
paths plus `tests/` and the spec document. Every acceptance criterion gets an
ID `<TASK>-NN`; tests must reference each ID by name or docstring.

## Stages

| Stage | Who acts | Checkpoint written | Exit condition |
|---|---|---|---|
| spec | Codex writes `docs/implementation/<task>-l4.md` | `spec_commit`, `acceptance_digest` | spec contains every acceptance ID; committed |
| implement | Codex edits code and tests | — | worktree has a diff |
| verify | **worker** runs the test command and security scan | `last_test`, `fix_rounds`, `unreferenced_acceptance` | tests pass and all IDs referenced |
| fix | Codex receives the failure tail | `fix_completed`, `last_failure_digest` | bounded by `dev.max_fix_rounds` |
| publish | worker commits, appends `## Evidence` to the spec, commits again | `commit`, `spec`, `pushed` | push only when `dev.push` is true |

The job row stores the checkpoint, so a restarted worker resumes at the
recorded stage. The lease is extended before every stage; each Codex call and
each test run has its own wall-clock budget.

Each task works in `data/worktrees/<task>` on branch `worker/<task>`. The main
checkout is never written by the pipeline.

## Guards and the AIOS failures they answer

Numbers are the AIOS local-pool incident list (2026-09-15 to 09-22).

| Guard | AIOS failure |
|---|---|
| Worker runs tests itself; model claims are ignored | #5 QA passed without a diff |
| Stage with no diff is an error; identical failure output after a completed fix round is a stall and dead-letters | #2 hook-denial retry loop, $0 one-turn exits |
| Fix rounds bounded; budget exhaustion dead-letters with the last output attached | #2 max_turns loops |
| Acceptance table digest is fixed at the spec stage; any later change is a permanent failure | #7 ratchet baseline raised, #18 spec drift |
| Existing files with 40+ lines may not lose more than 80% of their lines | #6 wholesale rewrite pushed |
| Optional `files` allowlist; changes or new files outside it are permanent failures | #8 leaves without file scope |
| Payload schema validated before any model call; invalid payload is dead at once | #9, #11 field type errors looping |
| Throttle markers in Codex output defer the job for `dev.usage_limit_delay_seconds` without consuming an attempt | #12, #13 context/usage limits |
| Main checkout status is compared before and after each Codex call; a change fails the stage | #15 writes to the live checkout |
| Lease heartbeat before each stage, per-call timeouts | #4 stale locks, #16 duplicate assignment |
| Security scan and passing tests before any commit; push disabled by default | #17 red CI from unpushed gates |
| One corrupt job row is dead-lettered and the next job is claimed | #1 one bad file killed the publisher |

Not adopted on purpose: no automatic capacity governor (#3). Capacity is the
number of worker processes the operator starts.

## Configuration

```json
"dev": {
  "max_fix_rounds": 3,
  "stage_timeout_seconds": 1800,
  "test_timeout_seconds": 900,
  "test_command": ["python", "-m", "unittest", "discover", "-s", "tests", "-t", "."],
  "push": false,
  "usage_limit_delay_seconds": 1800
}
```

`codex.enabled` must be true or the job returns a dry-run result. Model
prompts contain only the goal, acceptance text, spec path and test output
tail; never secrets or operator instructions.

## What a dead job looks like

`last_error` starts with one of: `fix budget exhausted`, `stalled:`, `security
scan rejected`, `acceptance table`, `wholesale rewrite`, `outside the allowed
file list`, `modified the main checkout`, or a payload validation message. The
worktree and branch are left in place for a human to inspect.
