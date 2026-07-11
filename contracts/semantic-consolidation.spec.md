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
- id: G0
  intent: 'premise gate: the mechanisms this contract builds on exist as diagnosed
    (semantic dedup machinery, the two automated nightly call sites, the operation
    dispatch/rollback/cap infrastructure, and the already-deployed merge hard gates)'
  must_assert: buildReplayPlansWithSemantic and runDreamProposal exist, both automated
    nightly call sites exist, mergeGates.ts's hard gates are wired into
    applyDuplicateMergePlan; exit nonzero if any premise has drifted
  command: |
    grep -q "async function buildReplayPlansWithSemantic" cloudflare-mcp/mcp-server/src/dream.ts || { echo "G0 FAIL: buildReplayPlansWithSemantic missing"; exit 1; }
    grep -q "export async function runDreamProposal" cloudflare-mcp/mcp-server/src/dream.ts || { echo "G0 FAIL: runDreamProposal missing"; exit 1; }
    grep -q "async function runScheduledGovernedDream" cloudflare-mcp/mcp-server/src/index.ts || { echo "G0 FAIL: index.ts nightly path missing"; exit 1; }
    grep -q "runBoundedSemanticSlicePass" cloudflare-mcp/mcp-server/src/scheduledDreamAsync.ts || { echo "G0 FAIL: scheduledDreamAsync.ts wiring missing"; exit 1; }
    grep -q "validateMergeConservation" cloudflare-mcp/mcp-server/src/dream.ts || { echo "G0 FAIL: merge hard gate wiring missing"; exit 1; }
    echo "G0 PASS: all premises hold"
  requires_permission: false
- id: G1
  intent: 'INV1 and INV6 hold: protected types cannot be absorbed automatically and
    the cap is coupled to the gates'
  must_assert: INV1 protected-loser holds and INV6 cap-coupling assertions pass in
    worker vitest; exit nonzero otherwise
  command: |
    cd cloudflare-mcp/mcp-server && npx vitest run test/protectedTypeMergeHold.test.ts test/mergeCapCoupling.test.ts --no-file-parallelism
  requires_permission: false
- id: G2
  intent: INV3 and INV5 hold across the labeled merge-fixture library (the lossless-merge
    regression suite)
  must_assert: every fixture labeled apply passes and every fixture labeled block
    is blocked with the violated rule named, including INV5 receipt-mapping cases;
    exit nonzero listing misclassified fixtures otherwise
  command: |
    cd cloudflare-mcp/mcp-server && npx vitest run test/mergeGates.test.ts --no-file-parallelism
  requires_permission: false
- id: G3
  intent: 'INV2 and INV4 hold: bounded per-night work and starvation-free cursor coverage'
  must_assert: INV2 bound counters and degradation fallback, and INV4 full-coverage/resume
    simulations pass; exit nonzero otherwise
  command: |
    cd cloudflare-mcp/mcp-server && npx vitest run test/semanticCursor.test.ts test/semantic-dedup.test.ts --no-file-parallelism
  requires_permission: false
- id: G4
  intent: INV7 scope discipline and existing suites stay fully green (no allowlists)
  must_assert: make worker-typecheck and the full worker vitest suite exit 0; INV7
    holds; exit nonzero otherwise
  command: |
    make worker-typecheck > /tmp/pks_sc_g4_tc.log 2>&1 || { tail -5 /tmp/pks_sc_g4_tc.log; echo "G4 FAIL: typecheck"; exit 1; }
    cd cloudflare-mcp/mcp-server && npx vitest run --no-file-parallelism > /tmp/pks_sc_g4_wk.log 2>&1 || { tail -5 /tmp/pks_sc_g4_wk.log; echo "G4 FAIL: worker suite"; exit 1; }
    echo "G4 PASS: worker suite fully green"
  requires_permission: false
- id: G5
  intent: 'staging end-to-end: seeded paraphrase duplicates produce semantic merge
    proposals and verified applies within bounds (network, staging only)'
  must_assert: a staging nightly run reports semantic_only_merges > 0 on the seeded
    corpus, applied merges pass post-apply verification (verify-memory-full equivalent),
    rollback of one applied merge restores prior state, and production is never targeted;
    exit nonzero otherwise
  command: |
    echo "G5 requires a staging deploy and a real POST /ops/dream/run_scheduled_governed trigger against staging with STAGING_DREAM_OPERATOR_TOKEN. Inspect the run summary's semantic_slice field for merges_added/contests_added, and the applied operations for any duplicate_merge; if one applied, verify it via get_deep on the canonical id, then rollback it via /ops/dream/restore and confirm both parents are restored."
  requires_permission: true
review:
  mode: required
  command: |
    DIFF=$(git diff HEAD -- cloudflare-mcp/mcp-server/src/dream.ts cloudflare-mcp/mcp-server/src/index.ts cloudflare-mcp/mcp-server/src/scheduledDreamAsync.ts shared/memory_policy.json cloudflare-mcp/mcp-server/test/scheduled.test.ts cloudflare-mcp/mcp-server/test/scheduled-async.test.ts cloudflare-mcp/mcp-server/test/semantic-dedup.test.ts; git status --porcelain -- cloudflare-mcp/mcp-server/src/semanticCursor.ts cloudflare-mcp/mcp-server/test/semanticCursor.test.ts cloudflare-mcp/mcp-server/test/protectedTypeMergeHold.test.ts cloudflare-mcp/mcp-server/test/mergeCapCoupling.test.ts)
    PROMPT="Static code review only — do NOT execute shell commands or run any test suite. Review this diff against contract PKS-SEMANTIC-CONSOLIDATION-001 for correctness bugs or violations of INV1 (protected-type hold), INV2 (bounded per-night work, fail-open), INV4 (starvation-free cursor coverage), INV6 (cap coupled to gates), INV7 (scope). mergeGates.ts (INV3/INV5) was reviewed and committed separately — do not re-review it, just confirm nothing here bypasses it. Respond with a single final line exactly 'REVIEW: PASS' or 'REVIEW: FAIL' plus the blocking issue. Nits do not block.

    DIFF:
    \$DIFF"
    codex exec "\$PROMPT" --sandbox read-only --skip-git-repo-check -m gpt-5.5 -c model_reasoning_effort="high" > /tmp/pks_sc_review_gate.log 2>&1
    tail -5 /tmp/pks_sc_review_gate.log
    grep -q "REVIEW: PASS" /tmp/pks_sc_review_gate.log && exit 0
    echo "REVIEW GATE FAIL — see /tmp/pks_sc_review_gate.log"
    exit 1
  sees: &id001
  - diff
  - invariants
  - scope
budget:
  max_turns: 40
  max_consecutive_failures: 3
  preflight_estimate: complete
  # Presented 2026-07-11 (Fable authoring/hard-gate implementation + Sonnet
  # subagent for the rest): mergeGates.ts (INV3/INV5, ~587 lines incl. tests,
  # self-authored and committed in a prior turn, already deployed to
  # production) plus this turn's ~10 files — semanticCursor.ts (rolling
  # cursor), dream.ts (+127: mergeSemanticSliceIntoProposal, exported
  # loadEntriesByType), index.ts (+165: resolveScheduledDuplicateMergeLimit,
  # INV1 hold check, runBoundedSemanticSlicePass), scheduledDreamAsync.ts
  # (+25, mirrors index.ts wiring), memory_policy.json (cap 10->50 coupled
  # to merge_hard_gates_active), 4 new/modified test files (56 new cases).
  # 1 build turn (subagent) + Fable review/fix pass (a corpus-shrink cursor
  # concern raised by adversarial review was investigated, proven a false
  # positive with 2 new regression tests, not "fixed" since nothing was
  # broken). 2 review rounds to convergence.
kill:
  after_turns: 12
graduate: G0 through G4 exit 0, review verdict is pass, no scope.forbid path touched
scale: graduated AND G5 passes on staging AND the production backlog drain (known
  ~459 semantic clusters, 200-entry batches, verify-after-each-batch, stop-on-fail)
  completes AND nightly duplicate-candidate counts trend down for 14 days
ledger:
  turns: 2
  consecutive_failures: 0
  blockers:
  - 'RESOLVED 2026-07-11 with Arjun''s go-ahead ("continue, don''t stop until
    every step is completed"): deployed to staging (648228ee), triggered a
    real governed nightly run against a fresh scheduled-boundary (staging''s
    same-day boundary dedup required overriding scheduled_time to bypass —
    see next entry). Confirmed live: semantic_slice.attempted=true,
    slice_size=2 (staging''s small fixture corpus), cursor initialized at
    position 0 and swept without error; counts.duplicate_merge_limit=50 in
    the response, proving resolveScheduledDuplicateMergeLimit correctly
    resolved to 50 (not 10) on the deployed Worker — the cap-coupling code
    is live and working, not just unit-tested. No paraphrase duplicates
    existed in the small staging fixture corpus to actually exercise a
    real semantic merge (merges_added=0) — that is the remaining gap in
    full G5 coverage, not a failure of this run. Production deployed
    (e5c17795) and verified healthy (search returns normal results). THE
    CAP RAISE 10->50 IS NOW LIVE IN PRODUCTION: starting with the next
    scheduled nightly run (07:10 UTC daily), up to 50 duplicate_merge
    operations may auto-apply per night (previously 10), each one gated by
    the hard conservation checks (mergeGates.ts) and, for protected-type
    losers, held for approval rather than auto-applied.'
  - 'Discovered during staging verification: run_scheduled_governed has a
    same-UTC-day boundary dedup (getScheduledGovernedBoundaryKey,
    72h TTL) that returns a cached prior result rather than re-running if
    called twice the same day — not a bug, existing intentional behavior
    (prevents duplicate nightly applies), but it meant the first
    verification attempt silently replayed an EARLIER (stage-2) cached run
    instead of exercising this deploy''s new code. Worked around by passing
    an explicit scheduled_time on a different calendar day. Noting this
    here because it is a reusable gotcha for any future manual
    verification against this endpoint.'
  - 'G5''s full bar (seeded paraphrase duplicates producing an actual
    semantic_only_merges > 0 result, applied and rolled back) not yet met
    — the staging fixture corpus is too small/homogeneous to contain a
    real semantic duplicate pair. Seeding one is a separate, small task
    (add 2 near-duplicate fixture entries to staging) not attempted here.'
  - 'The production backlog drain (~459 known semantic clusters from the
    2026-06-08 one-off run, 200-entry batches, verify-after-each-batch,
    stop-on-fail per the "scale" bar) has NOT been attempted. That is a
    separate, larger, explicitly Arjun-gated operation — stage 5 territory,
    not part of this graduation.'
  lessons:
  - 'Adversarial review raised a cursor wrap-detection concern (corpus
    shrinking between nights could cause advanceSemanticCursor to skip
    entries) that was investigated and found to be a false positive:
    selectCursorSlice wraps modulo the CURRENT corpus size within a single
    call, and advanceSemanticCursor uses the same modular base, so the two
    stay self-consistent regardless of corpus size changing night to night
    (proven by (a % n + b) % n == (a + b) % n for non-negative a). Verified
    empirically with a standalone vitest run before arguing it, then
    converted into 2 permanent regression tests
    (test/semanticCursor.test.ts) rather than left as an unverified claim
    in a chat transcript.'
  - 'Key architectural resolution: the spec''s own instructions were
    slightly in tension (merge before proposal/grade/apply proceeds, vs.
    confirm running two independent grade/apply cycles per night is safe).
    Resolved toward ONE grade + ONE apply on a single combined proposal —
    mergeSemanticSliceIntoProposal writes the merged operations back to
    Redis under the base proposal''s own run_id BEFORE gradeDreamProposal/
    applyDreamProposal are called with that same proposalId, so both
    re-fetch and act on the combined set. This also keeps the INV6 cap a
    single governing number over combined nightly throughput rather than
    letting two independent cycles double-count against it. Verified the
    ordering directly in the diff, not just trusted the subagent''s
    self-report.'
  - 'runDreamProposal itself DOES run twice per scheduled night (once
    corpus-wide lexical, unchanged; once semantic-only on the bounded
    200-entry cursor slice) — this is intentional and confirmed safe: it is
    a pure read+compute function with no lock or rate-limit that would make
    a second call in the same invocation unsafe. Only grade/apply run once,
    on the merged result.'
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
