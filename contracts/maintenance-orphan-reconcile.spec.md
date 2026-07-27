---
divecha: 3
id: PKS-MAINT-ORPHAN-RECONCILE-001
status: green
objective: Add a run-start reconciler to the nightly semantic maintenance script that
  rolls back orphaned `prepared` maintenance outbox entries left by crashed prior
  runs, guarded by a revision-match check, so a stale orphan can no longer fail every
  night's cohort barrier.
scope:
  write:
  - scripts/nightly_semantic_maintenance.py
  - tests/python/test_nightly_semantic_maintenance.py
  - contracts/maintenance-orphan-reconcile.spec.md
  forbid:
  - '**/.env*'
  - cloudflare-mcp/mcp-server/src/**
  - distillation/run.py
  - archive/**
context:
  repo: /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system
  read_first:
  - scripts/nightly_semantic_maintenance.py
  - tests/python/test_nightly_semantic_maintenance.py
  - cloudflare-mcp/mcp-server/src/dream.ts
  facts:
  - 'The bug: `barrier()` fails when ANY `maintenance:outbox:*` key has a status outside
    SAFE_OUTBOX_STATUSES = {completed, derived_complete, rolled_back}. A `prepared`
    entry from a crashed prior run therefore fails the barrier every night, raising
    cohort_verification_failed and rolling back the current run''s good merges. Confirmed
    live: one orphan, task nsm-20260725T091529Z-ef7a53de-6db025937bb19dd6, canonical
    ke_b952a6d7535b + duplicate ke_74f824f30864; both entries still active at their
    pre-merge revisions => the merge never applied.'
  - The reconciler must run at run start, AFTER lock acquire + health check, BEFORE
    pre_verification and the cohort loop (run_night in scripts/nightly_semantic_maintenance.py).
    At that point this run has enqueued nothing, so every `prepared` outbox entry
    is necessarily an orphan from a prior run.
  - 'Rollback mechanism already exists: OperatorClient.rollback(task_id) -> POST /ops/maintenance/rollback;
    the Worker''s rollbackSemanticCandidateTask (cloudflare-mcp/mcp-server/src/dream.ts)
    restores BOTH entries from journal.before_snapshots and flips journal+task status
    to rolled_back. It is idempotent (returns early if already rolled_back) and requires
    >=2 before_snapshots.'
  - "SAFETY: rollback restores snapshots UNCONDITIONALLY. So the reconciler must first\
    \ read the outbox journal's `expected_revisions` and compare to each entry's CURRENT\
    \ revision in Redis. Only roll back when every current revision equals the journal's\
    \ expected revision (nothing touched the entries since prepare). If any diverged,\
    \ DO NOT roll back (it would clobber a newer state) \u2014 record it for human\
    \ review and leave it."
  - The reconciler is Python-only and reuses the existing rollback endpoint; it must
    not add a new Worker mutation path (scope.forbid blocks Worker src).
  - "Reads of outbox keys and entry revisions use the same Redis client the RedisRunStore\
    \ already holds (store.redis). Outbox journal fields: status, task_id, canonical_id,\
    \ duplicate_ids, expected_revisions (map id->int), before_snapshots. Entry key\
    \ is `knowledge:<id>` or `project:<id>`; current revision is entry.metadata.revision\
    \ (absent/None treated as 0 for comparison only if the journal also lacks it \u2014\
    \ otherwise a missing revision on a live entry is a divergence)."
  - "The run report already has a `rollbacks` list and a `warnings` list. Add a `reconciled`\
    \ list (rolled-back orphans) and record diverged/unresolvable orphans as warnings\
    \ + in a `reconcile_skipped` list. Reconciler must never raise and abort the run\
    \ on a skip \u2014 a skipped orphan just means the barrier may still fail this\
    \ night, which is strictly better than corrupting data."
interfaces:
- kind: function
  signature: reconcile_orphan_outbox(*, store, operator) -> dict[str, Any]
  location: 'scripts/nightly_semantic_maintenance.py (new function; called from run_night
    after health check, before pre_verification). Returns {"reconciled": [task_id...],
    "skipped": [{task_id, reason}...]} and the caller merges these into the run report.'
behaviors:
- id: B1
  when: a `prepared` outbox entry exists whose journal expected_revisions all equal
    the entries' current Redis revisions (a never-applied / cleanly-preparable orphan)
  then: the reconciler calls operator.rollback(task_id) exactly once for it and lists
    its task_id under reconciled; it makes the outbox status safe so a subsequent
    barrier passes
  examples:
  - in:
      outbox:
        maintenance:outbox:T:ke_A:
          status: prepared
          task_id: T
          expected_revisions:
            ke_A: 0
            ke_B: 0
          before_snapshots:
          - entry:
              id: ke_A
          - entry:
              id: ke_B
      entries:
        ke_A:
          metadata:
            revision: 0
        ke_B:
          metadata:
            revision: 0
    out:
      rollback_calls:
      - T
      reconciled:
      - T
      skipped: []
  check: distillation/venv/bin/python -m unittest tests.python.test_nightly_semantic_maintenance.NightlySemanticMaintenanceTests.test_reconcile_rolls_back_clean_orphan
- id: B2
  given: a `prepared` outbox entry whose one entry's current revision differs from
    the journal's expected_revision (state changed since prepare)
  when: the reconciler runs
  then: it does NOT call operator.rollback for it (no stale-snapshot restore), records
    it under skipped with a divergence reason, and adds a warning; the run continues
    without raising
  examples:
  - in:
      outbox:
        maintenance:outbox:T:ke_A:
          status: prepared
          task_id: T
          expected_revisions:
            ke_A: 0
            ke_B: 0
      entries:
        ke_A:
          metadata:
            revision: 2
        ke_B:
          metadata:
            revision: 0
    out:
      rollback_calls: []
      reconciled: []
      skipped:
      - task_id: T
        reason: revision_diverged
  check: distillation/venv/bin/python -m unittest tests.python.test_nightly_semantic_maintenance.NightlySemanticMaintenanceTests.test_reconcile_skips_diverged_snapshot_without_rollback
- id: B3
  given: no `prepared` outbox entries (all outbox statuses terminal/safe)
  when: the reconciler runs
  then: it makes zero rollback calls, reconciled and skipped are both empty, and the
    run proceeds unchanged
  examples:
  - in:
      outbox:
        maintenance:outbox:X:ke_C:
          status: completed
        maintenance:outbox:Y:ke_D:
          status: derived_complete
    out:
      rollback_calls: []
      reconciled: []
      skipped: []
  check: distillation/venv/bin/python -m unittest tests.python.test_nightly_semantic_maintenance.NightlySemanticMaintenanceTests.test_reconcile_noop_when_no_orphans
- id: B4
  when: the existing nightly semantic maintenance python test suite runs
  then: it still passes (no regression from the reconciler)
  check: make test-python-checker
loop:
  max_turns: 8
  max_consecutive_failures: 3
ledger:
- at: '2026-07-27T08:46:43.600927+00:00'
  turn: 0
  failing: []
  note: green
  receipt: /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/.git/divecha/runs/PKS-MAINT-ORPHAN-RECONCILE-001-3668d2ea7754/evidence.json
---
