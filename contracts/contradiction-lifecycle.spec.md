---
schema_version: 1
spec_id: PKS-CONTRADICTION-LIFECYCLE-001
status: draft
target_agent: either
scope:
  in:
  - ingestion/**
  - distillation/models/entries.py
  - distillation/pipeline/**
  - distillation/utils/precedence.py
  - cloudflare-mcp/mcp-server/src/**
  - cloudflare-mcp/mcp-server/test/**
  - scripts/**
  - tests/**
  - shared/memory_policy.json
  - shared/precedence_fixtures.json
  out:
  - distillation/run.py
  - tests/probes/**
  forbid:
  - mcp-server/**
  - '**/.env*'
  - archive/**
  - shared/salience_fixtures.json
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
- id: G0
  intent: 'premise gate: the mechanisms this contract builds on exist as diagnosed
    (roles available at extraction; Evidence schema present; contested state and
    contradicts links exist in the Worker)'
  must_assert: message roles are exposed in both extraction pipelines, the Evidence
    dataclass exists, and the Worker manipulates contested state via contradicts
    links; exit nonzero if any premise has drifted
  command: |
    grep -q "class Evidence" distillation/models/entries.py || { echo "G0 FAIL: Evidence dataclass missing"; exit 1; }
    grep -q 'm.role == "user"' distillation/pipeline/filter.py || { echo "G0 FAIL: roles no longer visible in distillation filter"; exit 1; }
    grep -q '"role"' ingestion/agent_sessions/parsers.py || { echo "G0 FAIL: roles no longer parsed in agent-sessions ingestion"; exit 1; }
    grep -q '"contradicts"' cloudflare-mcp/mcp-server/src/dream.ts || { echo "G0 FAIL: contradicts links gone from Worker"; exit 1; }
    echo "G0 PASS: all premises hold"
  requires_permission: false
- id: G1
  intent: 'INV1 and INV5 hold: provenance capture is correct and the schema change
    is additive'
  must_assert: INV1 role-mapping fixtures and INV5 backward-compat round-trips pass
    in the python test suite; exit nonzero naming the failing fixture otherwise
  command: |
    distillation/venv/bin/python -m unittest -v tests.python.test_provenance_capture
  requires_permission: false
- id: G2
  intent: 'INV2 holds: the precedence lattice comparator is correct on the full labeled
    pair set, in BOTH the python and TypeScript implementations against the same
    shared fixture table'
  must_assert: INV2 table-driven cases all pass, including user-beats-assistant-regardless-of-recency
    and the behavioral-vs-stated escalate case; exit nonzero listing failing cells
    otherwise
  command: |
    distillation/venv/bin/python -m unittest -v tests.python.test_precedence_lattice || exit 1
    cd cloudflare-mcp/mcp-server && npx vitest run test/precedence.test.ts --no-file-parallelism
  requires_permission: false
- id: G3
  intent: 'INV3 and INV4 hold: dry-run sweep is write-free and applied changes are
    receipted and reversible'
  must_assert: INV3 (zero writes in dry-run) and INV4 (receipts present, restore reproduces
    prior state) pass against fixtures; exit nonzero otherwise
  command: |
    distillation/venv/bin/python -m unittest -v tests.python.test_contested_fossil_sweep
  requires_permission: false
- id: G4
  intent: INV6 scope discipline and the existing python + worker suites stay fully
    green (no allowlists)
  must_assert: the tests/python suite, worker typecheck, and worker vitest suite all
    exit 0, and INV6 holds; exit nonzero otherwise
  command: |
    distillation/venv/bin/python -m unittest discover -s tests/python -p 'test_*.py' > /tmp/pks_cl_g4_py.log 2>&1 || { tail -5 /tmp/pks_cl_g4_py.log; echo "G4 FAIL: python suite"; exit 1; }
    make worker-typecheck > /tmp/pks_cl_g4_tc.log 2>&1 || { tail -5 /tmp/pks_cl_g4_tc.log; echo "G4 FAIL: worker typecheck"; exit 1; }
    cd cloudflare-mcp/mcp-server && npx vitest run --no-file-parallelism > /tmp/pks_cl_g4_wk.log 2>&1 || { tail -5 /tmp/pks_cl_g4_wk.log; echo "G4 FAIL: worker suite"; exit 1; }
    echo "G4 PASS: both suites fully green"
  requires_permission: false
- id: G5
  intent: 'production dry-run of the fossil sweep: enumerate real contested entries
    with dangling counterparts, read-only (network)'
  must_assert: the sweep runs against production in dry-run, writes nothing (INV3
    semantics), and emits a reviewable JSON list of contested entries whose contradicts
    targets are archived/merged/self-referential; exit nonzero on any write attempt
  command: |
    distillation/venv/bin/python scripts/sweep_contested_fossils.py --dry-run
  requires_permission: true
review:
  mode: required
  command: |
    DIFF=$(git diff HEAD -- distillation/models/entries.py distillation/pipeline/ distillation/utils/precedence.py ingestion/ cloudflare-mcp/mcp-server/src/precedence.ts scripts/sweep_contested_fossils.py shared/precedence_fixtures.json; git status --porcelain -- tests/ cloudflare-mcp/mcp-server/test/)
    PROMPT="Static code review only — do NOT execute shell commands or run any test suite (the sandbox TMPDIR is broken; review by reading only, use your file-read tool for the new test files listed as untracked). Review this diff against contract PKS-CONTRADICTION-LIFECYCLE-001 for correctness bugs or violations of:
    INV1: asserted_by derives from message role (user msg -> user, assistant -> assistant, missing/unknown -> inferred); never invented.
    INV2: the precedence lattice — any user assertion outranks any assistant assertion regardless of recency; behavioral(3) vs user(4) returns escalate in both orders; decision>preference>fact>hypothesis within equal authority; recency only as final tiebreak; python and TypeScript implementations must be semantically identical against shared/precedence_fixtures.json.
    INV3: the fossil sweep in dry-run performs zero store writes, structurally.
    INV4: every applied sweep change appends a consolidation_notes receipt naming run id, basis, prior state, counterpart ids.
    INV5: schema change is additive — old entry JSON round-trips unchanged; new keys omitted when None.
    INV6: diff confined to the contract scope.
    Respond with a single final line exactly 'REVIEW: PASS' or 'REVIEW: FAIL' plus the blocking issue. Nits do not block.

    DIFF:
    $DIFF"
    codex exec "$PROMPT" --sandbox read-only --skip-git-repo-check -m gpt-5.5 -c model_reasoning_effort="high" > /tmp/pks_cl_review.log 2>&1
    tail -5 /tmp/pks_cl_review.log
    grep -q "REVIEW: PASS" /tmp/pks_cl_review.log && exit 0
    echo "REVIEW GATE FAIL — see /tmp/pks_cl_review.log"
    exit 1
  sees: &id001
  - diff
  - invariants
  - scope
budget:
  max_turns: 30
  max_consecutive_failures: 3
  preflight_estimate: complete
  # Presented 2026-07-10 (Fable authoring + Opus implementation subagent):
  # ~10 files — entries.py (+2 optional Evidence fields, serialization sites),
  # new distillation/utils/precedence.py + src/precedence.ts twins with
  # shared/precedence_fixtures.json (>=20 lattice cases), extraction wiring in
  # distillation/pipeline/extract.py + corrections.py + ingestion/core/extractor.py
  # (+agent_sessions if evidence has message basis), new
  # scripts/sweep_contested_fossils.py (dry-run default, double-flag apply),
  # 3 new python test modules + 1 vitest module. Scope.in amended by author:
  # added distillation/pipeline/**, distillation/utils/precedence.py,
  # cloudflare-mcp/mcp-server/test/**, shared/precedence_fixtures.json (the
  # original scope was authored too narrowly to reach the extraction sites the
  # contract's own Context names). G5 + apply remain stage-5, Arjun-gated.
kill:
  after_turns: 10
graduate: G1 through G4 exit 0, review verdict is pass, no scope.forbid path touched
scale: graduated AND G5 dry-run list reviewed and approved by Arjun AND the apply
  run clears the fossil contested set with receipts AND new extractions in production
  carry asserted_by
ledger:
  turns: 2
  consecutive_failures: 0
  blockers:
  - 'RESOLVED 2026-07-11T04:33:34Z with Arjun''s go-ahead: G5 ran clean
    against production (run_id fossil_sweep_fd92337e88d7), zero writes,
    382 fossil contested entries found across the corpus. Spot-verified:
    ke_4dbf732e757d (the Phase 0 flagship example) is present with exactly
    the described shape — 6 archived counterparts + 1 self-referential link
    to itself; a random second sample (ke_40b0a59c8e00, 3 archived
    counterparts) checked out consistent. Every counterpart across all 382
    candidates resolved to either "archived" (1223) or "self_referential"
    (39) — zero "missing" statuses, consistent with the repo''s
    archive-never-delete convention. Report:
    scripts/reports/contested_fossil_sweep_2026-07-11T043334+0000.json
    (gitignored, not durable — Arjun has been sent the file directly).
    Apply mode (reverting these 382 to active with receipts) is a separate,
    still-ungranted approval — stage 5 territory, not this graduation.'
  - 'RESOLVED (apply) 2026-07-11T05:53:14Z with Arjun''s go-ahead ("continue,
    don''t stop until every step is completed"): ran
    `sweep_contested_fossils.py --apply --i-reviewed-the-dry-run`
    (run_id fossil_sweep_cb98be1f3dc1) against production. All 382 candidates
    from the reviewed dry-run list reverted contested->active, each with a
    consolidation-note receipt naming the run id, basis, and counterpart
    statuses, and a bumped revision (reversible per-entry via that receipt +
    revision history). Verified the flagship example directly in the applied
    report: ke_4dbf732e757d prior_revision=1, new_revision=2, receipt names
    all 6 archived counterparts plus the self-referential link. Report:
    scripts/reports/contested_fossil_sweep_2026-07-11T055314+0000.json
    (gitignored). This clears the entire known April-heuristic fossil
    backlog in one pass — the contract''s "scale" bar (apply run clears the
    fossil contested set with receipts) is now met for this one-time sweep;
    ongoing contradiction detection going forward is a live-system property
    this contract''s detection/lifecycle code (not this one-time script)
    is responsible for.'
  lessons:
  - 'Build was interrupted mid-flight: an Opus subagent implementing this
    contract hit its session usage limit after finishing the schema change,
    precedence.py/precedence.ts twins, the fixture table, and 3 of 4
    extraction-wiring files (7 of 10 evidence-construction sites in
    ingestion/core/extractor.py were still unwired). Picked up and finished
    on Sonnet (high effort) rather than re-spawning: verified every completed
    piece against the locked design before trusting it (all correct), wired
    the remaining sites, wrote all 4 test files, and designed the fossil
    sweep script from scratch (not attempted by the failed agent).'
  - 'CRITICAL correctness finding made before writing the sweep script: the
    Python KnowledgeEntry/KnowledgeMetadata dataclasses do not declare
    several Worker-managed fields (revision, injection_quarantine,
    quarantined_at, quarantine_streak_nights, github_repo, ...).
    Round-tripping an entry through KnowledgeEntry.from_dict().to_dict()
    would SILENTLY DROP every field the Python model does not know about —
    a real corruption risk on live entries. scripts/sweep_contested_fossils.py
    therefore operates on raw parsed JSON dicts throughout and never imports
    the typed dataclass for read or write. Worth carrying forward: any future
    Python script that reads-modifies-writes a live entry must do the same
    raw-dict check before trusting KnowledgeEntry as a round-trip-safe
    representation.'
  - 'Adversarial review (codex, scoped diff) caught two real bugs on this
    contract that unit tests alone had not: (1) derive_asserted_by/
    deriveAssertedBy invented "assistant" authority for any role that was
    merely not-user (so an unrecognized role like "system" or "tool" would
    have been silently promoted to assistant-level authority) instead of
    the contract-mandated "never invent, fall back to inferred" default —
    fixed to check role !== "assistant" rather than role === undefined, with
    regression tests in both languages. (2) FossilSweep.apply()''s receipt
    named counterpart ids but not the literal basis text, failing INV4''s
    explicit "naming the run id, basis, and counterpart ids" wording even
    though the counterpart summary implied the same information — fixed to
    include basis= explicitly. Both fixes shipped with regression tests
    before the review passed. Three review rounds total; PASS on the third.'
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
