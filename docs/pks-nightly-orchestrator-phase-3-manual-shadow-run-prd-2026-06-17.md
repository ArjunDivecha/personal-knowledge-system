# PKS Nightly Orchestrator Phase 3 PRD

Date: 2026-06-17
Status: planned
Owner: PKS nightly orchestrator
Phase: 3 - controlled M4 shadow run with Worker-backed Dream

## Summary

Phase 3 proves the new M4-owned orchestrator can run end to end in shadow mode
against the real async Dream Worker endpoint built in Phase 2.

The goal is not to install launchd, disable old schedulers, or mutate live
memory. The goal is to exercise the full local state machine, durable ledger,
fencing lock, Worker-backed Dream start/status, report rendering, and comparison
workflow in the current production environment.

Phase 3 is the bridge between "the parts work" and "the M4 can own the night."

## Current State

Phase 1 delivered the local orchestrator package:

- `scripts/nightly_orchestrator.py`
- `orchestrator/` state machine, ledger, lock, preflight, report, stages, and
  Dream client abstraction
- shadow-only ingestion stage wrappers
- Lua-backed Redis fencing lock
- same-host resume
- report rendering from partial or complete ledger state

Phase 2 delivered the Worker-backed Dream contract:

- `POST /ops/dream/scheduled_governed/start`
- `GET /ops/dream/scheduled_governed/status?run_id=...`
- caller-supplied `dream_run_id`
- per-date async Dream lock
- server-enforced shadow mode
- live apply gate present but disabled
- staging and production shadow smoke tests passed

The old nightly schedules still remain the production source of truth.

## Product Goal

Run one controlled manual shadow orchestrator cycle from the anchored M4 that:

- acquires the orchestrator lock
- writes local and Redis ledger state
- runs all shadow ingestion and validation stages
- starts Dream through the Phase 2 HTTP client
- waits for the Worker terminal Dream status
- verifies `executed_mode=shadow` and `applied_count=0`
- writes JSON and Markdown reports
- leaves enough evidence to compare with the existing nightly artifacts

## Non-Goals

Phase 3 does not:

- install or modify launchd
- disable old GitHub, Cloudflare, or local schedulers
- run ingestion writes for real
- enable `PKS_ORCH_ALLOW_MUTATION`
- enable `PKS_ORCH_DREAM_LIVE_ENABLED`
- run Worker live apply
- delete old repair scripts
- change production cutover ownership

## Safety Model

Phase 3 must stay shadow-only.

Required environment:

```bash
PKS_ORCH_DREAM_CLIENT=http
DREAM_MCP_BASE_URL=https://mcp.dancing-ganesh.com
PKS_ORCH_ALLOW_MUTATION=0
BROWSER=/usr/bin/false
CI=1
GIT_TERMINAL_PROMPT=0
```

Allowed side effects:

- local orchestrator ledger under
  `ingestion/checkpoints/orchestrator_runs/{run_date}.json`
- local reports under `scripts/reports/pks-nightly-{run_date}.{json,md}`
- Redis orchestrator keys under `pks:orchestrator:*`
- Worker async Dream status/date-lock keys for the selected test date
- Dream proposal/grade/status artifacts created by the Worker shadow path

Forbidden side effects:

- active `knowledge:*` or `project:*` entry mutation
- archive or restore mutation
- vector metadata mutation for active entries
- thin index rebuild as a result of this phase
- live apply from the Worker async endpoint

## Run Strategy

Use two checks.

### Check A - Synthetic Shadow Proof

Run the orchestrator on a far-future date that cannot collide with real nightly
jobs. The recommended initial date is:

```bash
2099-12-29
```

If that date already has a completed Phase 3 ledger or async Dream date lock,
pick another far-future date. Do not delete real-date artifacts to make room for
a test.

This check proves the full M4 state machine and Worker HTTP Dream client.

### Check B - Nightly Artifact Comparison

Compare the Phase 3 report shape and Dream section against the latest existing
nightly artifacts:

- latest `scripts/reports/dream-sleep-*.md`
- latest `scripts/reports/check_overnight_dream_run_*.json`
- current `/health` response from `https://mcp.dancing-ganesh.com/health`

The comparison does not require counts to match exactly because Phase 3
ingestion stages are shadow no-ops. It must explain every expected difference.

Expected differences:

- Phase 3 ingestion saved counts are zero because ingestion remains shadow.
- Phase 3 Dream is async Worker-backed and shadow-only.
- Phase 3 may create a new proposal/grade/status artifact, but `applied_count`
  must remain zero.

Unexpected differences:

- active topic/project counts change
- archived count changes
- vector count changes
- report is missing or incomplete
- Dream status is rejected or failed
- the run needs a browser or manual auth prompt

## Commands

Use the repo Python environment:

```bash
cd "/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system"

RUN_DATE=2099-12-29

PKS_ORCH_DREAM_CLIENT=http \
DREAM_MCP_BASE_URL=https://mcp.dancing-ganesh.com \
PKS_ORCH_ALLOW_MUTATION=0 \
BROWSER=/usr/bin/false \
CI=1 \
GIT_TERMINAL_PROMPT=0 \
PYTHONDONTWRITEBYTECODE=1 \
ingestion/.venv/bin/python scripts/nightly_orchestrator.py preflight

PKS_ORCH_DREAM_CLIENT=http \
DREAM_MCP_BASE_URL=https://mcp.dancing-ganesh.com \
PKS_ORCH_ALLOW_MUTATION=0 \
BROWSER=/usr/bin/false \
CI=1 \
GIT_TERMINAL_PROMPT=0 \
PYTHONDONTWRITEBYTECODE=1 \
ingestion/.venv/bin/python scripts/nightly_orchestrator.py run --mode shadow --date "$RUN_DATE"

PYTHONDONTWRITEBYTECODE=1 \
ingestion/.venv/bin/python scripts/nightly_orchestrator.py report --date "$RUN_DATE"
```

If `DREAM_WAIT` times out but the Worker status later becomes terminal, resume
the same run:

```bash
PKS_ORCH_DREAM_CLIENT=http \
DREAM_MCP_BASE_URL=https://mcp.dancing-ganesh.com \
PKS_ORCH_ALLOW_MUTATION=0 \
BROWSER=/usr/bin/false \
CI=1 \
GIT_TERMINAL_PROMPT=0 \
PYTHONDONTWRITEBYTECODE=1 \
ingestion/.venv/bin/python scripts/nightly_orchestrator.py resume --date "$RUN_DATE"
```

Do not start a second run for the same date as a workaround.

## Evidence To Capture

For Check A, capture:

- command exit codes
- ledger path
- report JSON path
- report Markdown path
- orchestrator `status`
- all stage statuses
- Dream `dream_run_id`
- Dream terminal `status`
- Dream `executed_mode`
- Dream `applied_count`
- Dream `held_count`
- before/after `/health` thin-index counts

For Check B, capture:

- latest old nightly report path
- latest old Dream check JSON path
- comparison summary
- all expected differences
- any unexpected differences

Recommended evidence output:

```text
scripts/reports/phase3-shadow-run-{run_date}.json
scripts/reports/phase3-shadow-run-{run_date}.md
```

If no helper exists yet, write the evidence manually from the ledger/report and
commit it only if it contains no secrets.

## Acceptance Criteria

Phase 3 is complete only when all of these are true:

- preflight passes without opening a browser
- orchestrator run exits `0`
- local ledger exists for the chosen run date
- Redis ledger mirror exists for the chosen run date
- every stage is terminal-OK (`completed`, `completed_with_warnings`, or
  `completed_with_holds`)
- run status is terminal-OK
- `DREAM_START` used the HTTP Dream client
- Worker Dream status reached terminal
- Worker Dream terminal status is `completed_shadow`
- Dream `executed_mode` is `shadow`
- Dream `applied_count` is `0`
- report JSON and Markdown both exist
- report JSON has `complete=true`
- active topic/project/archive counts did not change except for explicitly
  allowed proposal/status artifacts
- comparison against old nightly artifacts is written and explains expected
  differences
- no launchd or scheduler state changed

## Failure Handling

If preflight fails:

- do not run the state machine
- fix the missing local dependency or env key
- rerun preflight

If `DREAM_START` is rejected:

- do not retry with a different mode
- inspect the Worker status/date-lock for the selected date
- choose a new far-future date only if the old date was synthetic and already
  consumed by a prior test

If `DREAM_WAIT` times out:

- check the Worker status endpoint for the same `dream_run_id`
- if terminal, run `resume --date "$RUN_DATE"`
- if not terminal, wait or inspect Worker logs
- do not call `run` again for the same date

If any active memory counts change unexpectedly:

- stop Phase 3
- preserve before/after evidence
- do not proceed to Phase 4
- investigate whether shadow called a mutating path

## Rollout Plan

1. Confirm `origin/main` contains the Phase 2 commit and the local tree is clean.
2. Confirm root `.env` has production Worker and Upstash keys.
3. Capture a before `/health` snapshot.
4. Run `preflight` with the Phase 3 environment.
5. Run the synthetic shadow proof.
6. If the run times out waiting for Dream, resume the same run after Worker
   terminal status appears.
7. Render the report from the ledger.
8. Capture an after `/health` snapshot.
9. Verify ledger/report/Dream acceptance criteria.
10. Compare against latest old nightly artifacts.
11. Update this PRD and the redesign status table with evidence.
12. Commit and push the Phase 3 evidence/docs.

## Gate To Phase 4

Do not install the launchd sidecar until Phase 3 passes.

Phase 4 may start only after:

- Phase 3 evidence is committed
- no active-memory mutation occurred
- the report is complete and understandable
- same-host resume behavior is either proven unnecessary or exercised after a
  controlled timeout
- old schedulers are still confirmed active as the production source of truth

## Builder Notes

The dangerous mistakes in Phase 3 are subtle:

- using `run` twice instead of `resume`
- testing a real date that collides with a live operation
- interpreting proposal/status writes as memory mutation
- ignoring a partial report because the final CLI exit code is nonzero
- accepting a shadow run with missing `executed_mode` or `applied_count`

Keep the proof boring. One synthetic date, one run id, one ledger, one report,
one comparison.
