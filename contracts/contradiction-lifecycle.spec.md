---
schema_version: 1
spec_id: PKS-CONTRADICTION-LIFECYCLE-001
status: draft
target_agent: either
scope:
  in:
  - ingestion/**
  - distillation/models/entries.py
  - cloudflare-mcp/mcp-server/src/**
  - scripts/**
  - tests/**
  - shared/memory_policy.json
  out:
  - distillation/run.py
  - tests/probes/**
  forbid:
  - mcp-server/**
  - '**/.env*'
  - archive/**
bet:
  if: extraction produces evidence, or a contested entry's counterpart entries have
    been archived, merged, or resolved
  then: evidence carries asserted_by (user|assistant|inferred) and assertion_kind
    (decision|preference|correction|fact|hypothesis); precedence between conflicting
    claims is decided by an authority-then-durability lattice, never naive recency;
    and orphaned contested states auto-revert to active with a receipt
  observable: fixture-replay extractions show populated provenance fields; the lattice
    comparator is unit-tested against a labeled pair set; a dry-run fossil sweep lists
    exactly the contested entries with dangling counterparts and an apply run clears
    them with consolidation_notes receipts
invariants:
- id: INV1
  holds: 'asserted_by derives correctly from message role at extraction: user-authored
    messages yield user, assistant messages yield assistant, extractor generalizations
    yield inferred; missing provenance is treated as inferred/hypothesis (lowest rank),
    never invented'
  check_intent: unit tests over extraction fixtures with known roles asserting the
    persisted asserted_by/assertion_kind values, including a no-role fixture mapping
    to inferred
- id: INV2
  holds: 'the precedence comparator implements the lattice: any user assertion outranks
    any assistant assertion regardless of recency; decision > preference > fact >
    hypothesis within equal authority; equal authority and kind falls back to as_of
    recency; behavioral-vs-stated user conflicts return escalate, never an automatic
    winner'
  check_intent: table-driven unit test over a labeled pair set covering every lattice
    cell and the escalate case, asserting the comparator verdicts
- id: INV3
  holds: the fossil sweep in dry-run mode performs zero writes to Redis or Vector
  check_intent: run the sweep in dry-run against a storage spy/staging snapshot and
    assert the write-call count is exactly zero while the candidate list is non-empty
    on a fixture with known fossils
- id: INV4
  holds: every state change made by the sweep or the lifecycle (contested->active
    revert, supersession) appends a consolidation_notes receipt naming the run id,
    basis, and counterpart ids, and is reversible via the entry's revision history
  check_intent: apply the sweep on a staging fixture; assert each mutated entry gained
    a receipt and that restore paths reproduce the prior state
- id: INV5
  holds: 'schema changes are additive and backward-compatible: entries without the
    new fields still parse, and no existing field is renamed or removed'
  check_intent: round-trip parse tests on pre-change entry JSON fixtures asserting
    from_dict/to_dict succeed unchanged
- id: INV6
  holds: no file outside scope.in is modified and no scope.forbid path is touched
    in the final diff
  check_intent: git diff --name-only is a subset of scope.in and excludes every scope.forbid
    path
gates:
- id: G1
  intent: 'INV1 and INV5 hold: provenance capture is correct and the schema change
    is additive'
  must_assert: INV1 role-mapping fixtures and INV5 backward-compat round-trips pass
    in the python test suite; exit nonzero naming the failing fixture otherwise
  command: TODO
  requires_permission: false
- id: G2
  intent: 'INV2 holds: the precedence lattice comparator is correct on the full labeled
    pair set'
  must_assert: INV2 table-driven cases all pass, including user-beats-assistant-regardless-of-recency
    and the behavioral-vs-stated escalate case; exit nonzero listing failing cells
    otherwise
  command: TODO
  requires_permission: false
- id: G3
  intent: 'INV3 and INV4 hold: dry-run sweep is write-free and applied changes are
    receipted and reversible'
  must_assert: INV3 (zero writes in dry-run) and INV4 (receipts present, restore reproduces
    prior state) pass against fixtures; exit nonzero otherwise
  command: TODO
  requires_permission: false
- id: G4
  intent: INV6 scope discipline and the existing python + worker suites stay green
  must_assert: make test-python-checker, make worker-typecheck, and make worker-test
    exit 0, and INV6 holds; exit nonzero otherwise
  command: TODO
  requires_permission: false
- id: G5
  intent: 'production dry-run of the fossil sweep: enumerate real contested entries
    with dangling counterparts, read-only (network)'
  must_assert: the sweep runs against production in dry-run, writes nothing (INV3
    semantics), and emits a reviewable JSON list of contested entries whose contradicts
    targets are archived/merged/self-referential; exit nonzero on any write attempt
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
scale: graduated AND G5 dry-run list reviewed and approved by Arjun AND the apply
  run clears the fossil contested set with receipts AND new extractions in production
  carry asserted_by
ledger:
  turns: 0
  consecutive_failures: 0
  blockers: []
  lessons: []
legacy:
  goal_condition: all non-permissioned gates exit 0 AND git diff --name-only is a
    subset of scope.in AND no scope.forbid path is modified
  kill_scale_graduate:
    kill: INV2 lattice cases cannot be made to pass after 10 turns, or the additive
      schema change breaks existing entry parsing irreparably (INV5)
    graduate: G1 through G4 exit 0, review verdict is pass, no scope.forbid path touched
    scale: graduated AND G5 dry-run list reviewed and approved by Arjun AND the apply
      run clears the fossil contested set with receipts AND new extractions in production
      carry asserted_by
  review:
    models:
    - council
    aggregation: worst_verdict_wins
    sees: *id001
---

## Context

Repo root: `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system`.
Design source: `docs/pks-foundational-upgrade-spec-2026-07-07.md` §4 (read it first).

Three defects this contract fixes, all verified live on 2026-07-07:

1. **Provenance discards the speaker.** `message.role` is available during
extraction (`distillation/pipeline/filter.py:114-125`, `extract.py:155`) but
`Evidence` (`distillation/models/entries.py:139-146`) persists only
`conversation_id + message_ids + snippet`. No field anywhere distinguishes
"Arjun said this" from "an assistant suggested this". Add `asserted_by`
(user|assistant|inferred) and `assertion_kind`
(decision|preference|correction|fact|hypothesis) to `Evidence`, populated at
extraction; surface an entry-level rollup (max authority over evidence).
Additive only — old entries must keep parsing (missing = inferred/hypothesis).

2. **No precedence model.** Implement the lattice comparator (authority:
arjun_explicit > arjun_behavioral > assistant_asserted > inferred; durability:
decision > preference > fact > hypothesis; recency only as the final tiebreak;
behavioral-vs-stated user conflicts → escalate verdict) as a pure, unit-tested
function usable from both the Worker (TypeScript) and Python. Keep the two
implementations in lockstep the way `salience.ts`/`salience.py` already are.

3. **Contested is a fossil state.** Example: `ke_4dbf732e757d` was marked
contested daily through April 2026 by a same-domain/low-similarity heuristic;
its conflicting entries were later merged INTO it; its `contradicts` links now
point at absorbed entries including itself; recent Dream runs report
`contradictions_detected: 0`; nothing ever reverts the state. Build the fossil
sweep: for each contested entry, if all `contradicts` counterparts are archived,
merged, self-referential, or missing, revert state to active with a
consolidation_notes receipt. Dry-run mode (JSON list, zero writes) is the
default; apply mode requires an explicit flag and is Arjun-gated (see G5 and
kill_scale_graduate).

## Build Loop vs Product Loop

The build loop proves, offline: role→asserted_by mapping on fixtures, additive
schema round-trips, every cell of the precedence lattice on a labeled pair set,
zero-write dry-run behavior, and receipted reversible applies on staging
fixtures — plus the existing python and worker suites staying green. These gates
prove the contract, not the product bet.

The product bet is epistemic: that contested-at-tier-1 disappears as a standing
state, that future contradictions resolve by authority rather than recency, and
that the store stops injecting inconsistent knowledge. That is only observable
after the production sweep is applied and weeks of new ingestion carry
provenance. The coding model may not claim the product bet is satisfied merely
because gates pass — in particular, a green lattice comparator does not prove
the lattice's rankings are the *right* rankings; that judgment stays with Arjun
via the escalate path and digest review.

## Verification Narrative

Offline: `make test-python-checker` runs the new fixture tests — an extraction
fixture with a user correction yields `asserted_by: user, assertion_kind:
correction`; a pre-change entry JSON round-trips unchanged; the lattice table
test prints its full verdict matrix; the sweep in dry-run against the bundled
fossil fixture emits the expected candidate list and a write-spy count of zero.
`make worker-test` covers the TypeScript comparator twin with the same table.
Permissioned: run the sweep against production with `--dry-run`, inspect the
emitted JSON list (expect the known April fossils, e.g. `ke_4dbf732e757d`),
confirm via `get_validation_status`/entry reads that nothing changed. Apply mode
runs only after Arjun approves the list. Finally, `git diff --name-only` is a
subset of scope.in with no scope.forbid path.
