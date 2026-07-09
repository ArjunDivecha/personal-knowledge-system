---
schema_version: 1
spec_id: PKS-USAGE-SIGNAL-001
status: in_progress
target_agent: either
scope:
  in:
  - cloudflare-mcp/mcp-server/src/index.ts
  - cloudflare-mcp/mcp-server/test/reconsolidation-usage-signal.test.ts
  - scripts/run_eval.py
  - tests/python/test_run_eval_search_args.py
  out:
  - tests/probes/**
  - shared/memory_policy.json
  forbid:
  - mcp-server/**
  - distillation/run.py
  - '**/.env*'
  - archive/**
bet:
  if: the usage-reinforcement loop's semantics are codified in tests and synthetic
    benchmark traffic is excluded from it
  then: the already-live access signal (last_accessed/access_count via rank-1 search
    reconsolidation, get_context, get_deep) stays honest — organic single-entry
    retrievals reinforce, candidate-pool exposure and eval probes never do — and
    those semantics cannot regress silently
  observable: the worker vitest suite pins the rank-1 cap and the suppression flag;
    an eval run against a flag-honoring Worker leaves access counts unchanged while
    an organic search still increments its rank-1 hit
invariants:
- id: INV1
  holds: 'exposure is not use: at most the rank-1 search result earns an access-signal
    write (MAX_RECONSOLIDATION_SEARCH_RESULTS === 1, the 2026-06-09 commit 34c590b
    decision); the wider candidate pool never does'
  check_intent: vitest pins the constant at 1 and asserts selectReconsolidationTargets
    returns only the first element of a full result page
- id: INV2
  holds: 'synthetic traffic never reinforces: a search call carrying suppress_access_signals=true
    yields zero reconsolidation targets, and every probe issued by scripts/run_eval.py
    carries that flag'
  check_intent: vitest asserts selectReconsolidationTargets returns [] under suppression;
    python unittest asserts build_search_arguments sets suppress_access_signals=True
- id: INV3
  holds: access-signal writes are minimal and monotonic — applyAccessSignals touches
    only access_count, last_accessed, salience_score; count merges by max (never
    rolls back); last_accessed keeps the latest timestamp (never rewinds)
  check_intent: vitest asserts field-level diff is exactly those three keys and that
    lagging/stale side-key values cannot regress the stored values
- id: INV4
  holds: 'the signal is meaningful: computeSalience scores an entry accessed today
    strictly above an otherwise-identical never-accessed entry, with decay ordering
    fresh > stale > never (the retrievalBoost term, salience.ts:147)'
  check_intent: vitest constructs identical entries differing only in last_accessed
    and asserts strict ordering
- id: INV5
  holds: no file outside scope.in is modified and no scope.forbid path is touched
    in the final diff
  check_intent: git diff --name-only (this contract's files) is a subset of scope.in
    and excludes every scope.forbid path
gates:
- id: G0
  intent: 'premise gate: the mechanism this contract protects actually exists and
    is wired into all three read tools (guards against building on a stale diagnosis
    — the failure mode that killed this contract''s v1)'
  must_assert: scheduleReconsolidation is invoked from the search, get_context, and
    get_deep tool handlers in src/index.ts; exit nonzero if any wiring disappears
  command: |
    cd cloudflare-mcp/mcp-server
    CALLS=$(grep -c "this.scheduleReconsolidation(" src/index.ts)
    if [ "$CALLS" -lt 3 ]; then
      echo "G0 FAIL: expected >=3 scheduleReconsolidation call sites (search rank-1, get_context, get_deep); found $CALLS — the premise this contract protects has changed, re-author before building"
      exit 1
    fi
    echo "G0 PASS: usage loop wired at $CALLS call sites"
  requires_permission: false
- id: G1
  intent: 'INV1, INV2 (worker side), INV3, INV4 hold: the new vitest file is green'
  must_assert: all cases in test/reconsolidation-usage-signal.test.ts pass — cap
    pinned at 1, suppression yields zero targets, applyAccessSignals minimal+monotonic,
    computeSalience ordering strict; exit nonzero naming the failing case otherwise
  command: |
    cd cloudflare-mcp/mcp-server && npx vitest run test/reconsolidation-usage-signal.test.ts --no-file-parallelism
  requires_permission: false
- id: G2
  intent: 'INV2 (eval side) holds: every run_eval.py probe is marked synthetic'
  must_assert: build_search_arguments returns suppress_access_signals=True for every
    query and leaks no other arguments; exit nonzero otherwise
  command: |
    distillation/venv/bin/python -m unittest -v tests.python.test_run_eval_search_args
  requires_permission: false
- id: G3
  intent: existing Worker suite and typecheck stay green (no new failures) and INV5
    scope discipline holds
  must_assert: make worker-typecheck exits 0; the worker vitest suite shows no failures
    beyond the two pre-existing ones documented in ledger.blockers (oauth-mcp create-entry,
    dream-replay rollback — both fail at committed HEAD with this contract's diff
    stashed); exit nonzero on any new failure
  command: |
    make worker-typecheck || exit 1
    cd cloudflare-mcp/mcp-server
    npx vitest run --no-file-parallelism > /tmp/pks_g3_worker_suite.log 2>&1
    tail -5 /tmp/pks_g3_worker_suite.log
    NEW_FAILS=$(grep -E "^\s+× " /tmp/pks_g3_worker_suite.log | grep -vE "creates a knowledge entry through the write-scoped MCP tool|rolls back supported applied proposal operations with revision preflight" || true)
    if [ -n "$NEW_FAILS" ]; then
      echo "G3 FAIL: new worker-test failures beyond the two documented pre-existing ones:"
      echo "$NEW_FAILS"
      exit 1
    fi
    echo "G3 PASS: no new failures (2 pre-existing documented in ledger.blockers, unchanged)"
  requires_permission: false
- id: G4
  intent: 'end-to-end on staging: suppression honored and organic reinforcement still
    live (network, staging deploy required)'
  must_assert: against a staging Worker running this code, (a) a search with suppress_access_signals=true
    leaves the rank-1 hit's access_count unchanged, (b) the same search without the
    flag increments it and refreshes last_accessed, (c) both responses are well-formed;
    production is not deployed to or targeted; exit nonzero on mismatch
  command: |
    echo "G4 requires a staging deploy (make deploy-staging) and STAGING_WORKER_BASE_URL set."
    test -n "$STAGING_WORKER_BASE_URL" || { echo "G4 FAIL: STAGING_WORKER_BASE_URL not set"; exit 1; }
    distillation/venv/bin/python - <<'PY'
    import json, os, sys
    sys.path.insert(0, "scripts")
    from check_overnight_dream_run import call_mcp_tool, fetch_dream_session

    base = os.environ["STAGING_WORKER_BASE_URL"]
    session, token, sid = fetch_dream_session(base)

    def top_hit(args, rpc):
        payload = call_mcp_tool(session, base, token, sid, rpc_id=rpc, name="search", arguments=args)
        results = payload.get("results", [])
        if not results:
            print("G4 FAIL: staging search returned no results"); sys.exit(1)
        return results[0]

    q = "PKS architecture"
    before = top_hit({"query": q, "suppress_access_signals": True}, 11)
    import time; time.sleep(2)
    after_suppressed = top_hit({"query": q, "suppress_access_signals": True}, 12)
    if (after_suppressed.get("access_count") or 0) > (before.get("access_count") or 0):
        print(f"G4 FAIL: suppressed search still incremented access_count "
              f"({before.get('access_count')} -> {after_suppressed.get('access_count')})"); sys.exit(1)

    organic = top_hit({"query": q}, 13)
    time.sleep(2)
    after_organic = top_hit({"query": q, "suppress_access_signals": True}, 14)
    if (after_organic.get("access_count") or 0) <= (after_suppressed.get("access_count") or 0):
        print(f"G4 FAIL: organic search did not increment access_count "
              f"({after_suppressed.get('access_count')} -> {after_organic.get('access_count')})"); sys.exit(1)
    print("G4 PASS: suppression honored, organic reinforcement live "
          f"(suppressed steady at {after_suppressed.get('access_count')}, organic bumped to {after_organic.get('access_count')})")
    PY
  requires_permission: true
review:
  mode: required
  command: |
    DIFF=$(git diff HEAD -- cloudflare-mcp/mcp-server/src/index.ts scripts/run_eval.py; git diff --no-index /dev/null cloudflare-mcp/mcp-server/test/reconsolidation-usage-signal.test.ts 2>/dev/null; git diff --no-index /dev/null tests/python/test_run_eval_search_args.py 2>/dev/null; true)
    PROMPT="Static code review only — do NOT execute shell commands or run any test suite (the sandbox TMPDIR is broken and produces spurious unrelated errors; review by reading only). Review this diff against contract PKS-USAGE-SIGNAL-001 (scope: cloudflare-mcp/mcp-server/src/index.ts, cloudflare-mcp/mcp-server/test/reconsolidation-usage-signal.test.ts, scripts/run_eval.py, tests/python/test_run_eval_search_args.py) for correctness bugs or violations of these invariants:
    INV1: at most the rank-1 search result earns an access-signal write; the candidate pool never does (MAX_RECONSOLIDATION_SEARCH_RESULTS pinned at 1).
    INV2: a search carrying suppress_access_signals=true schedules zero reconsolidation writes; every run_eval.py probe carries the flag.
    INV3: applyAccessSignals touches only access_count/last_accessed/salience_score, merges count by max, keeps latest last_accessed.
    INV4: computeSalience orders fresh-accessed > stale-accessed > never-accessed for otherwise-identical entries.
    INV5: no file outside scope.in touched.
    Context you must respect: this contract deliberately does NOT widen reinforcement beyond rank-1 (the 2026-06-09 exposure!=use decision, commit 34c590b) — flagging rank-1-only as 'too narrow' is not a blocking issue.
    Respond with a single final line exactly 'REVIEW: PASS' if there are no blocking correctness issues, or 'REVIEW: FAIL' plus the specific blocking issue otherwise. Nits do not block.

    DIFF:
    $DIFF"
    codex exec "$PROMPT" --sandbox read-only --skip-git-repo-check -m gpt-5.5 -c model_reasoning_effort="high" > /tmp/pks_usage_review.log 2>&1
    tail -5 /tmp/pks_usage_review.log
    grep -q "REVIEW: PASS" /tmp/pks_usage_review.log && exit 0
    echo "REVIEW GATE FAIL — see /tmp/pks_usage_review.log"
    exit 1
  sees:
  - diff
  - invariants
  - scope
budget:
  max_turns: 12
  max_consecutive_failures: 3
  preflight_estimate: complete
  # Presented 2026-07-09 (v2 rewrite, Fable): 4 files — src/index.ts (+~25 lines:
  # two exports, selectReconsolidationTargets, schema field, handler wiring),
  # new test/reconsolidation-usage-signal.test.ts (~145 lines), run_eval.py
  # (+~20: build_search_arguments + notes), new tests/python/test_run_eval_search_args.py
  # (~60). 1 build turn; gates G0-G3 verified by hand before being written here.
kill:
  after_turns: 6
graduate: G0 through G3 exit 0, review verdict is pass, no scope.forbid path touched
scale: graduated AND G4 confirms both suppression and organic reinforcement on staging
  AND after 7 nights of production traffic the access-write rate from organic sessions
  is nonzero while nightly eval runs produce zero access writes
ledger:
  turns: 1
  consecutive_failures: 0
  blockers:
  - '2 worker vitest failures pre-exist at committed HEAD (verified by stashing this
    contract''s diff and re-running): oauth-mcp.test.ts "creates a knowledge entry
    through the write-scoped MCP tool" and dream-replay.test.ts "rolls back supported
    applied proposal operations with revision preflight". Unrelated to this scope
    (create_entry write path, dream rollback preflight). G3 allowlists exactly these
    two by name; flagged to Arjun, not silently fixed or hidden. Same repo-hygiene
    pattern as the 2 pre-existing python failures noted in the stage-0 contract.'
  - 'G4 (staging e2e) not yet run: requires a staging deploy of this code and
    STAGING_WORKER_BASE_URL. Production deploy is a separate, explicitly-approved
    step — code on main is NOT live until wrangler deploy runs.'
  lessons:
  - 'v1 of this contract (authored 2026-07-07) was built on a false premise: it
    claimed search never writes last_accessed and the retrievalBoost term was dead
    code. In fact scheduleReconsolidation/reconsolidateEntry has been live since
    2026-03-27, wired into search (rank-1 only since 2026-06-09, commit 34c590b
    "exposure != use"), get_context, and get_deep — verified empirically 2026-07-09
    with two live production searches (access_count 0->1, salience 0.4786->0.6286,
    exactly the retrievalBoost delta). The corpus-wide access_count:0 observation
    that misled the diagnosis is explained by rank-1-only writes + low organic
    search volume, not a missing mechanism. v2 (this file) protects the existing
    semantics and closes the one real gap (eval-traffic pollution) instead of
    rebuilding what exists.'
  - 'Structural takeaway now encoded as G0: every future contract in this program
    gets a premise gate — a cheap mechanical check that the defect/mechanism the
    contract assumes actually exists in the repo as described. The build-ladder
    escalates on gate failure only; it cannot catch a wrong diagnosis whose gates
    would pass green while building the wrong thing. A premise gate converts that
    blind spot into a deterministic failure.'
legacy:
  goal_condition: all non-permissioned gates exit 0 AND git diff --name-only is a
    subset of scope.in AND no scope.forbid path is modified
  kill_scale_graduate:
    kill: G0 fails and cannot be reconciled after 6 turns (the premise changed under
      the contract) — hand back to the author model, never build on a stale diagnosis
    graduate: G0 through G3 exit 0, review verdict is pass, no scope.forbid path touched
    scale: graduated AND G4 confirms staging behavior AND 7 nights of production
      traffic show organic access writes with zero eval-run writes
  review:
    models:
    - council
    aggregation: worst_verdict_wins
    sees:
    - diff
    - invariants
    - scope
---

## Context

Repo root: `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system`.
Production MCP Worker: `cloudflare-mcp/mcp-server/` (root `mcp-server/` is legacy,
forbidden here).

**This is v2 of the contract, rewritten 2026-07-09 after the v1 premise was
falsified against the live system.** v1 (and §1.1.1/§2.2/§7.1 of the 2026-07-07
upgrade spec) claimed the retrieval path never writes `last_accessed`/`access_count`
and that salience's `retrievalBoost` term (`salience.ts:147`) was dead code. That
is wrong. The usage loop exists and is live:

- `scheduleReconsolidation` → `reconsolidateEntry` (`src/index.ts:1610-1727`),
  shipped 2026-03-27: async via `ctx.waitUntil`, fail-open (errors logged to a
  Redis list, never surfaced to the caller), writes durable side keys
  (`entry_access:<id>`, `entry_last_accessed:<id>`), merges them into the entry
  via `applyAccessSignals` (max-count, latest-timestamp), recomputes salience,
  PATCHes vector metadata, and — critically — aborts rather than un-archiving an
  entry Dream archived while the write was queued (the 3.3 fix).
- Wired into all three read tools: `search` (rank-1 result only), `get_context`
  (its single returned entry), `get_deep` (the requested entry).
- Every read path hydrates the side keys back onto entries (`loadEntry` →
  `hydrateEntryAccessSignals`), so ingestion re-writes of entry JSON cannot
  permanently erase the signal.
- The rank-1 cap is a **deliberate 2026-06-09 decision** (commit 34c590b,
  "exposure != use"): top-5 triggers were granting every search result permanent
  archive immunity regardless of relevance. That principle is preserved here —
  this contract must NOT widen reinforcement.

What was actually missing, and what this contract ships:

1. **Zero test coverage** of any of those semantics — the cap, the fail-open
   write, the minimal-field merge, the salience ordering. One careless refactor
   could silently revert the 06-09 decision. Now codified in
   `test/reconsolidation-usage-signal.test.ts`.
2. **Eval traffic pollutes the signal.** `scripts/run_eval.py` (52 enabled
   probes, about to run nightly under the stage-0 regression gate) increments
   the rank-1 hit of every probe query on every run — synthetic reinforcement
   that salience would treat as organic use, systematically boosting exactly the
   entries the probe suite queries. Fixed with an additive, optional
   `suppress_access_signals` flag on the `search` tool schema; the eval runner
   now sets it on every probe. Old Workers strip the unknown argument harmlessly
   (zod non-strict), so the runner can deploy ahead of the Worker.

## Build Loop vs Product Loop

The build loop is offline and machine-checkable: G0 pins the premise (the
mechanism exists and is wired), G1/G2 pin the semantics in vitest/unittest,
G3 proves no collateral damage. Passing them proves the implementation
contract — the semantics are codified and the flag plumbing is correct.

The product loop is the real bet: that an honest usage signal (organic-only
reinforcement) gives salience-v2 (PKS-INJECTION-RANKING-002) a discriminative
usage component worth 0.30 of the score. That is only observable after the
Worker is deployed and weeks of organic traffic accumulate, and after checking
that rank-1-only volume is sufficient signal density across a 10.5k-entry
corpus — if it proves too sparse, the right lever is widening what counts as
*genuine* use (e.g. get_context/get_deep already count; session-level
utilization telemetry per spec §5.3), never blanket candidate-pool
reinforcement. The build model may not claim the product bet from gate success.

## Verification Narrative

A fresh agent verifies as follows. Offline: run the G0 grep (3+ wiring sites),
then `cd cloudflare-mcp/mcp-server && npx vitest run test/reconsolidation-usage-signal.test.ts`
(9 cases: cap pin, rank-1 selection, empty-page, suppression×2, minimal-fields,
max-merge, latest-timestamp, salience ordering×2), then
`distillation/venv/bin/python -m unittest tests.python.test_run_eval_search_args`
(flag pinned on every probe), then `make worker-typecheck` and the full worker
suite expecting no failures beyond the two pre-existing ones named in
ledger.blockers. Permissioned (G4): deploy to staging, run the embedded python
snippet — a suppressed search twice (count steady), an organic search (count
bumps), assert ordering. Finally confirm the diff touches only scope.in files.
