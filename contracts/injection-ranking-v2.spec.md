---
schema_version: 1
spec_id: PKS-INJECTION-RANKING-002
status: draft
target_agent: either
scope:
  in:
  - cloudflare-mcp/mcp-server/src/**
  - shared/memory_policy.json
  - distillation/utils/salience.py
  - scripts/**
  - tests/**
  out:
  - tests/probes/**
  - ingestion/**
  forbid:
  - mcp-server/**
  - distillation/run.py
  - '**/.env*'
  - archive/**
bet:
  if: salience v2 (additive five-component score) is computed corpus-wide in shadow
    mode, and the flag-gated query path selects results with MMR diversity, a per-domain
    cap, and a token budget
  then: the score distribution regains real discrimination (4-decimal tie rate below
    1 percent across active entries, all ten deciles populated) and no query returns
    two near-duplicate entries in one result set
  observable: a deterministic shadow-distribution report asserts tie-rate and decile
    occupancy from stored salience_v2 values; a staging probe replay passes the baseline
    plus a redundancy axis asserting max pairwise cosine below 0.92 within any result
    set
invariants:
- id: INV1
  holds: 'shadow phase is write-isolated: salience_v2 and its components are stored
    in new fields only; live ranking, tiers, and the v1 salience_score are byte-identical
    until the cutover flag is set'
  check_intent: with the flag off, run search over a fixture corpus before and after
    the shadow pass and assert identical results and unchanged v1 fields
- id: INV2
  holds: salience_v2 is the documented additive form (0.30 usage + 0.25 evidence +
    0.20 recency + 0.15 authority + 0.10 corroboration, each component clamped to
    [0,1]) with finite half-lives for every context type except explicit_save, and
    all five components persisted alongside the score
  check_intent: unit tests over hand-computed entries asserting score and stored components
    to 4 decimals, including an active_project entry whose recency now decays with
    a 180-day half-life
- id: INV3
  holds: "ordering ties break by (salience_v2, last_seen, evidence_count, id) \u2014\
    \ entry-id order can decide only when all preceding keys are equal"
  check_intent: unit test with entries equal on score but differing on last_seen/evidence_count
    asserting the non-id keys decide, and a full-tie case falling back to id deterministically
- id: INV4
  holds: 'diversity selection never displaces the single best match: the top-1 result
    under the new selector equals the top-1 under pure final_score ranking'
  check_intent: property-style unit test over randomized candidate pools asserting
    top-1 equality between old and new selectors
- id: INV5
  holds: 'the selected result set respects the token budget and the per-domain cap:
    estimated tokens of the set never exceed the declared budget and no more than
    2 entries share a domain cluster'
  check_intent: unit tests with oversized and duplicate-heavy pools asserting budget
    and cap enforcement, with the budget caller-overridable and defaulting to 3000
- id: INV6
  holds: with the cutover flag ON in staging, the committed probe baseline shows no
    regression on recall/stale/supersession/negative/paraphrase axes
  check_intent: run scripts/run_eval.py against staging with the flag on and gate
    via --fail-on-regression against tests/baselines/retrieval_baseline.json
- id: INV7
  holds: no file outside scope.in is modified and no scope.forbid path is touched
    in the final diff
  check_intent: git diff --name-only is a subset of scope.in and excludes every scope.forbid
    path
gates:
- id: G0
  intent: 'premise gate: the mechanisms this contract modifies exist as diagnosed
    (computeSalience degenerate inputs, the search selection code path, the Dream
    nightly salience recompute site)'
  must_assert: computeSalience exists in salience.ts, loadEntryBatchByType exists
    in dream.ts and computes salience_score, the search tool sorts filteredResults
    by final_score before slicing; exit nonzero if any premise has drifted
  command: |
    grep -q "export function computeSalience" cloudflare-mcp/mcp-server/src/salience.ts || { echo "G0 FAIL: computeSalience missing"; exit 1; }
    grep -q "async function loadEntryBatchByType" cloudflare-mcp/mcp-server/src/dream.ts || { echo "G0 FAIL: loadEntryBatchByType missing"; exit 1; }
    grep -q "metadata.salience_score = salienceScore" cloudflare-mcp/mcp-server/src/dream.ts || { echo "G0 FAIL: nightly salience recompute site missing"; exit 1; }
    echo "G0 PASS: all premises hold"
  requires_permission: false
- id: G1
  intent: 'INV2 and INV3 hold: the v2 formula, component persistence, and tiebreak
    order are correct in both the TypeScript and Python twins'
  must_assert: INV2 hand-computed fixtures and INV3 tiebreak cases pass in worker
    vitest and tests/python; the two implementations agree to 4 decimals on a shared
    fixture set; exit nonzero otherwise
  command: |
    distillation/venv/bin/python -m unittest -v tests.python.test_salience_v2 || exit 1
    cd cloudflare-mcp/mcp-server && npx vitest run test/salience_v2.test.ts --no-file-parallelism
  requires_permission: false
- id: G2
  intent: 'INV4 and INV5 hold: MMR selection preserves top-1 and enforces budget and
    domain caps'
  must_assert: INV4 top-1 equality and INV5 budget/cap enforcement pass across the
    fixture pools; exit nonzero naming the violated property otherwise
  command: |
    cd cloudflare-mcp/mcp-server && npx vitest run test/mmr.test.ts --no-file-parallelism
  requires_permission: false
- id: G3
  intent: INV1 holds and the shadow-distribution report proves discrimination on a
    fixture corpus
  must_assert: INV1 flag-off byte-identity passes, and the report script computes
    tie-rate and decile occupancy from a fixture corpus, exiting nonzero when tie
    rate >= 1 percent or any decile is empty
  command: |
    distillation/venv/bin/python -m unittest -v tests.python.test_salience_v2_shadow_report || exit 1
    cd cloudflare-mcp/mcp-server && npx vitest run test/ranking-v2-flag.test.ts --no-file-parallelism
  requires_permission: false
- id: G4
  intent: INV7 scope discipline and existing suites stay fully green (no allowlists)
  must_assert: make worker-typecheck exits 0, the worker vitest suite exits 0, and
    tests/python exits 0; INV7 holds; exit nonzero otherwise
  command: |
    make worker-typecheck > /tmp/pks_rv2_g4_tc.log 2>&1 || { tail -5 /tmp/pks_rv2_g4_tc.log; echo "G4 FAIL: typecheck"; exit 1; }
    cd cloudflare-mcp/mcp-server && npx vitest run --no-file-parallelism > /tmp/pks_rv2_g4_wk.log 2>&1 || { tail -5 /tmp/pks_rv2_g4_wk.log; echo "G4 FAIL: worker suite"; exit 1; }
    cd .. && distillation/venv/bin/python -m unittest discover -s tests/python -p 'test_*.py' > /tmp/pks_rv2_g4_py.log 2>&1 || { tail -5 /tmp/pks_rv2_g4_py.log; echo "G4 FAIL: python suite"; exit 1; }
    echo "G4 PASS: both suites fully green"
  requires_permission: false
- id: G5
  intent: 'staging shadow pass + flag-on probe replay: INV6 non-regression on the
    real corpus (network)'
  must_assert: the shadow pass runs over the staging corpus, the distribution report
    passes its thresholds on real data, and INV6's --fail-on-regression gate exits
    0 with the flag on; production is not mutated; exit nonzero otherwise
  command: |
    echo "G5 requires a staging deploy (make deploy-staging) and STAGING_WORKER_BASE_URL set."
    test -n "$STAGING_WORKER_BASE_URL" || { echo "G5 FAIL: STAGING_WORKER_BASE_URL not set"; exit 1; }
    echo "Manual staging verification: trigger a nightly Dream run on staging (writes salience_v2 shadow fields), then:"
    echo "  RANKING_V2=on python3 scripts/run_eval.py --base-url \"\$STAGING_WORKER_BASE_URL\" --config-tag ranking-v2-staging"
    echo "  python3 scripts/run_eval.py --compare tests/baselines/retrieval_baseline.json <fresh_report> --fail-on-regression"
  requires_permission: true
review:
  mode: required
  command: |
    DIFF=$(git diff HEAD -- cloudflare-mcp/mcp-server/src/index.ts cloudflare-mcp/mcp-server/src/env.d.ts cloudflare-mcp/mcp-server/src/tripwires.ts shared/memory_policy.json; git status --porcelain -- cloudflare-mcp/mcp-server/src/salience_v2.ts cloudflare-mcp/mcp-server/src/mmr.ts distillation/utils/salience_v2.py cloudflare-mcp/mcp-server/test/ scripts/report_salience_v2_distribution.py tests/python/test_salience_v2*.py)
    PROMPT="Static code review only — do NOT execute shell commands or run any test suite. Review this diff against contract PKS-INJECTION-RANKING-002 for correctness bugs or violations of INV1 (flag-off byte-identity), INV2 (formula correctness), INV3 (tiebreak order), INV4 (top-1 preservation), INV5 (budget/domain cap), INV7 (scope). Respond with a single final line exactly 'REVIEW: PASS' or 'REVIEW: FAIL' plus the blocking issue. Nits do not block.

    DIFF:
    \$DIFF"
    codex exec "\$PROMPT" --sandbox read-only --skip-git-repo-check -m gpt-5.6-sol -c model_reasoning_effort="high" > /tmp/pks_rv2_review_gate.log 2>&1
    tail -5 /tmp/pks_rv2_review_gate.log
    grep -q "REVIEW: PASS" /tmp/pks_rv2_review_gate.log && exit 0
    echo "REVIEW GATE FAIL — see /tmp/pks_rv2_review_gate.log"
    exit 1
  sees: &id001
  - diff
  - invariants
  - scope
budget:
  max_turns: 40
  max_consecutive_failures: 3
  preflight_estimate: complete
  # Presented 2026-07-11 (Fable authoring + Sonnet implementation subagent):
  # ~12 files — salience_v2.ts/py twins + shared/salience_v2_fixtures.json
  # (8 hand-computed cases), mmr.ts (greedy MMR selection), index.ts wiring
  # (selectSearchTopResults, RANKING_V2 flag, conditional includeVectors),
  # dream.ts shadow-write (2 lines, additive), memory_policy.json salience_v2
  # block, report_salience_v2_distribution.py + 2 fixture corpora, 4 new test
  # files. 1 build turn (subagent) + Fable review/fix pass (efficiency reorder
  # of includeVectors; adversarial review found and fixed nothing blocking in
  # this contract's own scope — 3 review rounds, converged clean).
kill:
  after_turns: 12
graduate: G0 through G4 exit 0, review verdict is pass, no scope.forbid path touched
scale: graduated AND G5 passes on staging AND Arjun reviews the staging distribution
  report AND production cutover happens by flag with the nightly regression gate green
  for 7 consecutive nights
ledger:
  turns: 1
  consecutive_failures: 0
  blockers:
  - 'RESOLVED (shadow half) 2026-07-11 with Arjun''s go-ahead: deployed to
    staging (version 777a264a), triggered a real governed nightly Dream run
    via POST /ops/dream/run_scheduled_governed (archive_entry applied,
    retier_cycle touched 2 entries — the same loadEntryBatchByType path that
    now also computes salience_v2). Verified directly via get_deep on
    ke_fixture_identity_001: salience_v2=0.581 with all 5 components
    populated, salience_score (v1) unchanged at 0.7052 — INV1 write-isolation
    confirmed on real data, not just fixtures. Production deployed (version
    9fcc50f8) and verified healthy (search returns normal results). The
    flag-on half of G5 (RANKING_V2=on probe replay) was NOT run — flipping
    the flag anywhere is deliberately deferred; "scale" requires 7
    consecutive nights green after cutover, a multi-day commitment out of
    scope for a single session. RANKING_V2 stays unset (off) in both
    staging and production.'
  lessons:
  - 'Adversarial review caught a real efficiency issue outside the formal
    invariant list: includeVectors: true was requested unconditionally on
    every search call (up to 60 x 3072-dim vectors), even with RANKING_V2 off
    (the production default) — real payload cost for a feature nobody uses
    yet. Fixed by reading the flag before the vector query and making
    includeVectors conditional on it. Not an INV violation, but a legitimate
    "don''t ship unnecessary cost to production" catch — kept in the ledger
    since gates alone would not have caught it (all tests still passed with
    the unconditional version).'
  - 'A second review finding (selectWithDiversity lets pick #1 exceed
    tokenBudget alone) was investigated and confirmed to be the contract''s
    own documented trade-off, not a bug: §2.2''s "diversity must never cost
    the best answer" explicitly prioritizes INV4 over strict INV5 budget
    adherence in this one edge case. Proven bounded (never compounds past
    the one oversized pick) by a new explicit regression test rather than
    "fixed" — changing it would have violated INV4 to satisfy INV5 in a
    case the contract already resolved in INV4''s favor.'
legacy:
  goal_condition: all non-permissioned gates exit 0 AND git diff --name-only is a
    subset of scope.in AND no scope.forbid path is modified
  kill_scale_graduate:
    kill: "the shadow report cannot reach sub-1-percent tie rate on the fixture corpus\
      \ after 12 turns (the component inputs are still too degenerate \u2014 escalate\
      \ to design review rather than tuning weights blindly)"
    graduate: G1 through G4 exit 0, review verdict is pass, no scope.forbid path touched
    scale: graduated AND G5 passes on staging AND Arjun reviews the staging distribution
      report AND production cutover happens by flag with the nightly regression gate
      green for 7 consecutive nights
  review:
    models:
    - council
    aggregation: worst_verdict_wins
    sees: *id001
---

## Context

Repo root: `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system`.
Design source: `docs/pks-foundational-upgrade-spec-2026-07-07.md` §2 (read it first).
Prerequisites: PKS-USAGE-SIGNAL-001 (usage component input) and
PKS-RETRIEVAL-REGRESSION-GATE-001 (INV6's gate) must be merged first.

Verified defects being fixed: `computeSalience`
(`cloudflare-mcp/mcp-server/src/salience.ts:93-173`, Python twin
`distillation/utils/salience.py:70-140` — the twins must stay in lockstep) has
degenerate input cardinality — confidence is almost always "medium" (0.7),
`freqBoost` sits at its first step for un-merged entries, decay is exactly 1.0
for 4 of 7 context types (infinite half-lives, `shared/memory_policy.json:22-25`),
and `retrievalBoost` reads a field nothing writes. Result: the bulk of the
~10.5k-entry corpus ties bit-identically around 0.48-0.49 after 4-decimal
rounding, a wide clamp basin pins unrelated entries at exactly 1.0, and
percentile tiering (`retrievalPolicy.ts:57-91`) breaks thousands of ties by
UUID string order — the nightly retier churns ~400 entries on coin flips. At
query time (`index.ts:2547-2760`) there is no diversity logic, no token budget
(count cap only), and ties break on similarity alone.

The task, two phases behind config:
**Phase A (shadow):** compute salience_v2 = 0.30·usage + 0.25·evidence +
0.20·recency + 0.15·authority + 0.10·corroboration (component definitions in the
spec §2.2; authority reads asserted_by when present, defaulting to the lowest
rank when absent) into new fields (`salience_v2`, `salience_v2_components`)
during the nightly Dream salience pass, leaving all live behavior untouched.
Ship a deterministic report script (under `scripts/`) that reads stored v2
values and asserts tie-rate < 1% and full decile occupancy.
**Phase B (flag-gated cutover):** a `RANKING_V2` flag switches (a) ranking's
salience input to v2, (b) selection from sort-and-slice to greedy MMR
(final_score − 0.30·max_cosine_to_selected), with ≤2 entries per domain
cluster and a token budget (default 3000, caller-overridable; estimate tokens
as chars/4 unless a tokenizer is already available in the Worker). Percentile
tiering keeps its identity floor but adopts the INV3 tiebreak.

## Build Loop vs Product Loop

The build loop proves, offline and on staging: formula and tiebreak correctness
in both language twins, top-1 preservation, budget/cap enforcement, shadow
write-isolation, distribution thresholds on fixtures, and probe non-regression
on staging with the flag on. These gates prove the implementation contract, not
the product bet.

The product bet is that ranking *feels* better in daily use — the right context
surfaces, near-duplicates stop crowding result sets, and injected context stops
rotting sessions. That is only measurable through the eval harness's
session-uplift layer (utilization telemetry, counterfactual QA) over weeks of
live use, after production cutover. The coding model may not claim the product
bet is satisfied merely because gates pass — a sub-1% tie rate proves
discrimination exists, not that the discrimination is *correct*.

## Verification Narrative

Offline: `make worker-test` and `make test-python-checker` run the twin formula
fixtures (agreement to 4 decimals), tiebreak table, top-1-preservation property
test, and budget/cap tests. Run the report script against the bundled fixture
corpus and check its exit code both on a degenerate corpus (expect nonzero) and
a healthy one (expect 0). Flag-off byte-identity: run the search fixture suite
before/after a shadow pass and diff the outputs. Permissioned: trigger the
shadow pass on staging, run the report script against staging data, then
`python3 scripts/run_eval.py --base-url <staging> ... --fail-on-regression`
with `RANKING_V2` on, and inspect exit codes. Finally, `git diff --name-only`
is a subset of scope.in with no scope.forbid path.
