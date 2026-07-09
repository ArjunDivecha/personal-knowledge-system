---
schema_version: 1
spec_id: PKS-ADMISSION-DEDUP-001
status: draft
target_agent: either
scope:
  in:
  - ingestion/**
  - tests/python/**
  - shared/memory_policy.json
  out:
  - distillation/**
  - tests/probes/**
  forbid:
  - mcp-server/**
  - cloudflare-mcp/mcp-server/src/**
  - distillation/run.py
  - '**/.env*'
  - archive/**
bet:
  if: ingestion extracts a candidate entry whose top-1 semantic neighbor among active
    same-type entries has cosine similarity at or above the append threshold (default
    0.85)
  then: the candidate is routed as an evidence-append to the existing entry (new Evidence
    appended, mention_count incremented, last_seen refreshed, source conversation
    added) instead of minting a new entry; mid-band candidates (0.70-0.85) are admitted
    but linked related; below-band candidates are admitted unchanged
  observable: a dry-run replay of a fixture ingestion batch containing known re-observations
    logs append/link/new decisions per candidate with neighbor ids and scores, creates
    zero new entries for the known duplicates in live mode, and shows their mention_count
    incremented instead
invariants:
- id: INV1
  holds: "evidence is conserved on append: the appended Evidence (conversation_id,\
    \ message_ids, snippet, and provenance fields when present) is exactly what a\
    \ newly minted entry would have carried \u2014 nothing dropped, nothing rewritten"
  check_intent: unit test comparing the Evidence object persisted by the append path
    against the one the mint path would create for the same candidate; assert field-level
    equality
- id: INV2
  holds: "append targets are only active, non-contested, same-type entries; archived\
    \ or contested neighbors are never appended to \u2014 such candidates are admitted\
    \ as new entries"
  check_intent: unit tests with archived and contested top-1 neighbors above threshold
    asserting the decision is new-entry, not append
- id: INV3
  holds: dry-run mode performs zero writes while emitting the full decision log; live
    mode is reachable only via an explicit flag that defaults off
  check_intent: run the fixture batch in dry-run against a storage spy and assert
    write-call count is zero while the decision log contains every candidate; assert
    the flag default is off in config
- id: INV4
  holds: 'below-threshold admission is byte-identical to current behavior: candidates
    with no neighbor at or above 0.70 produce exactly the entry they produce today'
  check_intent: golden-file test replaying a no-duplicate fixture batch with the feature
    on and off, diffing the resulting entries
- id: INV5
  holds: thresholds and the enable flag live in shared/memory_policy.json (admission_dedup
    block), not hardcoded
  check_intent: unit test loading policy overrides and asserting the router honors
    changed thresholds without code edits
- id: INV6
  holds: no file outside scope.in is modified and no scope.forbid path is touched
    in the final diff
  check_intent: git diff --name-only is a subset of scope.in and excludes every scope.forbid
    path
gates:
- id: G1
  intent: 'INV1 and INV4 hold: appends conserve evidence and non-duplicates are untouched
    by the feature'
  must_assert: INV1 field-level evidence equality and INV4 golden-file byte-identity
    pass in tests/python; exit nonzero naming the divergence otherwise
  command: TODO
  requires_permission: false
- id: G2
  intent: 'INV2 and INV5 hold: routing respects entry state and policy-driven thresholds'
  must_assert: INV2 archived/contested-neighbor cases route to new-entry and INV5
    policy-override cases pass; exit nonzero otherwise
  command: TODO
  requires_permission: false
- id: G3
  intent: 'INV3 holds: dry-run is write-free with a complete decision log, and the
    live flag defaults off'
  must_assert: 'INV3 passes: storage-spy write count is zero in dry-run, every fixture
    candidate appears in the decision log with a decision and neighbor score, and
    the default config value is off; exit nonzero otherwise'
  command: TODO
  requires_permission: false
- id: G4
  intent: INV6 scope discipline and the existing python suite stay green
  must_assert: make test-python-checker exits 0 and INV6 holds (git diff subset of
    scope.in, no forbid path); exit nonzero otherwise
  command: TODO
  requires_permission: false
- id: G5
  intent: 'live shadow run: one real ingestion cycle in dry-run against production
    data, decision log reviewed (network, read-only writes-wise)'
  must_assert: a real ingestion run with admission_dedup in dry-run emits a decision
    log whose append-rate and neighbor scores are plausible (spot-checked against
    known duplicate clusters like the curate-my-world categoryMapping entries); zero
    entry writes attributable to the router; exit nonzero on any router-attributed
    write
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
  max_turns: 30
  max_consecutive_failures: 3
  preflight_estimate: required
kill:
  after_turns: 10
graduate: G1 through G4 exit 0, review verdict is pass, no scope.forbid path touched
scale: graduated AND G5 decision log reviewed by Arjun AND live mode enabled AND over
  the next 7 days the nightly duplicate-merge candidate count trends down instead
  of up
ledger:
  turns: 0
  consecutive_failures: 0
  blockers: []
  lessons: []
legacy:
  goal_condition: all non-permissioned gates exit 0 AND git diff --name-only is a
    subset of scope.in AND no scope.forbid path is modified
  kill_scale_graduate:
    kill: "INV1 evidence conservation cannot be satisfied after 10 turns (the append\
      \ path structurally loses provenance) \u2014 stop; a lossy admission router\
      \ is worse than duplicates"
    graduate: G1 through G4 exit 0, review verdict is pass, no scope.forbid path touched
    scale: graduated AND G5 decision log reviewed by Arjun AND live mode enabled AND
      over the next 7 days the nightly duplicate-merge candidate count trends down
      instead of up
  review:
    models:
    - council
    aggregation: worst_verdict_wins
    sees: *id001
---

## Context

Repo root: `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system`.
Design source: `docs/pks-foundational-upgrade-spec-2026-07-07.md` §3.2 (read it first).

The verified defect: ingestion re-mints entries for knowledge it already holds.
Live example from 2026-07-07: `ke_871c4c235a94` and `ke_f4f7bbfb8411` were both
distilled from the same two source files (curate-my-world's `CLAUDE.md` and
`.claude/CLAUDE.md`) on different ingestion days with slightly different domain
names. The corpus grew ~8,700 → ~10,300 entries in 48 hours while nightly Dream
merges are capped at 10 — consolidation cannot win downstream; the fix is at
admission. There is currently no retrieve-before-admit step anywhere under
`ingestion/` (`ingestion/core/storage.py` writes entries; `extractor.py`
produces candidates; per-source runners orchestrate).

The task: before storing a new candidate entry, embed it (the embedding client
already exists in `ingestion/core/` — production uses text-embedding-3-large @
3072 dims; never the legacy 1536-dim model) and query Upstash Vector for the
top-1 active same-type neighbor. Route: cosine ≥ 0.85 → evidence-append to the
neighbor; 0.70–0.85 → admit new but link `related` to the neighbor; < 0.70 →
admit unchanged. Contested/archived neighbors are never append targets.
Thresholds and the enable flag live in `shared/memory_policy.json` under a new
`admission_dedup` block; the feature defaults to dry-run (decision log only).
Note the side effect that makes this doubly valuable: `mention_count` currently
only increments on Dream merges, so appends turn repetition into a real
corroboration signal that salience v2 (PKS-INJECTION-RANKING-002) consumes.

## Build Loop vs Product Loop

The build loop proves, offline: evidence conservation on append, state-aware
routing, policy-driven thresholds, write-free dry-run, byte-identical
below-threshold behavior, and suite/scope discipline. A permissioned shadow run
proves the decision log is sane on real data. These gates prove the
implementation contract, not the product bet.

The product bet is that the duplicate factory shuts down: new-entry rate bends
visibly, the nightly duplicate-merge candidate count (92→161 and rising as of
2026-07-07) reverses trend, and the consolidation backlog becomes drainable.
That is only observable after live enablement over days of real ingestion. The
coding model may not claim the product bet is satisfied merely because gates
pass — in particular, a clean dry-run log does not prove the 0.85 threshold is
right; that calibration is exactly what the reviewed shadow period is for.

## Verification Narrative

Offline: `make test-python-checker` runs the new cases — an append-path
Evidence object equals the mint-path one field-for-field; an archived top-1
neighbor above threshold still yields a new entry; a policy override to 0.90
changes routing; the golden-file no-duplicate batch is byte-identical with the
feature on/off; the dry-run storage spy records zero writes while the decision
log names every candidate. Permissioned: run one real ingestion cycle (e.g.
`cd ingestion && python github/run.py` with admission_dedup in dry-run),
open the decision log, and confirm known re-observations (the categoryMapping
cluster's source files) appear with append decisions and high neighbor scores.
Finally, `git diff --name-only` is a subset of scope.in with no scope.forbid
path.
