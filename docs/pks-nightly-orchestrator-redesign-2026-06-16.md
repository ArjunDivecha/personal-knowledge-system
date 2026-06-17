# PKS Nightly Orchestrator Redesign

Date: 2026-06-16
Status: Phase 0-2 complete; Phase 3 PRD written 2026-06-17

## Implementation Status

| Phase | Scope | State |
|---|---|---|
| 0 | docs + repo guidance only | done |
| 1 | orchestrator ledger, lock, preflight, report renderer, shadow wrappers; Python tests; NO production mutation | done (32 tests pass) |
| 2 | async Dream start/status, server-enforced shadow mode, caller-supplied `dream_run_id`, atomic date lock, Worker tests | done: code/tests, staging deploy + shadow smoke, and production deploy + shadow smoke all passed. See `docs/pks-nightly-orchestrator-phase-2-dream-worker-prd-2026-06-16.md` |
| 3 | run orchestrator manually in shadow, compare with existing nightly artifacts | PRD written; implementation not started. See `docs/pks-nightly-orchestrator-phase-3-manual-shadow-run-prd-2026-06-17.md` |
| 4 | install one launchd orchestrator plist (old schedules stay active) | not started |
| 5 | cut over | not started |
| 6 | retire old repair scripts | not started |

Phase 1 modules (all shadow-only / non-mutating):
`orchestrator/{config,ids,states,backends,lock,ledger,preflight,report,dream,stages,engine,cli}.py`,
entrypoint `scripts/nightly_orchestrator.py`, uninstalled launchd wrapper
`scripts/run_orchestrator_launchd.sh`, tests `orchestrator/tests/` (32 passing,
incl. an opt-in live Upstash Lua test proving the production atomic path). The
Atomicity And Race Contract is implemented in `backends.py` (Lua `EVAL` for
acquire+fence / heartbeat / check_fence / cas_set) and `lock.py`/`ledger.py`
(fence-guarded CAS terminal writes).

Phase 1 code lives in the `orchestrator/` package; the production entrypoint is
`scripts/nightly_orchestrator.py` (CLI: `preflight | run | resume | report`).
Everything is **shadow-only and non-mutating** in Phase 1: the lock and ledger
already enforce the Atomicity And Race Contract (atomic acquire+fence via Lua
`EVAL`, equality-only fence, fence-guarded CAS terminal writes), but no stage
performs ingestion writes or Dream `applyDreamProposal`. The async Dream Worker
endpoints (Phase 2) are not built yet; the orchestrator's Dream stages run
against an injectable shadow Dream client that always reports
`executed_mode=shadow`, `applied_count=0`. The launchd plist is NOT installed
(Phase 4); the existing nightly schedules remain the source of truth.

## Summary

The PKS nightly update system is moving from multiple independent schedulers to
one M4-owned production controller.

The anchored M4 owns scheduling, sequencing, state, reporting, and alert
coordination. Cloudflare remains the Dream execution service. Upstash
Redis/Vector remains the shared data plane. GitHub Actions and Cloudflare cron
become CI, manual, or read-only monitoring surfaces, not production writers.

The design priority is a single owner with explicit idempotency, crash recovery,
and a report that can be rendered from durable state at any point.

## Architecture

The production nightly entrypoint is a one-shot local orchestrator:

```text
launchd -> scripts/run_orchestrator_launchd.sh -> scripts/nightly_orchestrator.py
```

The orchestrator CLI must support:

```text
nightly_orchestrator.py preflight
nightly_orchestrator.py run --mode shadow|live --date YYYY-MM-DD|auto
nightly_orchestrator.py resume --date YYYY-MM-DD|auto
nightly_orchestrator.py report --date YYYY-MM-DD|auto
```

The state machine is:

```text
INIT
LOCKED
PREFLIGHT
SNAPSHOT_BEFORE
INGEST_TWITTER
VALIDATE_TWITTER
INGEST_GITHUB
VALIDATE_GITHUB
INGEST_AGENT_SESSIONS
VALIDATE_AGENT_SESSIONS
DREAM_START
DREAM_WAIT
DREAM_VERIFY
INDEX_VERIFY
CONSISTENCY_VERIFY
REPORT_WRITE
NOTIFY
DONE
```

Terminal statuses:

```text
completed
completed_with_warnings
completed_with_holds
failed_recoverable
failed_terminal
abandoned_by_newer_run
```

Each stage record includes:

```json
{
  "stage": "INGEST_GITHUB",
  "status": "completed_with_warnings",
  "attempt": 1,
  "started_at": "2026-06-16T23:00:00-07:00",
  "completed_at": "2026-06-16T23:10:00-07:00",
  "counts": {},
  "warnings": [],
  "errors": [],
  "retryable": true,
  "next_action": null
}
```

Local ledger path:

```text
ingestion/checkpoints/orchestrator_runs/{run_date}.json
```

Remote ledger and heartbeat keys:

```text
pks:orchestrator:run:{run_date}
pks:orchestrator:last_started
pks:orchestrator:last_heartbeat
pks:orchestrator:last_completed
pks:orchestrator:last_status
pks:orchestrator:last_report
```

Report paths:

```text
scripts/reports/pks-nightly-{run_date}.json
scripts/reports/pks-nightly-{run_date}.md
```

## Atomicity And Race Contract

This section is mandatory for implementation. Do not implement the Dream apply
or fencing path without preserving these invariants.

Hard invariants:

- No superseded job may apply mutations.
- No shadow run may apply mutations.
- No missed night may go unalerted.
- A resumed Dream stage reattaches to the original `dream_run_id`; it never
  starts a second Dream run for the same orchestrator attempt.

Run identities:

```text
orchestrator_run_id = pksn_YYYYMMDD_HHMMSS_<8hex>
dream_run_id        = dga_YYYYMMDD_<8hex>
```

The `<8hex>` suffix is shared between the orchestrator and Dream run ids for
operator traceability. `dream_run_id` is the Cloudflare Dream idempotency key.

Orchestrator lock:

```text
key:   pks:orchestrator:lock:{run_date}
value: {
  "orchestrator_run_id": "...",
  "owner_host": "m4max-base",
  "pid": 12345,
  "acquired_at": "...",
  "heartbeat_at": "...",
  "fencing_token": 17
}
ttl: 2 hours
heartbeat refresh: every 60 seconds
stale threshold: 90 minutes since heartbeat_at
```

Lock acquisition and fence increment must be one atomic Redis operation. Use a
Lua script or `WATCH`/`MULTI`; do not use separate compare-then-write calls.

Fence semantics:

- The fence token is equality-only.
- A stage that captured fence `N` may commit terminal success only if the
  current lock still has fence `N` and the same `orchestrator_run_id`.
- If the current fence is `N+1`, the old stage must abort and record
  `abandoned_by_newer_run` if it can still write safely.
- Stage terminal writes must be atomic compare-and-set operations.

Dream date lock:

```text
key:   dream:scheduled-governed:date-lock:{run_date}
value: {
  "dream_run_id": "...",
  "orchestrator_run_id": "...",
  "mode": "shadow|live",
  "fencing_token": 17,
  "acquired_at": "..."
}
```

The Dream date lock is the authoritative Cloudflare-side mutation fence. The
fence value must be co-located in this lock record so the Worker does not need
to read separate keys before applying mutations.

`applyDreamProposal` may run only after an atomic Worker-side gate confirms:

```text
date_lock.dream_run_id == request.dream_run_id
date_lock.orchestrator_run_id == request.orchestrator_run_id
date_lock.mode == "live"
date_lock.fencing_token == request.fencing_token
request.mode == "live"
```

If any check fails, the Worker must not mutate. It must write a terminal Dream
status such as `rejected_superseded`, `rejected_shadow_mode`, or
`rejected_fence_mismatch`.

Shadow mode:

- The Worker may propose, grade, and build a decision.
- The Worker must not call `applyDreamProposal`.
- Status must include `executed_mode`.
- Status must include `applied_count`.
- Missing `executed_mode` or missing `applied_count` is a terminal failure for
  the orchestrator.
- A shadow run with `applied_count > 0` is a terminal failure.

Dream wait and resume:

- `DREAM_START` is issued at most once per `dream_run_id`.
- Start is skipped if status already exists for that `dream_run_id`.
- `DREAM_WAIT` polls every 30 seconds.
- First timeout: 45 minutes, mark `DREAM_WAIT` as `failed_recoverable`.
- Resume reattaches to `GET /ops/dream/scheduled_governed/status?run_id=...`.
- Resume never issues a new start while status exists for the original
  `dream_run_id`.
- Hard Dream cutoff: 07:30 Pacific. If Dream is still non-terminal after that,
  mark the orchestrator run `failed_terminal` and let the dead-man alert at
  09:00 Pacific.

Resume host policy for v1:

- Resume is same-host-only.
- This is required because agent-session checkpoints are local-first.
- Cross-host resume is not supported until all source checkpoints required for
  replay are Redis-authoritative.

## Dream Worker Contract

Keep Dream governance in Cloudflare. Do not port or rewrite apply logic locally.

Add:

```text
POST /ops/dream/scheduled_governed/start
GET  /ops/dream/scheduled_governed/status?run_id=...
```

Start request:

```json
{
  "run_id": "dga_20260616_ab12cd34",
  "orchestrator_run_id": "pksn_20260616_230000_ab12cd34",
  "run_date": "2026-06-16",
  "mode": "shadow",
  "fencing_token": 17,
  "cron": "m4-orchestrator",
  "scheduled_time": 1781650800000
}
```

Start response:

```json
{
  "accepted": true,
  "requested_mode": "shadow",
  "executed_mode": "shadow",
  "dream_run_id": "dga_20260616_ab12cd34",
  "orchestrator_run_id": "pksn_20260616_230000_ab12cd34",
  "run_date": "2026-06-16",
  "status_url": "/ops/dream/scheduled_governed/status?run_id=dga_20260616_ab12cd34"
}
```

Status response must echo:

```text
requested_mode
executed_mode
dream_run_id
orchestrator_run_id
run_date
applied_count
```

The existing synchronous endpoint may remain temporarily for manual debugging,
but production orchestration must use async start/status.

## Per-Stage Idempotency

| Stage | Class | Resume rule |
|---|---|---|
| PREFLIGHT | idempotent | Rerun. |
| SNAPSHOT_BEFORE | idempotent | Rerun and replace snapshot pointer. |
| INGEST_TWITTER | cursor-checkpointed | Rerun only if prior stage was not completed; relies on processed tweet ids. |
| INGEST_GITHUB | cursor-checkpointed | Rerun only if prior stage was not completed; relies on per-repo baselines and source markers. |
| INGEST_AGENT_SESSIONS | cursor-checkpointed, same-host-only | Rerun only if prior stage was not completed; relies on local byte-offset checkpoint and Redis best-effort. |
| VALIDATE_* | idempotent | Rerun. |
| DREAM_START | idempotent by `dream_run_id` | Call start only if no status exists. |
| DREAM_WAIT | idempotent poll | Reattach to existing `dream_run_id`. |
| DREAM_VERIFY | idempotent | Rerun. |
| INDEX_VERIFY | idempotent | Rerun; v1 verifies only unless explicitly configured to rebuild. |
| CONSISTENCY_VERIFY | idempotent | Rerun. |
| REPORT_WRITE | idempotent | Atomically overwrite same-date report. |
| NOTIFY | skip-if-completed | Do not resend unless `--force-notify`. |

## Preflight And Auth Failure

`preflight` must verify:

- repo `.env` is readable
- Upstash Redis is reachable
- Upstash Vector is reachable
- Cloudflare `/health` is reachable
- `DREAM_OPERATOR_TOKEN` is present
- Claude CLI is present
- non-interactive Claude SDK or CLI auth is live
- API fallback is available and capped if SDK auth is unavailable
- no-browser guards are set for unattended auth checks

Auth unavailable is a recoverable terminal state:

```json
{
  "status": "failed_recoverable",
  "failure_code": "auth_unavailable",
  "next_action": "Reauthenticate Claude CLI/SDK on M4 or enable capped API fallback."
}
```

Preflight must never open a browser.

## Missed-Night Handling

Use one launchd-owned controller, but allow a catch-up subcommand.

Production plist invokes:

```text
scripts/run_orchestrator_launchd.sh
```

That wrapper runs the same orchestrator binary for all paths:

- scheduled 23:00 execution
- on-load catch-up after reboot/login
- optional 30-minute catch-up checks between 23:15 and 08:45

Catch-up behavior:

- if today's run is done, exit
- if today's run is incomplete, resume
- if no run exists and current time is before 08:45, start late
- if no run exists after 08:45, mark missed locally if possible
- dead-man checks at 09:00 and alerts if no completed orchestrator report exists

The 08:45 cutoff prevents the local catch-up check and 09:00 dead-man monitor
from racing each other.

## Dead-Man Monitor

Before cutover, the dead-man runs in `shadow_validation` mode:

- checks orchestrator heartbeat/report
- writes GitHub artifact/log status only
- does not alert the user directly
- old NightWatch/GitHub artifacts remain user-facing truth

At cutover, flip:

```text
PKS_NIGHTLY_SOURCE_OF_TRUTH=orchestrator
```

After cutover:

- dead-man alerts if no terminal orchestrator report exists by 09:00 Pacific
- dead-man remains read-only
- old GitHub sleep report is disabled or manual-only

## Rollout

Phase 0: update docs and repo guidance only.

Phase 1: implement orchestrator ledger, lock, preflight, report renderer, and
shadow wrappers. No production mutation.

Phase 2: implement async Dream start/status, server-enforced shadow mode,
caller-supplied `dream_run_id`, atomic date lock, and Worker tests.

Phase 3: run orchestrator manually in shadow and compare with existing nightly
artifacts.

Phase 4: install one launchd orchestrator plist. Keep old mutating schedules
active until shadow validation completes. Orchestrator Dream remains shadow-only.

Phase 5: cut over by disabling Cloudflare Dream cron mutation, disabling GitHub
nightly sleep-report schedule, disabling or manualizing GitHub ingestion
schedules, and replacing 02:00 second-chance launchd with orchestrator resume.

Phase 6: after 5 clean live nights, retire old repair scripts to manual/debug
docs.

## Cutover Gates

All gates must pass:

- 5 consecutive terminal orchestrator shadow runs.
- One deliberately interrupted run resumes successfully.
- One deliberate dead-man missed-completion drill alerts in shadow-validation.
- Async Dream shadow proves `applied_count == 0`.
- Worker status echoes mode, run ids, run date, and applied count.
- No missing `executed_mode` or `applied_count`.
- Old and new reports agree exactly on Dream status, held ops, tripwires, and
  final verdict.
- Ingestion count deltas above 10 entries or 5 percent, whichever is larger,
  must be explained in the report.
- Orchestrator reaches terminal state before 08:30 Pacific during validation.

## Tests

Python tests:

- ledger transition validation
- lock acquisition and stale lock refusal
- same-host resume
- stage terminal write rejects stale fence
- partial report from incomplete ledger
- preflight auth unavailable path
- no-browser auth guard
- Dream wait timeout and reattach
- notify skip-if-completed

Worker tests:

- async start returns quickly
- status returns accepted/running/terminal records
- duplicate start with same `dream_run_id` does not duplicate work
- shadow never calls apply
- missing `executed_mode` or `applied_count` is detectable
- live apply gate rejects wrong mode
- live apply gate rejects wrong fence
- live apply gate rejects superseded run id
- date lock prevents same-date double apply

Integration tests:

- shadow orchestrator writes local and Redis ledger
- forced mid-run failure renders partial report
- dead-man detects missing completion in shadow-validation
- manual `resume --date` continues same-host run
- async Dream shadow status has zero applied mutations

## Defaults

- M4 is the production controller.
- 23:00 Pacific remains the nightly target time.
- 08:30 Pacific is the orchestrator SLA.
- 08:45 Pacific is the local catch-up cutoff.
- 09:00 Pacific is the external dead-man check.
- Redis lock TTL is 2 hours.
- Orchestrator heartbeat interval is 60 seconds.
- Stale lock threshold is 90 minutes.
- Dream poll interval is 30 seconds.
- First Dream wait timeout is 45 minutes.
- Hard Dream cutoff is 07:30 Pacific.
- Resume is same-host-only in v1.
