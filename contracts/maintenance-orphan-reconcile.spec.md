---
divecha: 3
id: PKS-MAINT-ORPHAN-RECONCILE-001
status: green
objective: Add a run-start reconciler to the nightly semantic maintenance script that
  clears orphaned `prepared` maintenance outbox entries left by crashed prior runs,
  so a single stale orphan can no longer fail every night's cohort barrier and roll
  back the run's good merges.
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
  - 'The bug: barrier() fails when ANY maintenance:outbox:* key has a status outside
    SAFE_OUTBOX_STATUSES = {completed, derived_complete, rolled_back}. A `prepared`
    entry from a crashed prior run therefore fails the barrier every night, raising
    cohort_verification_failed and rolling back the current run''s good merges. Confirmed
    live (07-25..07-26 failures): exactly one orphan, task nsm-20260725T091529Z-ef7a53de-6db025937bb19dd6,
    canonical ke_b952a6d7535b + duplicate ke_74f824f30864, both entries still active
    at their pre-merge revisions and not archived => the merge never applied.'
  - The reconciler runs at run start, AFTER lock acquire + health check, BEFORE pre_verification
    and the cohort loop (run_night). At that point this run has enqueued nothing,
    so every `prepared` outbox entry is necessarily a prior-run orphan. Live-only
    (it mutates).
  - 'AIRTIGHT never-applied proof is required before touching an orphan: every entry
    in the journal must be PRESENT, at its EXPECTED revision, AND NOT archived. An
    applied merge bumps the canonical''s revision and archives the duplicate, so revision-mismatch
    OR archived => maybe partially applied => never touch it, record it for review.
    Absent revision field normalizes to 0 (Worker convention: revision ?? 0); a missing
    entry KEY is a divergence.'
  - "Two clear paths for a proven never-applied orphan. (a) If maintenance:task:<id>\
    \ exists: roll back via the EXISTING OperatorClient.rollback -> POST /ops/maintenance/rollback\
    \ (Worker's rollbackSemanticCandidateTask restores the identical snapshots and\
    \ marks it rolled_back). (b) DISCOVERED in build: the live orphan is TASK-LESS\
    \ \u2014 its maintenance:task:<id> is absent (only the outbox journal survives),\
    \ so the endpoint returns 400 maintenance_task_not_found and cannot clear it.\
    \ For that case, flip the outbox journal to `rolled_back` DIRECTLY (bookkeeping\
    \ status write, never an entry) \u2014 safe precisely because the never-applied\
    \ proof holds, so there is nothing to restore, only a stale marker to clear."
  - 'Python-only; no new Worker mutation path (scope.forbid blocks Worker src). Redis
    reads/writes use the client RedisRunStore already holds. Never raises: a skipped
    orphan is strictly safer than a corrupted entry.'
  - 'Report: add a `reconciled` list (cleared orphan task_ids) and, when non-empty,
    a `reconcile_skipped` list plus one warning per skip. Existing all-held-noop and
    cohort-rollback behavior is unchanged (Sol already fixed the score-scale and all-held
    bugs from the stale NIGHTLY-STALL.md note).'
interfaces:
- kind: function
  signature: reconcile_orphan_outbox(*, store, operator) -> dict[str, list]
  location: 'scripts/nightly_semantic_maintenance.py (new; called from run_night after
    health check, before pre_verification, live-only). Returns {reconciled: [task_id...],
    skipped: [{task_id, reason, detail}...]}; the caller merges these into the run
    report.'
behaviors:
- id: B1
  when: a proven never-applied `prepared` orphan HAS a maintenance:task:<id> record
  then: the reconciler clears it through the existing rollback endpoint (operator.rollback
    called once) and NOT by a direct outbox write; task_id is listed under reconciled
  examples:
  - in:
      orphan:
        task_id: T
        expected_revisions:
          ke_A: 0
          ke_B: 0
      entries:
        ke_A:
          revision: 0
          archived: false
        ke_B:
          revision: 0
          archived: false
      task_record_exists: true
    out:
      rollback_calls:
      - T
      terminalized: []
      reconciled:
      - T
      skipped: []
  check: distillation/venv/bin/python -m unittest tests.python.test_nightly_semantic_maintenance.NightlySemanticMaintenanceTests.test_reconcile_task_bearing_orphan_uses_rollback_endpoint
- id: B2
  when: a proven never-applied `prepared` orphan has NO maintenance:task:<id> record
    (the 400 maintenance_task_not_found case)
  then: the reconciler flips the outbox journal to rolled_back directly (no endpoint
    call) and lists task_id under reconciled
  examples:
  - in:
      orphan:
        task_id: T
        outbox_key: maintenance:outbox:T:ke_A
        expected_revisions:
          ke_A: 0
          ke_B: 0
      entries:
        ke_A:
          revision: 0
          archived: false
        ke_B:
          revision: 0
          archived: false
      task_record_exists: false
    out:
      rollback_calls: []
      terminalized:
      - maintenance:outbox:T:ke_A
      reconciled:
      - T
      skipped: []
  check: distillation/venv/bin/python -m unittest tests.python.test_nightly_semantic_maintenance.NightlySemanticMaintenanceTests.test_reconcile_task_less_orphan_terminalizes_outbox
- id: B3
  given: "a `prepared` orphan that might have partially applied \u2014 an entry whose\
    \ revision diverged, an archived entry, or a missing entry"
  when: the reconciler runs
  then: it takes NO action on it (no rollback, no terminalize) and records it under
    skipped with the reason (revision_diverged / entry_archived / entry_missing);
    the run continues without raising
  examples:
  - in:
      orphan:
        task_id: T
        expected_revisions:
          ke_A: 0
          ke_B: 0
      entries:
        ke_A:
          revision: 2
        ke_B:
          revision: 0
    out:
      rollback_calls: []
      terminalized: []
      reconciled: []
      skipped:
      - task_id: T
        reason: revision_diverged
  check: distillation/venv/bin/python -m unittest tests.python.test_nightly_semantic_maintenance.NightlySemanticMaintenanceTests.test_reconcile_skips_diverged_snapshot
    tests.python.test_nightly_semantic_maintenance.NightlySemanticMaintenanceTests.test_reconcile_skips_archived_entry
    tests.python.test_nightly_semantic_maintenance.NightlySemanticMaintenanceTests.test_reconcile_skips_when_entry_missing
- id: B4
  given: no `prepared` outbox entries
  when: the reconciler runs
  then: it makes zero rollback calls, zero terminalizations, empty reconciled and
    skipped; the run proceeds unchanged
  examples:
  - in:
      orphan: none
    out:
      rollback_calls: []
      terminalized: []
      reconciled: []
      skipped: []
  check: distillation/venv/bin/python -m unittest tests.python.test_nightly_semantic_maintenance.NightlySemanticMaintenanceTests.test_reconcile_noop_when_no_orphans
- id: B5
  when: the full python test suite runs
  then: it still passes (no regression from the reconciler, and run_night wires it
    in live mode)
  check: make test-python-checker
loop:
  max_turns: 8
  max_consecutive_failures: 3
ledger:
- at: '2026-07-27T08:55:09.980563+00:00'
  turn: 0
  failing: []
  note: green
  receipt: /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/.git/divecha/runs/PKS-MAINT-ORPHAN-RECONCILE-001-3668d2ea7754/evidence.json
---
