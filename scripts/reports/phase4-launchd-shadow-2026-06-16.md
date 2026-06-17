# Phase 4 Launchd Shadow Evidence - 2026-06-16

Generated: 2026-06-17T16:19:38Z

## Verdict

Phase 4 shadow sidecar passed. The orchestrator ledger reached `DONE` with
`completed_with_holds`; Worker Dream was terminal `completed_shadow`,
`executed_mode=shadow`, and `applied_count=0`.

Legacy comparison found a separate failure: the old 02:00 second-chance
ingestion run failed overall because Twitter ingestion refused to run from local
state when Redis-backed state was missing. I repaired that state after the run
and verified Twitter from Redis state.

## Phase 4 Shadow Sidecar

- ledger: `ingestion/checkpoints/orchestrator_runs/2026-06-16.json`
- report: `scripts/reports/pks-nightly-2026-06-16.{json,md}`
- orchestrator run: `pksn_20260616_222500_e8e6b9c6`
- Dream run: `dga_20260616_e8e6b9c6`
- status: `completed_with_holds`
- current stage: `DONE`
- ingestion stages: shadow no-op, `saved=0`

Scheduled launchd follow-ups from 23:20 through 08:50 reattached to the same
completed `run_date=2026-06-16` ledger and no-op'd. This is expected because the
accelerated 22:25 PT validation had already completed that run date.

## Worker Dream

- terminal state: `terminal`
- status: `completed_shadow`
- requested mode: `shadow`
- executed mode: `shadow`
- applied count: `0`
- selected operations: `60`
- held operations: `12`
- proposal: `dpr_2026-06-17T05-25-14-537Z`
- grade: `dpg_2026-06-17T05-25-21-509Z`

No active-memory mutation came from Phase 4: local ingestion stages were
shadow-only and Worker Dream applied zero operations.

## Legacy Comparison

The legacy 02:00 second-chance run did mutate storage, as expected while legacy
remains the source of truth. It did not finish green:

- final verdict: `FAILED`
- failed stage: `Twitter ingestion(rc=1)`
- reason: Twitter required Redis-backed state, but only local file state was
  available at run time.
- repair: ran `ingestion/twitter/run.py --sync-state-only`
- verification: ran `ingestion/twitter/run.py --require-redis-state`; it loaded
  Redis state and exited 0 with no new tweets to process.

Other legacy stages completed:

- GitHub: 56 repositories processed, 2619 entries saved
- GitHub dedupe markers after run: 56 repo markers, 14 agent-context markers
- Agent sessions: 23 files yielded 88 entries
- Dream judge: 0 pending items

## Scheduler State

- `com.arjun.pks-nightly-orchestrator.shadow`: loaded, not running, last exit 0
- `com.arjun.knowledge-ingestion`: loaded, not running, restored during this
  check after a manual bootout the prior evening
- `com.arjun.knowledge-ingestion-2am`: loaded, not running, last exit 1 from
  the repaired legacy run

## Follow-Up

Phase 4 itself is acceptable. The legacy Twitter-state failure is repaired, but
tonight's run should be watched because the success marker for the overnight
legacy run remains `ok=false` for historical accuracy.
