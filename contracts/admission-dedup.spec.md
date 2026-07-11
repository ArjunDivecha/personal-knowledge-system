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
- id: G0
  intent: 'premise gate: the mechanisms this contract builds on exist as diagnosed
    (embedding client, vector query filter support, the two entry-save call sites)'
  must_assert: StorageClient exposes generate_embedding and a filter-capable vector
    query, and the two ingestion runners save knowledge entries via StorageClient;
    exit nonzero if any premise has drifted
  command: |
    grep -q "def generate_embedding" ingestion/core/storage.py || { echo "G0 FAIL: generate_embedding missing"; exit 1; }
    grep -q "def query_top_neighbor" ingestion/core/storage.py || { echo "G0 FAIL: query_top_neighbor missing"; exit 1; }
    grep -q "save_knowledge_entry_with_dedup" ingestion/agent_sessions/run.py || { echo "G0 FAIL: agent_sessions wiring missing"; exit 1; }
    grep -q "save_knowledge_entry_with_dedup" ingestion/github/run.py || { echo "G0 FAIL: github wiring missing"; exit 1; }
    echo "G0 PASS: all premises hold"
  requires_permission: false
- id: G1
  intent: 'INV1 and INV4 hold: appends conserve evidence and non-duplicates are untouched
    by the feature'
  must_assert: INV1 field-level evidence equality and INV4 golden-file byte-identity
    pass in tests/python; exit nonzero naming the divergence otherwise
  command: |
    distillation/venv/bin/python -m unittest -v tests.python.test_admission_router.AppendEvidenceConservationTests tests.python.test_admission_router.SaveKnowledgeEntryWithDedupTests.test_disabled_policy_calls_save_knowledge_entry_with_identical_args
  requires_permission: false
- id: G2
  intent: 'INV2 and INV5 hold: routing respects entry state and policy-driven thresholds'
  must_assert: INV2 archived/contested-neighbor cases route to new-entry and INV5
    policy-override cases pass; exit nonzero otherwise
  command: |
    distillation/venv/bin/python -m unittest -v tests.python.test_admission_router.QueryTopNeighborFilterTests tests.python.test_admission_router.PolicyOverrideNotHardcodedTests
  requires_permission: false
- id: G3
  intent: 'INV3 holds: dry-run is write-free with a complete decision log, and the
    live flag defaults off'
  must_assert: 'INV3 passes: storage-spy write count is zero in dry-run, every fixture
    candidate appears in the decision log with a decision and neighbor score, and
    the default config value is off; exit nonzero otherwise'
  command: |
    distillation/venv/bin/python -m unittest -v tests.python.test_admission_router.SaveKnowledgeEntryWithDedupTests.test_dry_run_true_performs_zero_writes tests.python.test_admission_router.CheckedInPolicyDefaultsTests tests.python.test_memory_policy_admission_dedup
  requires_permission: false
- id: G4
  intent: INV6 scope discipline and the existing python suite stays fully green (no
    allowlist)
  must_assert: tests/python suite exits 0 and INV6 holds (git diff subset of scope.in,
    no forbid path); exit nonzero otherwise
  command: |
    distillation/venv/bin/python -m unittest discover -s tests/python -p 'test_*.py' > /tmp/pks_ad_g4.log 2>&1
    RC=$?
    tail -5 /tmp/pks_ad_g4.log
    if [ $RC -ne 0 ]; then echo "G4 FAIL: tests/python suite not green"; exit 1; fi
    echo "G4 PASS: tests/python suite fully green"
  requires_permission: false
- id: G5
  intent: 'live shadow run: one real ingestion cycle in dry-run against production
    data, decision log reviewed (network, read-only writes-wise)'
  must_assert: a real ingestion run with admission_dedup in dry-run emits a decision
    log whose append-rate and neighbor scores are plausible (spot-checked against
    known duplicate clusters like the curate-my-world categoryMapping entries); zero
    entry writes attributable to the router; exit nonzero on any router-attributed
    write
  command: |
    echo "G5 requires flipping admission_dedup.enabled=true, dry_run=true in shared/memory_policy.json (a real, reviewed policy edit — not done automatically), then one real ingestion cycle, e.g.:"
    echo "  cd ingestion && python github/run.py --repo ArjunDivecha/curate-my-world"
    echo "Inspect scripts/reports/admission_dedup_decisions_<stamp>.json for the categoryMapping duplicate cluster and confirm append-rate is plausible."
  requires_permission: true
review:
  mode: required
  command: |
    DIFF=$(git diff HEAD -- ingestion/core/storage.py ingestion/agent_sessions/run.py ingestion/github/run.py shared/memory_policy.json; git status --porcelain -- ingestion/core/admission_router.py tests/python/test_admission_router.py tests/python/test_memory_policy_admission_dedup.py)
    PROMPT="Static code review only — do NOT execute shell commands or run any test suite. Review this diff against contract PKS-ADMISSION-DEDUP-001 for correctness bugs or violations of INV1-INV6 (evidence conservation, state-aware routing, write-free dry-run, byte-identical disabled path, policy-driven thresholds, scope). Respond with a single final line exactly 'REVIEW: PASS' or 'REVIEW: FAIL' plus the blocking issue. Nits do not block.

    DIFF:
    \$DIFF"
    codex exec "\$PROMPT" --sandbox read-only --skip-git-repo-check -m gpt-5.5 -c model_reasoning_effort="high" > /tmp/pks_ad_review_gate.log 2>&1
    tail -5 /tmp/pks_ad_review_gate.log
    grep -q "REVIEW: PASS" /tmp/pks_ad_review_gate.log && exit 0
    echo "REVIEW GATE FAIL — see /tmp/pks_ad_review_gate.log"
    exit 1
  sees: &id001
  - diff
  - invariants
  - scope
budget:
  max_turns: 30
  max_consecutive_failures: 3
  preflight_estimate: complete
  # Presented 2026-07-11 (Fable authoring + Sonnet implementation subagent):
  # ~5 files — admission_router.py (AdmissionRouter: route/apply_decision/
  # write_report), storage.py (+query_top_neighbor, +save_knowledge_entry_with_dedup),
  # 2 runner call-site edits, memory_policy.json admission_dedup block
  # (enabled:false, dry_run:true), 2 new test files (32+ cases). 1 build turn
  # + Fable review/fix pass (self-match exclusion in query_top_neighbor;
  # embedding_text threading to avoid a double OpenAI call).
kill:
  after_turns: 10
graduate: G0 through G4 exit 0, review verdict is pass, no scope.forbid path touched
scale: graduated AND G5 decision log reviewed by Arjun AND live mode enabled AND over
  the next 7 days the nightly duplicate-merge candidate count trends down instead
  of up
ledger:
  turns: 1
  consecutive_failures: 0
  blockers:
  - 'G5 (live shadow ingestion run) not yet run: requires flipping
    admission_dedup.enabled=true (dry_run stays true) in shared/memory_policy.json
    and running a real ingestion cycle. Deferred to the deploy step.'
  lessons:
  - 'Adversarial review caught a real correctness bug: query_top_neighbor had
    no self-exclusion, so a candidate already present in the vector index
    (e.g. a retried ingestion run re-processing a candidate whose id was
    minted and saved on a prior partial attempt) could self-match at top-1
    (cosine ~1.0) and be routed as an append-to-itself. Fixed:
    query_top_neighbor now accepts exclude_id, widens top_k to 2 when set,
    filters the self-match, and falls through to a genuine second neighbor.
    AdmissionRouter.route() threads candidate_entry["id"] through. 4 new
    regression tests, including one proving the fallback to a real second
    neighbor works, not just "returns None".'
  - 'A second review finding (apply_decision re-derives embedding_text from
    scratch for the "new"/"link" paths, wasting the embedding already
    computed once in route() as a second OpenAI call) was a real efficiency
    issue, not a correctness one — fixed by threading the already-computed
    embedding_text through apply_decision rather than re-deriving it.'
  - 'A third review round (round 2 of this contract''s own dialogue) flagged
    that tests patch admission_router.load_admission_dedup_policy while
    storage.py imports it via a function-local import, and speculated this
    could mean patches don''t take effect in tests. Verified false: Python
    resolves "from X import Y" against X''s current namespace at the time
    the import statement executes, and a function-local import re-executes
    every call — confirmed empirically by a test that asserts patched values
    only reachable if the patch took effect, which passes. Documented here
    so a future reviewer doesn''t re-raise the same false lead.'
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
