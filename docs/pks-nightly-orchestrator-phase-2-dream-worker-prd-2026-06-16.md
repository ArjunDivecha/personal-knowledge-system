# PKS Nightly Orchestrator Phase 2 PRD

Date: 2026-06-16
Status: code + tests implemented; production deployed + shadow-smoked; staging smoke blocked by Upstash rate limit
Owner: PKS nightly orchestrator
Phase: 2 - async Dream Worker contract

## Implementation Status (2026-06-16)

Done (local, non-deploy steps of the Rollout Plan, items 1-4):

- Worker module `cloudflare-mcp/mcp-server/src/scheduledDreamAsync.ts`: zod start
  schema (id/suffix/date cross-validation), atomic date-lock Lua, async
  start/status, idempotent shadow executor (proposal -> grade ->
  buildScheduledGovernedDecision, **never** applyDreamProposal in shadow),
  pure `verifyScheduledGovernedLiveApplyGate`, and `validateTerminalStatus`.
- Routes added in `src/index.ts` (`POST /ops/dream/scheduled_governed/start`,
  `GET /ops/dream/scheduled_governed/status`) via dynamic import; the shared
  decision helpers + limit constants are now `export`ed from `index.ts` (no
  static circular import — the new module imports them, index reaches the new
  module only through a dynamic import). `Env.PKS_ORCH_DREAM_LIVE_ENABLED` added.
- Worker tests `test/scheduled-async.test.ts`: 21 passing (schema, 401/404
  routes, accepted-before-executor, duplicate idempotent, same-date date_locked,
  shadow-never-applies, executed_mode/applied_count, failed-on-throw, live
  rejected without flag, gate wrong mode/fence/run-id/date, terminal-status
  validation). `npm run type-check` passes. Existing suite: all previously
  passing tests still pass; `dream-replay.test.ts` and `oauth-mcp.test.ts` have
  2 failures that **pre-exist on HEAD** and are unrelated to this work.
- Python `orchestrator/dream.py`: `HttpDreamClient` (Bearer token, finite
  timeouts, 404->None, non-2xx recoverable unless terminal rejection, injectable
  transport) + `default_dream_client()` gated on `PKS_ORCH_DREAM_CLIENT=http`.
  `orchestrator/stages.py` also fails fast on Worker start rejections such as
  `date_locked`, so the M4 does not wait on a dream_run_id that the Worker never
  accepted. 8 new Python tests; full suite 40 passing.
- Smoke script
  `cloudflare-mcp/mcp-server/scripts/test-scheduled-dream-async-smoke.ts`:
  one-command deployed endpoint validation for accepted shadow start, terminal
  `completed_shadow`, `applied_count=0`, duplicate idempotency, and same-date
  `date_locked`.

Deploy/smoke update (2026-06-16 PT):

- Staging deploy succeeded with the repo `CLOUDFLARE_API_TOKEN`:
  `arjun-knowledge-mcp-staging`, version
  `e6b2eded-39e5-425f-a024-687abe96192a`.
- Staging scheduled-Dream smoke reached the endpoint but failed before accept
  because the staging Upstash Redis returned: `Your database has been
  temporarily rate-limited`. A 45s backoff retry failed the same way.
- Production deploy succeeded through `scripts/deploy_cloudflare_worker.sh`:
  `arjun-knowledge-mcp`, version `05bc9425-f57c-4328-bd30-b814b359d9b0`;
  cron remained `10 7 * * *`; live mode remains disabled (no
  `PKS_ORCH_DREAM_LIVE_ENABLED`).
- Production far-future scheduled-Dream shadow smoke passed against
  `https://mcp.dancing-ganesh.com` for `2099-12-31`: terminal
  `completed_shadow`, `executed_mode=shadow`, `applied_count=0`, duplicate
  start HTTP 200, same-date different-run HTTP 409, and smoke status/date-lock
  cleanup enabled.
- Production `/health` returned `status=ok` after deploy.

Remaining blocker:

- Fix or replace the staging Upstash Redis resource, then rerun the staging
  scheduled-Dream smoke. Code and production shadow behavior are verified, but
  the staging smoke acceptance criterion remains blocked by staging
  infrastructure.

## Summary

Phase 2 adds an async, idempotent Cloudflare Dream Worker contract for the M4
orchestrator built in Phase 1.

The goal is not to cut over production scheduling. The goal is to give the
orchestrator a real Worker-backed Dream client that can start exactly one
scheduled-governed Dream run per orchestrator attempt, poll durable status, and
prove that shadow mode cannot mutate live memory state.

Phase 2 must preserve the Atomicity And Race Contract from
`docs/pks-nightly-orchestrator-redesign-2026-06-16.md`.

## Product Goal

Build the minimum Worker-side protocol needed for a robust M4-owned nightly:

- `POST /ops/dream/scheduled_governed/start`
- `GET /ops/dream/scheduled_governed/status?run_id=...`
- caller-supplied `dream_run_id`
- Redis-persisted run status
- Redis-persisted per-date Dream lock
- server-enforced shadow mode in production Phase 2
- live apply gate code and tests, but no live production apply
- Python orchestrator client wired to these endpoints

The M4 remains the controller. Cloudflare remains the Dream execution service.
Upstash Redis remains the shared status and locking substrate.

## Non-Goals

Phase 2 does not:

- install launchd
- disable old GitHub or Cloudflare schedules
- cut over production reporting
- run ingestion stages for real inside the new orchestrator
- enable Worker-side live apply from the new async endpoint in production
- remove or rewrite the existing synchronous `/ops/dream/run_scheduled_governed`
  repair endpoint

## Current State

Phase 1 landed:

- `orchestrator/` package
- `scripts/nightly_orchestrator.py`
- local and Redis orchestrator ledger
- Lua-backed fencing lock
- shadow-only stage wrappers
- in-process `Phase1ShadowDreamClient`
- 32 Python tests

The Worker currently has:

- `runScheduledGovernedDream(env, controller)` in
  `cloudflare-mcp/mcp-server/src/index.ts`
- operator endpoint `POST /ops/dream/run_scheduled_governed`
- existing proposal, grade, bounded apply, tripwire, judge queue, and policy cap
  machinery
- existing scheduled cron path
- Worker tests in `cloudflare-mcp/mcp-server/test/scheduled.test.ts`

Phase 2 should wrap and reuse this machinery. It should not port Dream logic to
Python or duplicate apply logic outside Cloudflare.

## Design Principles

1. One source of execution truth.

   The async Dream run record is the authoritative Worker-side status for a
   `dream_run_id`. The orchestrator only observes and reacts.

2. Shadow must be impossible to mutate.

   In Phase 2, production start requests with `mode=shadow` may propose, grade,
   and build a decision. They must not call `applyDreamProposal`.

3. Live must require two locks and an explicit flag.

   Future live apply can happen only when the orchestrator Redis fence is valid,
   the Worker date lock matches the request, the request mode is live, the date
   lock mode is live, and Worker live mode is explicitly enabled.

4. Idempotency beats clever retries.

   Duplicate `start` for the same `dream_run_id` returns the existing status.
   Duplicate `start` for the same date but a different run id is rejected by the
   date lock.

5. Bounded autonomy remains the long-term model.

   The Worker should keep the proposal -> grade -> bounded apply -> verify shape
   with caps, tripwires, and held exceptional cases. In Phase 2, the same
   decision path is exercised in shadow and status exposes what would have been
   selected or held.

6. No request-scoped globals.

   All run state must live in Redis or function-local variables. Do not store a
   current run id, request, env, or status object in module-level mutable state.

7. Every promise is accounted for.

   Worker background execution must be awaited, returned, or passed to
   `ctx.waitUntil()`. Failed async work must write a terminal failed status.

## External Guidance Consulted

Cloudflare Worker guidance used for this plan:

- Workers best practices: keep compatibility configuration current, use
  `nodejs_compat`, keep secrets out of source, use `ctx.waitUntil()` for work
  after a response, use Queues/Workflows for heavier async work when needed,
  avoid global request state, account for all promises, use Web Crypto for
  secure token generation, and test with `@cloudflare/vitest-pool-workers`.
- Existing repo config already uses `nodejs_compat`, generated worker types, and
  `@cloudflare/vitest-pool-workers`; Phase 2 should stay within those patterns.

## API Contract

### Start

```text
POST /ops/dream/scheduled_governed/start
Authorization: Bearer ${DREAM_OPERATOR_TOKEN}
Content-Type: application/json
```

Request:

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

Validation:

- `run_id` must match `^dga_[0-9]{8}_[0-9a-f]{8}$`.
- `orchestrator_run_id` must match
  `^pksn_[0-9]{8}_[0-9]{6}_[0-9a-f]{8}$`.
- the suffix in both ids must match.
- `run_date` must match the date embedded in both ids.
- `mode` must be `shadow` or `live`.
- `fencing_token` must be a positive integer.
- `cron` must be a short string.
- `scheduled_time` must be a positive integer epoch milliseconds.

Phase 2 production behavior:

- `mode=shadow`: accepted.
- `mode=live`: rejected unless `PKS_ORCH_DREAM_LIVE_ENABLED=1` is present in
  the Worker env. Do not set this flag in production in Phase 2.

Accepted response:

```json
{
  "accepted": true,
  "duplicate": false,
  "requested_mode": "shadow",
  "executed_mode": "shadow",
  "dream_run_id": "dga_20260616_ab12cd34",
  "orchestrator_run_id": "pksn_20260616_230000_ab12cd34",
  "run_date": "2026-06-16",
  "state": "accepted",
  "status": "accepted",
  "status_url": "/ops/dream/scheduled_governed/status?run_id=dga_20260616_ab12cd34"
}
```

Duplicate same-run response:

```json
{
  "accepted": true,
  "duplicate": true,
  "reason": "status_exists",
  "status": { "...": "existing durable status record" }
}
```

Rejected same-date different-run response:

```json
{
  "accepted": false,
  "error": "date_locked",
  "blocked_by": {
    "dream_run_id": "dga_20260616_aaaaaaaa",
    "orchestrator_run_id": "pksn_20260616_230000_aaaaaaaa",
    "fencing_token": 16
  }
}
```

### Status

```text
GET /ops/dream/scheduled_governed/status?run_id=dga_20260616_ab12cd34
Authorization: Bearer ${DREAM_OPERATOR_TOKEN}
```

Response:

```json
{
  "schema_version": 1,
  "dream_run_id": "dga_20260616_ab12cd34",
  "orchestrator_run_id": "pksn_20260616_230000_ab12cd34",
  "run_date": "2026-06-16",
  "requested_mode": "shadow",
  "executed_mode": "shadow",
  "fencing_token": 17,
  "state": "terminal",
  "status": "completed_shadow",
  "accepted_at": "2026-06-16T23:00:05.000Z",
  "started_at": "2026-06-16T23:00:05.100Z",
  "updated_at": "2026-06-16T23:02:14.000Z",
  "completed_at": "2026-06-16T23:02:14.000Z",
  "proposal_id": "dpr_...",
  "proposal_status": "proposal_ready",
  "risk_score": "medium",
  "grade_id": "dpg_...",
  "grade_status": "passed",
  "decision": {
    "selected_operation_ids": [],
    "held_operations": []
  },
  "counts": {
    "operation_count": 82,
    "selected_operation_count": 0,
    "held_operation_count": 82,
    "applied_count": 0,
    "archive_limit": 50,
    "promotion_limit": 10,
    "duplicate_merge_limit": 10,
    "mark_contested_limit": 10
  },
  "applied_count": 0,
  "held_count": 82,
  "errors": [],
  "warnings": [],
  "next_action": null
}
```

Required fields for every terminal response:

- `executed_mode`
- `applied_count`
- `dream_run_id`
- `orchestrator_run_id`
- `run_date`
- `requested_mode`
- `state`
- `status`

If any required field cannot be populated, the Worker must write a terminal
`failed` status with `executed_mode` and `applied_count: 0` still present.

## Redis Keys

Use these keys:

```text
dream:scheduled-governed:status:{dream_run_id}
dream:scheduled-governed:date-lock:{run_date}
dream:scheduled-governed:events:{dream_run_id}
dream:scheduled-governed:last_started
dream:scheduled-governed:last_completed
```

Keep existing keys:

```text
dream:last_run
dream:last_attempt
dream:runs:index
dream:scheduled-governed:boundary:{...}
```

Phase 2 may mirror terminal async runs into the existing Dream run index so
current health and report tools can discover them, but the new status key is the
source of truth for the orchestrator.

## Atomic Date Lock

Add a single Redis Lua script in Worker code for date-lock acquisition.

Inputs:

- status key
- date-lock key
- run id
- orchestrator run id
- run date
- requested mode
- executed mode
- fencing token
- now timestamp
- ttl seconds

Behavior:

1. If status already exists for `dream_run_id`, return duplicate existing.
2. If date lock does not exist, write the date lock and accepted status in the
   same script.
3. If date lock exists for the same `dream_run_id` and same
   `orchestrator_run_id`, ensure status exists and return duplicate/same-run.
4. If date lock exists for a different run, do not overwrite it. Return
   `date_locked`.
5. Never compare fences with greater-than or less-than semantics. The Worker
   date lock uses equality only.

Date lock value:

```json
{
  "dream_run_id": "dga_20260616_ab12cd34",
  "orchestrator_run_id": "pksn_20260616_230000_ab12cd34",
  "run_date": "2026-06-16",
  "mode": "shadow",
  "fencing_token": 17,
  "acquired_at": "2026-06-17T06:00:05.000Z"
}
```

TTL: 36 hours. The lock should outlive the nightly window but not accumulate
forever.

## Worker Execution State Machine

Use this Worker-side state machine:

```text
accepted
running_proposal
proposal_ready
running_grade
decision_ready
shadow_completed
running_apply
terminal
```

Terminal statuses:

```text
completed_shadow
completed
completed_with_holds
held
failed
rejected_live_disabled
rejected_shadow_mode
rejected_date_locked
rejected_fence_mismatch
rejected_superseded
```

Status writes must happen after every meaningful substage. A failed background
executor must catch the exception and write terminal `failed`.

## Async Execution Strategy

Implement the start endpoint as:

1. authenticate operator token
2. validate body with `zod`
3. atomically acquire or reattach to date lock/status
4. return `202 Accepted` or duplicate response quickly
5. invoke the executor with `ctx.waitUntil(executeScheduledGovernedDreamAsync(...))`

The executor must be idempotent:

- if status is terminal, exit
- if proposal already exists, reuse it
- if grade already exists, reuse it
- if mode is shadow, never apply
- if mode is live and live is disabled, write `rejected_live_disabled`
- if mode is live and enabled, run the live apply gate before apply

Cloudflare background execution can still be interrupted. Therefore every
substage must persist status before moving to the next substage. If Phase 2
staging proves `ctx.waitUntil` is not reliable for full Dream duration, do not
cut over; add a Phase 2b Workflows or Queue-backed executor before Phase 3.

This keeps Phase 2 simple while creating an explicit pivot point if the runtime
needs a durable queue.

## Shadow Execution Semantics

For `mode=shadow`:

- call `runDreamProposal`
- call `gradeDreamProposal`
- call `buildScheduledGovernedDecision`
- do not call `applyDreamProposal`
- status must include `executed_mode: "shadow"`
- status must include `applied_count: 0`
- status should include `selected_operation_ids` as "would apply" metadata
- status should include held operations and reasons

If shadow status ever has `applied_count > 0`, the orchestrator must treat it as
failed terminal. The Worker test suite must prove this cannot happen.

## Live Apply Gate

Implement a reusable gate even though production Phase 2 keeps live disabled.

`applyDreamProposal` may be reached from the async scheduled endpoint only if
all checks pass:

```text
env.PKS_ORCH_DREAM_LIVE_ENABLED == "1"
request.mode == "live"
date_lock.mode == "live"
date_lock.dream_run_id == request.run_id
date_lock.orchestrator_run_id == request.orchestrator_run_id
date_lock.fencing_token == request.fencing_token
date_lock.run_date == request.run_date
status.executed_mode == "live"
```

Any failure must write terminal status and skip apply:

- `rejected_live_disabled`
- `rejected_shadow_mode`
- `rejected_fence_mismatch`
- `rejected_superseded`

The gate should be pure and directly unit-tested.

## Worker Code Changes

Add:

```text
cloudflare-mcp/mcp-server/src/scheduledDreamAsync.ts
cloudflare-mcp/mcp-server/test/scheduled-async.test.ts
```

Prefer a new module so `src/index.ts` only handles routing.

Export from the new module:

```ts
export const scheduledDreamStartRequestSchema = z.object(...);
export async function startScheduledGovernedDreamAsync(env, ctx, requestBody): Promise<ResponsePayload>;
export async function getScheduledGovernedDreamStatus(env, runId): Promise<Record<string, unknown> | null>;
export async function executeScheduledGovernedDreamAsync(env, request): Promise<void>;
export function verifyScheduledGovernedLiveApplyGate(...): GateResult;
```

Refactor existing helpers as needed:

- move `buildScheduledGovernedDecision` and `verifyScheduledGovernedApply` into
  a shared module, or export them from `index.ts` only if that does not create a
  circular import.
- keep `runScheduledGovernedDream` working for the current cron and repair
  endpoint until Phase 5.

Add routes in `src/index.ts` near existing `/ops/dream/*` routes:

```text
POST /ops/dream/scheduled_governed/start
GET  /ops/dream/scheduled_governed/status
```

Do not add secrets to `wrangler.json`. Reuse `DREAM_OPERATOR_TOKEN` and existing
Upstash env vars.

## Orchestrator Code Changes

Replace the default Dream client only after Worker tests pass.

Add to `orchestrator/dream.py`:

```text
HttpDreamClient
```

Behavior:

- base URL from `DREAM_MCP_BASE_URL`
- operator token from `DREAM_OPERATOR_TOKEN`
- `start(...)` posts to `/ops/dream/scheduled_governed/start`
- `status(run_id)` gets `/ops/dream/scheduled_governed/status?run_id=...`
- timeouts are finite and explicit
- HTTP 404 status returns `None`
- HTTP non-2xx raises a recoverable stage error unless the response is a known
  terminal rejection payload

Phase 2 default:

- CLI should use `HttpDreamClient` only when `PKS_ORCH_DREAM_CLIENT=http`.
- Otherwise keep `Phase1ShadowDreamClient`.

This gives us a safe manual validation switch before Phase 3 changes the normal
shadow run path.

## Tests

### Worker Unit Tests

Add tests for:

- start validates request schema
- unauthorized start/status returns 401
- start writes accepted status before executor work
- duplicate start with same `dream_run_id` does not duplicate work
- same-date different-run start is rejected by date lock
- status returns 404 for unknown run id
- shadow execution never calls `applyDreamProposal`
- shadow terminal status includes `executed_mode=shadow`
- shadow terminal status includes `applied_count=0`
- missing `executed_mode` or `applied_count` is caught by validation helper
- live request rejected when `PKS_ORCH_DREAM_LIVE_ENABLED` is absent
- live apply gate rejects wrong mode
- live apply gate rejects wrong fence
- live apply gate rejects wrong run id
- live apply gate rejects wrong date
- terminal failed status is written when proposal throws
- no floating promises in route code

### Existing Worker Tests

Run:

```text
cd cloudflare-mcp/mcp-server
npm run type-check
npm run test:worker
```

Existing scheduled tests must still pass.

### Python Orchestrator Tests

Add tests for:

- `HttpDreamClient.start` sends operator Bearer token
- `HttpDreamClient.status` maps 404 to `None`
- `HttpDreamClient` preserves `executed_mode` and `applied_count`
- engine can complete a run using a fake HTTP-backed Dream client
- rejected Worker status becomes failed terminal in `DREAM_VERIFY`

Run:

```text
PYTHONDONTWRITEBYTECODE=1 ingestion/.venv/bin/python -m pytest orchestrator/tests -q
```

### Live Smoke Tests

After deploy to staging:

1. POST start with a unique future test date and `mode=shadow`.
2. Confirm response is accepted and has `executed_mode=shadow`.
3. Poll status until terminal.
4. Confirm `applied_count=0`.
5. Confirm no live entry count changed except proposal/status/audit keys.
6. POST duplicate start for same run id and confirm duplicate response.
7. POST same date with different run id and confirm `date_locked`.

After deploy to production:

1. Run the same smoke with a far-future test date.
2. Restore or delete only the test status/date-lock keys if needed.
3. Do not enable live mode.

## Acceptance Criteria

Phase 2 is complete only when:

- new Worker start/status endpoints exist
- Worker routes are operator-token authenticated
- date-lock acquisition is atomic
- duplicate start is idempotent
- same-date different-run is rejected
- shadow run reaches terminal status
- shadow status has `executed_mode=shadow`
- shadow status has `applied_count=0`
- live mode cannot apply in production Phase 2
- live gate rejection tests pass
- existing scheduled cron behavior is unchanged
- Python orchestrator can use the HTTP client behind an opt-in env switch
- Worker type-check passes
- Worker tests pass
- Python orchestrator tests pass
- staging smoke passes
- production shadow smoke passes without mutation

## Rollout Plan

1. Implement Worker module and tests.
2. Run Worker type-check and tests.
3. Implement Python `HttpDreamClient` behind `PKS_ORCH_DREAM_CLIENT=http`.
4. Run Python tests.
5. Deploy Worker to staging.
6. Run staging shadow smoke.
7. Deploy Worker to production.
8. Run production far-future shadow smoke.
9. Commit and push Phase 2.

Do not proceed to Phase 3 until all Phase 2 acceptance criteria pass.

## Builder Notes

Start with Worker tests. The dangerous thing in this phase is not missing a
field; it is accidentally creating a path where shadow can apply or a stale
same-date run can win. Make the date lock and live gate boring, explicit, and
easy to test.

Use the existing proposal, grade, bounded decision, caps, tripwires, and held
operations. Do not reimplement Dream apply.
