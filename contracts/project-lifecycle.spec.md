---
schema_version: 1
spec_id: PKS-PROJECT-LIFECYCLE-001
status: draft
target_agent: either
scope:
  in:
  - cloudflare-mcp/mcp-server/src/**
  - shared/memory_policy.json
  - scripts/audit_memory_quality.py
  - tests/**
  out:
  - ingestion/**
  - distillation/**
  - tests/probes/**
  forbid:
  - mcp-server/**
  - distillation/run.py
  - '**/.env*'
  - archive/**
bet:
  if: a project entry has status active, last_touched older than the policy staleness
    window (default 90 days), and no access within the grace window (default 30 days)
  then: the nightly Dream run generates a governed project_status_transition proposal
    (active to dormant) that flows through the standard grade/apply/receipt machinery,
    and get_index labels dormant projects instead of presenting them as live work
  observable: on a staging corpus containing a 2024-dated active project, the nightly
    proposal includes the transition; after apply the entry carries status dormant
    with a consolidation receipt; get_index output distinguishes it; restore_entry
    returns it to active
invariants:
- id: INV1
  holds: status transitions happen only through the governed Dream proposal/apply
    path with a per-run cap (default 10); no code path mutates project status directly
  check_intent: unit test asserting the only writer of project status is the dream
    apply operation handler and that an eleventh candidate in one run is held with
    a cap reason
- id: INV2
  holds: explicit_save-typed and explicitly pinned projects are never proposed for
    transition regardless of staleness
  check_intent: unit test with a stale explicit_save project asserting no proposal
    is generated for it
- id: INV3
  holds: 'every applied transition is receipted and reversible: the entry records
    prior status, run id, and basis, and restore returns it to the exact prior status'
  check_intent: apply a transition on a fixture entry, assert the receipt fields,
    run the restore path, and assert the entry equals its pre-transition state
- id: INV4
  holds: get_index presents dormant projects distinctly (status field and ordering)
    and its active-project list contains no project whose status is dormant
  check_intent: unit test over a fixture index with mixed statuses asserting the returned
    structure separates or labels dormant entries
- id: INV5
  holds: 'staleness and grace windows are read from shared/memory_policy.json project_lifecycle
    block (already present: active_stale_after_days 90, grace 30), not hardcoded'
  check_intent: unit test overriding the policy values and asserting candidate selection
    follows the override
- id: INV6
  holds: no file outside scope.in is modified and no scope.forbid path is touched
    in the final diff
  check_intent: git diff --name-only is a subset of scope.in and excludes every scope.forbid
    path
gates:
- id: G1
  intent: 'INV1, INV2, and INV5 hold: governed-only transitions, pinned exemption,
    policy-driven windows'
  must_assert: INV1 sole-writer and cap tests, INV2 exemption test, and INV5 policy-override
    test pass in worker vitest; exit nonzero otherwise
  command: TODO
  requires_permission: false
- id: G2
  intent: 'INV3 and INV4 hold: receipted reversibility and honest get_index presentation'
  must_assert: INV3 apply/restore round-trip and INV4 index-labeling tests pass; exit
    nonzero otherwise
  command: TODO
  requires_permission: false
- id: G3
  intent: INV6 scope discipline and existing suites stay green
  must_assert: make worker-typecheck and make worker-test exit 0 and INV6 holds; exit
    nonzero otherwise
  command: TODO
  requires_permission: false
- id: G4
  intent: 'staging end-to-end: a seeded stale 2024 project transitions through the
    full nightly path and back (network, staging only)'
  must_assert: the staging nightly run proposes, grades, applies, and receipts the
    transition; get_index reflects it; restore_entry reverts it; production is not
    targeted; exit nonzero otherwise
  command: TODO
  requires_permission: true
review:
  mode: required
  command: TODO
  sees: &id001
  - diff
  - invariants
  - scope
budget:
  max_turns: 20
  max_consecutive_failures: 3
  preflight_estimate: required
kill:
  after_turns: 8
graduate: G1 through G3 exit 0, review verdict is pass, no scope.forbid path touched
scale: graduated AND G4 passes on staging AND the production one-time sweep (26 projects,
  dry-run list reviewed by Arjun first) is applied and get_index shows only genuinely
  live work as active
ledger:
  turns: 0
  consecutive_failures: 0
  blockers: []
  lessons: []
legacy:
  goal_condition: all non-permissioned gates exit 0 AND git diff --name-only is a
    subset of scope.in AND no scope.forbid path is modified
  kill_scale_graduate:
    kill: "INV3 reversibility cannot be satisfied after 8 turns (transitions cannot\
      \ be made restorable) \u2014 stop; an irreversible lifecycle is worse than pollution"
    graduate: G1 through G3 exit 0, review verdict is pass, no scope.forbid path touched
    scale: graduated AND G4 passes on staging AND the production one-time sweep (26
      projects, dry-run list reviewed by Arjun first) is applied and get_index shows
      only genuinely live work as active
  review:
    models:
    - council
    aggregation: worst_verdict_wins
    sees: *id001
---

## Context

Repo root: `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system`.
Design source: `docs/pks-foundational-upgrade-spec-2026-07-07.md` §3.5 (read it first).

Verified defect (2026-07-07): a project entry's `status` is LLM-assigned once at
extraction (`distillation/models/entries.py:584,729`, defaulting to "active")
and no code path in the repo ever transitions it. `active_project` context has
an infinite salience half-life (`shared/memory_policy.json:25`) and sits in
`identity_floor_context_types` (`:90`), so Dream's archive candidacy
(salience < 0.05, `dream.ts:1885-1889`) can never fire for it. The live index
shows 26 "active" projects including one-shot 2024 chat sessions ("SPX MA200
Strategy Backtest", touched 2024-08-25) alongside genuinely live work. The
exact detector needed already exists — `compute_m9_project_lifecycle`
(`scripts/audit_memory_quality.py:809-878`) flags active projects stale beyond
`project_lifecycle.active_stale_after_days` (90) minus a 30-day access grace —
but it is detection-only; nothing consumes it.

The task: introduce a `project_status_transition` Dream operation type
(active→dormant; dormant→active restore) whose candidate selection reuses the
M9 logic (port it into the Worker or call-share its policy block), wire it into
nightly proposal generation with its own per-run cap of 10, apply it through the
existing grade/apply/receipt/rollback machinery, and make `get_index`
(`index.ts:1748-1846`) label dormant projects so the thin index stops
presenting 2024 one-shots as live. "Dormant" is a new project status value —
keep the existing four (`active|paused|completed|abandoned`) parseable and map
nothing destructively; dormant is additive.

## Build Loop vs Product Loop

The build loop proves, offline and on staging: governed-only transitions with a
cap, pinned-project exemption, policy-driven windows, receipted reversibility,
honest index presentation, and green suites within scope. These gates prove the
implementation contract, not the product bet.

The product bet is that the project layer becomes trustworthy — that when any
session asks "what is Arjun working on", the answer reflects reality, and that
no genuinely live project ever gets mislabeled dormant (the cost asymmetry is
real: a false dormant on active work is worse than a stale active). That
calibration is only observable through the one-time production sweep review and
weeks of use. The coding model may not claim the product bet is satisfied
merely because gates pass.

## Verification Narrative

Offline: `make worker-test` runs the new cases — a fixture project touched 100
days ago with no recent access is proposed; the same fixture as explicit_save
is not; setting `active_stale_after_days` to 400 in a policy override removes
the proposal; an applied transition carries `{prior_status, run_id, basis}` and
restore reproduces the original entry; a `get_index` fixture call returns the
dormant project labeled and excluded from the active ordering; an 11th
candidate in one run is held with a cap reason. Permissioned: on staging, seed
a 2024-dated active project, run the nightly path, confirm the proposal →
grade → apply → receipt chain in the run summary, check `get_index` output,
then `restore_entry` and confirm reversal. Finally, `git diff --name-only` is
a subset of scope.in with no scope.forbid path.
