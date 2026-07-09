---
schema_version: 1
spec_id: PKS-SEMANTIC-CONSOLIDATION-001
status: draft
target_agent: either
scope:
  in:
  - cloudflare-mcp/mcp-server/src/**
  - shared/memory_policy.json
  - tests/**
  - scripts/**
  out:
  - ingestion/**
  - tests/probes/**
  forbid:
  - mcp-server/**
  - distillation/run.py
  - '**/.env*'
  - archive/**
bet:
  if: the nightly Dream run executes with the semantic slice enabled
  then: a bounded rolling-cursor slice of the corpus (at most 200 candidates, at most
    400 vector queries per night) receives semantic duplicate detection, so different-title
    duplicates become merge proposals; and every duplicate merge must pass the hard
    lossless gates (evidence conservation, metadata monotonicity, reversibility, protected-type
    exclusion) before apply
  observable: on a staging corpus seeded with paraphrase duplicates the nightly path
    reports semantic_only_merges greater than zero within its bounds, the merge-fixture
    library passes and fails each gate exactly as labeled, and the persistent cursor
    provably advances and wraps the full corpus
invariants:
- id: INV1
  holds: 'protected context types (explicit_save, stated_preference, professional_identity)
    are never merge losers on the automated path: any proposal absorbing one is held
    for judge or human approval, never auto-applied'
  check_intent: unit test building a merge cluster whose lexically-inferior member
    is explicit_save; assert the proposal is generated with a hold/requires-approval
    marker and the scheduled governed decision does not select it
- id: INV2
  holds: 'per-night bounds are hard: at most 200 semantic candidates and 400 vector
    neighbor queries per run, fail-open to lexical-only on vector-store degradation,
    within Worker subrequest limits'
  check_intent: unit test with a 1000-entry fixture asserting query and candidate
    counters stop at the bounds, and a degraded-store stub falls back to lexical with
    a DEDUP_DEGRADED log
- id: INV3
  holds: 'hard merge gates block on violation: the merged entry''s evidence multiset
    equals the union of parents'' evidence, mention_count is the sum, first_seen the
    min, last_seen the max, source_conversations the union, and losers are archived
    (never deleted) with a merge receipt naming winner, operation id, and pre-merge
    revisions'
  check_intent: fixture library of labeled merge cases (clean, subset, lossy-evidence,
    wrong-metadata) asserting apply proceeds only for the clean/subset cases and the
    gate names the violated conservation rule for the rest
- id: INV4
  holds: 'the rolling cursor persists across runs, advances monotonically, wraps,
    and cannot starve any corpus region: every active entry id is visited within ceil(corpus_size/200)
    consecutive nights'
  check_intent: unit test simulating consecutive runs over a fixture corpus asserting
    full coverage within the computed number of runs and correct resume after an interrupted
    run
- id: INV5
  holds: an insight dropped during merge deduplication is recorded in the merge receipt
    with the id of the retained insight that entails it; no insight disappears without
    a receipt mapping
  check_intent: unit test merging entries with near-identical insights asserting the
    receipt contains a dropped-to-retained mapping covering every removed insight,
    and that a merge attempting an unmapped drop is blocked
- id: INV6
  holds: the scheduled duplicate_merge cap remains 10 until this contract's gates
    are green and is raised to 50 only in the same change that activates the hard
    gates
  check_intent: config assertion test tying the cap value to the merge-gate feature
    flag such that cap>10 with gates off fails the suite
- id: INV7
  holds: no file outside scope.in is modified and no scope.forbid path is touched
    in the final diff
  check_intent: git diff --name-only is a subset of scope.in and excludes every scope.forbid
    path
gates:
- id: G1
  intent: 'INV1 and INV6 hold: protected types cannot be absorbed automatically and
    the cap is coupled to the gates'
  must_assert: INV1 protected-loser holds and INV6 cap-coupling assertions pass in
    worker vitest; exit nonzero otherwise
  command: TODO
  requires_permission: false
- id: G2
  intent: INV3 and INV5 hold across the labeled merge-fixture library (the lossless-merge
    regression suite)
  must_assert: every fixture labeled apply passes and every fixture labeled block
    is blocked with the violated rule named, including INV5 receipt-mapping cases;
    exit nonzero listing misclassified fixtures otherwise
  command: TODO
  requires_permission: false
- id: G3
  intent: 'INV2 and INV4 hold: bounded per-night work and starvation-free cursor coverage'
  must_assert: INV2 bound counters and degradation fallback, and INV4 full-coverage/resume
    simulations pass; exit nonzero otherwise
  command: TODO
  requires_permission: false
- id: G4
  intent: INV7 scope discipline and existing suites stay green
  must_assert: make worker-typecheck and make worker-test exit 0 and INV7 holds; exit
    nonzero otherwise
  command: TODO
  requires_permission: false
- id: G5
  intent: 'staging end-to-end: seeded paraphrase duplicates produce semantic merge
    proposals and verified applies within bounds (network, staging only)'
  must_assert: a staging nightly run reports semantic_only_merges > 0 on the seeded
    corpus, applied merges pass post-apply verification (verify-memory-full equivalent),
    rollback of one applied merge restores prior state, and production is never targeted;
    exit nonzero otherwise
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
  max_turns: 40
  max_consecutive_failures: 3
  preflight_estimate: required
kill:
  after_turns: 12
graduate: G1 through G4 exit 0, review verdict is pass, no scope.forbid path touched
scale: graduated AND G5 passes on staging AND the production backlog drain (known
  ~459 semantic clusters, 200-entry batches, verify-after-each-batch, stop-on-fail)
  completes AND nightly duplicate-candidate counts trend down for 14 days
ledger:
  turns: 0
  consecutive_failures: 0
  blockers: []
  lessons: []
legacy:
  goal_condition: all non-permissioned gates exit 0 AND git diff --name-only is a
    subset of scope.in AND no scope.forbid path is modified
  kill_scale_graduate:
    kill: "INV3 conservation gates cannot be made to block a lossy fixture after 12\
      \ turns (the gate cannot detect loss, so raising throughput is unsafe) \u2014\
      \ stop and escalate"
    graduate: G1 through G4 exit 0, review verdict is pass, no scope.forbid path touched
    scale: graduated AND G5 passes on staging AND the production backlog drain (known
      ~459 semantic clusters, 200-entry batches, verify-after-each-batch, stop-on-fail)
      completes AND nightly duplicate-candidate counts trend down for 14 days
  review:
    models:
    - council
    aggregation: worst_verdict_wins
    sees: *id001
---

## Context

Repo root: `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system`.
Design source: `docs/pks-foundational-upgrade-spec-2026-07-07.md` §3.3-§3.4 (read it first).
Prerequisites: PKS-RETRIEVAL-REGRESSION-GATE-001 (Phase 9 probe bracket) and
ideally PKS-ADMISSION-DEDUP-001 (shrinks the inflow this pipeline must beat).

Verified defects being fixed (2026-07-07): the nightly Dream proposal path is
lexical-only — `runDreamProposal` enables semantic dedup only when candidate
ids are supplied or `semantic: true` is passed (`dream.ts:3053`, design comment
`:3050-3052`), and neither automated caller does (`index.ts:1311-1317`,
`scheduledDreamAsync.ts:397-403`). Lexical means exact-normalized-title
fingerprints (`getDuplicateFingerprint`, `dream.ts:592-598`) — different-title
duplicates are invisible. Every recent run logs `semantic_only_merges: 0`.
Semantic dedup ran against the corpus once, manually, on 2026-06-08: 459
clusters over 1,338 entries found, 35 merges applied, aborted on a
verify-memory-full failure, never resumed. Separately, merge-loser selection
(`compareCanonicalPriority`, `dream.ts:623-641`) ignores context type — an
explicit_save entry can be absorbed automatically today. And current merges are
lossless-by-concatenation: absorbed insights pile up as near-identical
paraphrases (e.g. `ke_4dbf732e757d` carries 9).

The task: (1) add a nightly bounded semantic slice to proposal generation,
reusing the existing operator-path machinery (`buildReplayPlansWithSemantic`,
`dream.ts:971`; policy caps `COSINE_DUP_THRESHOLD 0.95`,
`SEMANTIC_MAX_CLUSTER_SIZE 6`, `SEMANTIC_DEDUP_MAX_QUERIES 400` in
`shared/memory_policy.json`) driven by a persistent rolling cursor stored in
Redis; (2) implement the hard merge gates (INV3, INV5) in the apply path;
(3) close the protected-type hole (INV1); (4) couple the cap raise 10→50 to the
gates (INV6). The judged gates (claim coverage, no-new-claims, contradiction
routing) from spec §3.4 Ring 2 are a follow-on contract once the Mac judge
pipeline (`DREAM_OPUS_MODE`) is operational — do not block this contract on
them, but design the gate interface so judged verdicts slot in.

## Build Loop vs Product Loop

The build loop proves, offline and on staging: protected-type exclusion, hard
conservation gates against a labeled fixture library, receipt-mapped insight
deduplication, bounded per-night work, starvation-free cursor coverage,
cap-gate coupling, suite/scope discipline, and a seeded staging run producing
and safely applying semantic merges with a demonstrated rollback. These gates
prove the implementation contract, not the product bet.

The product bet is that consolidation keeps up with ingestion without losing
information: the duplicate backlog (849 duplicate entries / 228 multi-member
clusters at the 2026-06-08 audit; candidate counts 92→161 and rising) drains
and stays drained, retrieval stops surfacing duplicate pile-ups, and no user
ever discovers a silently lost fact. Loss prevention is only provable
negatively over time — via the nightly sampled merge audits and the Phase 9
probe brackets defined in the spec's eval harness. The coding model
may not claim the product bet is satisfied merely because gates pass.

## Verification Narrative

Offline: `make worker-test` runs the merge-fixture library — a clean duplicate
pair applies with a receipt; a fixture whose merged entry drops one Evidence
row is blocked naming evidence conservation; a fixture absorbing an
explicit_save loser is held, not applied; the cursor simulation covers a
1,000-entry fixture corpus in 5 simulated nights and resumes mid-slice after a
simulated crash; the cap-coupling test fails if the cap exceeds 10 while gates
are off. Permissioned: deploy to staging, seed the staging corpus with the
bundled paraphrase-duplicate fixtures, trigger the nightly path, and confirm
the run summary shows `semantic_only_merges > 0`, applied merges carry
receipts, `rollbackDreamApply` on one merge restores both parents, and the
staging probe replay passes. Finally, `git diff --name-only` is a subset of
scope.in with no scope.forbid path.
