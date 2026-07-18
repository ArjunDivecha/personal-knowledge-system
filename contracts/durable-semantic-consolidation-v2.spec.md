---
schema_version: 1
spec_id: PKS-DURABLE-SEMANTIC-CONSOLIDATION-002
status: under_review
target_agent: either
scope:
  in:
  - cloudflare-mcp/mcp-server/src/**
  - cloudflare-mcp/mcp-server/test/**
  - cloudflare-mcp/mcp-server/wrangler.json
  - cloudflare-mcp/mcp-server/package.json
  - cloudflare-mcp/mcp-server/package-lock.json
  - cloudflare-mcp/mcp-server/worker-configuration.d.ts
  - shared/memory_policy.json
  - ingestion/core/**
  - scripts/**
  - tests/**
  - .github/workflows/**
  - Makefile
  - contracts/durable-semantic-consolidation-v2.spec.md
  out:
  - legacy Vercel MCP implementation
  - destructive export refresh pipeline
  - archived entry payloads
  - unrelated ingestion extractors
  - retrieval-ranking policy changes not required for consistency fencing
  forbid:
  - mcp-server/**
  - distillation/run.py
  - archive/**
  - '**/.env*'
  - '**/*secret*'
  - '**/*credential*'
bet:
  if: semantic, lexical, retier, and derived-index maintenance are converted from
    one corpus-scale Worker cron into externally planned and/or indexed candidate
    generation plus small idempotent Cloudflare Queue jobs
  then: the existing semantic backlog and future duplicate inflow drain through the
    Worker's current policy, CAS, conservation, protection, and rollback authority
    without any invocation loading the corpus or leaving Redis, Vector, and the thin
    index silently divergent after a crash or retry
  observable: a production-shaped 20k-key cost test has fixed per-message bounds;
    crash injection at every merge/vector/index boundary converges automatically;
    staging proves enqueue-to-verified-apply and rollback; and production cohorts
    reduce the uncapped tight-cluster backlog while strict consistency including embedding
    freshness remains at zero issues
invariants:
- id: INV1
  holds: 'External Python is candidate generation only: it may submit entry-id clusters
    and snapshot evidence, but only the Worker may decide that entries are duplicates,
    select the canonical winner, construct a merge, grade it, or authorize apply;
    the Worker re-reads current Redis revisions and current vectors and runs one versioned
    in-repo duplicate algorithm and current policy before every apply.'
  check_intent: Submit false, stale, incomplete, oversized, cross-type, threshold-edge,
    and valid clusters; assert only a current complete component accepted by the Worker's
    own algorithm becomes a proposal and no client-supplied winner or merged payload
    is honored.
- id: INV2
  holds: No automatic path can make explicit_save, professional_identity, or stated_preference
    a merge loser; this guard is enforced at the common duplicate-merge authority/apply
    boundary, not only in scheduled decision code. Exactly one protected member may
    become canonical; multiple protected members are held unless a separate human
    approval is bound to the exact operation digest, actor, reason, and expiry.
  check_intent: Exercise queue, scheduled, manual operator, judge-verdict, retry,
    and legacy internal call sites and prove INV2 blocks or holds every unapproved
    protected loser, including forged or stale client evidence.
- id: INV3
  holds: 'Redis is authoritative and each merge''s Redis transition is all-or-nothing:
    current revisions are CAS-checked and the canonical entry, archived losers, archive
    snapshots/pointers, access/index bookkeeping, mutation id, write-ahead journal,
    and derived-store outbox intent are committed in one atomic Redis operation before
    any job can acknowledge success.'
  check_intent: Fault-inject before, during, and after every Redis transition; assert
    INV3 yields either the complete pre-state or complete committed Redis state, never
    a partially merged set, and a duplicate delivery cannot apply twice.
- id: INV4
  holds: 'Vector and the thin index are explicitly derived state with automatic convergence:
    a committed outbox first re-embeds and upserts the canonical entry, verifies its
    id/model/dimensions/Redis revision/embedding-input SHA-256, then deletes loser
    vectors and applies bounded thin-index patches; retries, recovery scans, and a
    DLQ make every unfinished intent visible and resumable.'
  check_intent: Crash and throttle every Vector and thin-index step; assert INV4 preserves
    a retrievable canonical vector, retries idempotently, never marks the merge verified
    early, and converges without a manual repair script.
- id: INV5
  holds: 'Semantic freshness is machine-verifiable: every active vector carries embedding_model,
    embedding_dimensions, redis_revision, and embedding_input_sha256 derived from
    the exact normalized text embedded for that Redis revision; strict verification
    treats a missing or mismatched value as an issue and merged canonical content
    is always re-embedded.'
  check_intent: Prove the current metadata-only update bug fails INV5, then mutate
    canonical content without re-embedding and assert strict verification reports
    stale_embedding_input even when vector presence and ordinary metadata match.
- id: INV6
  holds: 'Every queue message has a fixed cost independent of corpus size: no SCAN,
    full-corpus load, full thin-index rebuild, or unbounded fan-out is reachable;
    a semantic message contains one cluster of 2..SEMANTIC_MAX_CLUSTER_SIZE entries
    and has explicit caps for Redis keys/bytes, Vector fetch/query/upsert/delete calls,
    OpenAI embeddings, wall time, retries, and concurrent Upstash requests.'
  check_intent: Run the same message against tiny and 20k-key fake stores with cost
    counters and assert INV6's call/key/byte bounds are identical; force 429 and resource-budget
    exhaustion and assert delayed retry or DLQ rather than continued fan-out.
- id: INV7
  holds: 'Queue delivery is at-least-once but effects are idempotent: stable plan/cohort/cluster/mutation
    ids, atomic claim/fencing, terminal result caching, bounded exponential backoff,
    max retries, dead-letter routing, and replay tooling prevent duplicate or lost
    merges.'
  check_intent: Deliver messages concurrently, out of order, repeatedly, after timeout,
    and after worker restart; assert INV7 produces one committed merge result or one
    explicit terminal hold/failure with no hidden work.
- id: INV8
  holds: 'The backlog mechanism is durable and repeatable, not one-shot: an uncapped
    full planner bulk-fetches active ids/vectors and performs blockwise offline cosine
    clustering, while a dirty-entry feed handles daily changes; capped/incomplete
    audits are rejected, plan manifests carry corpus/policy/algorithm watermarks,
    and stale candidates are harmlessly revalidated or held.'
  check_intent: Compare blockwise output with brute force on fixtures, reject query_capped
    manifests, resume from checkpoints, mutate/re-embed entries between plan and apply,
    and prove INV8 rediscovers new clusters on later runs without double-applying
    old ones.
- id: INV9
  holds: The Cloudflare cron becomes trigger-only after cutover. Semantic discovery
    is external; lexical candidates come from bounded dirty fingerprint buckets; percentile
    retier walks a maintained Redis salience index in bounded cursor jobs; judge consumption,
    archive/promotion, vector outbox, recovery, and thin-index maintenance are isolated
    jobs. No live phase calls the old full-corpus runDreamProposal, runScheduledRetierCycle,
    runBoundedSemanticSlicePass, or rebuildThinIndexSafely path.
  check_intent: Trace a scheduled tick in live mode and assert INV9 only enqueues
    bounded phase messages; each phase can fail independently without starving the
    others, and grep/call-spy guards prove the monolithic functions are unreachable
    after cutover.
- id: INV10
  holds: Retrieval validates Vector hits against batched current Redis state before
    returning them, excluding archived, missing, or wrong-revision rows; a pending
    derived-state reconciliation may reduce ranking freshness briefly but cannot make
    an active canonical entry permanently invisible or expose an archived loser as
    active.
  check_intent: Seed stale loser vectors, missing canonical vectors, wrong revisions,
    and pending outbox records; assert INV10 filters false hits, repairs/holds missing-canonical
    cases, and returns only current active Redis entries.
- id: INV11
  holds: Every merge is individually reversible from its durable journal, including
    atomic Redis restore plus a new derived-state outbox; rollback is idempotent,
    revision-fenced, and cannot overwrite later legitimate edits.
  check_intent: Apply, retry, edit-after-apply, rollback, retry rollback, and crash
    during rollback; assert INV11 restores exact snapshots only when fences match
    and otherwise stops with an explicit conflict.
- id: INV12
  holds: A cohort cannot advance until targeted post-merge verification and full make
    verify-memory-full (expanded to embedding freshness and index/outbox/journal checks)
    both pass with zero issues; any failure, DLQ message, stale in-progress journal,
    Upstash 429 tripwire, or configured cost-budget breach pauses new mutation messages
    while read retrieval stays available.
  check_intent: Inject each failure class, assert INV12 freezes only maintenance,
    preserves retrieval, emits a durable actionable status, and requires a clean verification
    barrier before the next cohort.
- id: INV13
  holds: 'Platform limits are observed rather than guessed: run records capture configured/deployed
    Cloudflare subrequest limit, actual external subrequests, bytes loaded, duration/CPU
    outcome, queue retries/DLQ depth, Upstash 429s/backoff, OpenAI embedding count/cost
    proxy, and pending-derived-state age; the design remains below explicit policy
    budgets even if Cloudflare''s published defaults change.'
  check_intent: Assert every staging and production-shaped run record contains non-null
    INV13 measurements, that unmeasured fields are UNMEASURED rather than zero, and
    that policy-budget breaches stop mutation before provider hard limits.
- id: INV14
  holds: 'Cutover is fail-closed and reversible: SEMANTIC_SLICE_SIZE stays 0; queue
    mode progresses off -> shadow -> staging live -> one-cluster production canary
    -> bounded cohorts; disabling queue live mode freezes maintenance rather than
    falling back to the dangerous monolithic cron.'
  check_intent: Exercise every feature-flag transition and rollback; assert INV14
    never enables two mutation engines, never silently re-enables the old semantic
    slice, and leaves the live retrieval service healthy when maintenance is frozen.
gates:
- id: G0
  intent: 'Premise and regression gate: current evidence is captured and INV5''s hidden
    stale-embedding bug plus the scheduled-only INV2 guard are reproduced by tests
    that fail on the pre-fix implementation.'
  must_assert: Pin current counts/report evidence, prove applyDuplicateMergePlan changes
    Redis embedding input without changing the vector embedding, and prove an automatic
    non-scheduled apply can currently absorb a protected loser; exit nonzero if either
    defect is not reproduced before product code changes.
  command: python3 scripts/check_durable_semantic_build.py --gate G0
  requires_permission: false
- id: G1
  intent: INV1 and INV2 hold at one common Worker authority boundary for semantic
    and lexical duplicate candidates.
  must_assert: Candidate-only submission, current-policy revalidation, canonical selection,
    oversized/incomplete-cluster rejection, and protected-loser behavior pass across
    every call path; no external payload can supply the winning content or bypass
    a hold.
  command: python3 scripts/check_durable_semantic_build.py --gate G1
  requires_permission: false
- id: G2
  intent: INV3, INV7, and INV11 hold under atomicity, concurrency, retry, and rollback
    fault injection.
  must_assert: Every injected boundary produces pre-state or complete Redis commit,
    duplicate queue deliveries have one effect, journals/results are durable, and
    rollback is exact and revision-fenced.
  command: python3 scripts/check_durable_semantic_build.py --gate G2
  requires_permission: false
- id: G3
  intent: INV4, INV5, and INV10 hold for Vector, embedding freshness, retrieval fencing,
    thin-index patching, outbox recovery, and DLQ visibility.
  must_assert: Canonical re-embedding happens on content change; derived work is ordered
    and idempotent; stale/missing/hash-mismatched rows fail strict verification; retrieval
    never exposes an archived loser; all crash/throttle fixtures converge or surface
    terminally.
  command: python3 scripts/check_durable_semantic_build.py --gate G3
  requires_permission: false
- id: G4
  intent: INV6 and INV13 hold in production-shaped cost tests.
  must_assert: A 20k-key fake corpus causes no SCAN/full load/full rebuild, per-message
    key/byte/subrequest/embedding/concurrency counts remain within policy and independent
    of corpus size, and 429/resource exhaustion backs off or dead-letters with non-null
    telemetry.
  command: python3 scripts/check_durable_semantic_build.py --gate G4
  requires_permission: false
- id: G5
  intent: INV8 holds for recurring offline semantic planning and safe manifest/checkpoint
    behavior.
  must_assert: Blockwise offline clustering matches brute force, uncapped full and
    dirty incremental runs are resumable/repeatable, capped manifests fail closed,
    policy/corpus/algorithm watermarks are recorded, and stale candidates are safely
    revalidated.
  command: python3 scripts/check_durable_semantic_build.py --gate G5
  requires_permission: false
- id: G6
  intent: INV9 holds for the trigger-only cron and bounded lexical, retier, judge,
    archive/promotion, recovery, vector, and thin-index phase isolation.
  must_assert: Live-mode call traces cannot reach monolithic corpus loaders/rebuilders,
    phase failures are isolated, maintained secondary indexes reconcile to Redis truth,
    and cursor/index tests prove eventual coverage under corpus mutation.
  command: python3 scripts/check_durable_semantic_build.py --gate G6
  requires_permission: false
- id: G7
  intent: INV12 and INV14 hold and all repository regressions, types, config generation,
    strict consistency fixtures, and scope checks pass.
  must_assert: Cohort barriers freeze on every failure class, flags cannot double-run
    engines or fall back to monolith, Worker/Python/orchestrator suites and typecheck
    pass with no allowlists, generated Cloudflare binding types match queue config,
    and final diff is inside scope.
  command: python3 scripts/check_durable_semantic_build.py --gate G7
  requires_permission: false
- id: G8
  intent: Staging end-to-end proves INV1 through INV14 with real Cloudflare Queue
    delivery, real staging Redis/Vector/OpenAI, fault injection, recovery, verification
    barrier, and rollback.
  must_assert: A seeded lexical cluster and semantic paraphrase cluster are planned,
    accepted, queued, applied exactly once, re-embedded with matching hash/revision,
    made consistent, and rolled back; a protected-loser cluster is held; a forced
    retry and DLQ/replay are observable; no production resource is addressed.
  command: python3 scripts/run_durable_semantic_permission_gate.py staging
  requires_permission: true
- id: G9
  intent: Production canary and controlled backlog cohort prove the deployed mechanism
    without re-enabling the old slice.
  must_assert: Preflight strict verification is zero; one tight cluster applies and
    rolls back cleanly; then a reviewed cohort applies with zero strict issues and
    no stale journals/outbox/DLQ items, retrieval health remains green, measured budgets
    have headroom, and the uncapped backlog report decreases by exactly the live revalidated
    effects.
  command: python3 scripts/run_durable_semantic_permission_gate.py production
  requires_permission: true
review:
  mode: required
  command: python3 scripts/review_durable_semantic_consolidation.py
  sees:
  - diff
  - invariants
  - scope
budget:
  max_turns: 45
  max_consecutive_failures: 3
  preflight_estimate: complete
kill:
  after_turns: 12
  gate: G2
graduate: G0 through G7 and the required review pass from a clean checkout; G8 then
  passes on staging with explicit permission and zero unresolved high-severity findings.
scale: Graduated, then G9 passes; the 1,160-cluster baseline drains in verified cohorts
  with a durable checkpoint; two subsequent uncapped weekly audits and fourteen daily
  incremental runs show continued discovery with zero strict consistency issues, zero
  stale journals/outbox items beyond SLO, zero DLQ backlog, and no monolithic cron
  execution.
ledger:
  turns: 39
  consecutive_failures: 0
  blockers: []
  lessons:
  - Do not encode Cloudflare's former approximately 1,000-subrequest limit as current
    truth. Published paid-plan defaults changed in 2026; measure the deployed account
    and enforce a lower application budget independent of provider limits.
  - A persisted before-snapshot is a rollback handle, not crash consistency. Cross-store
    writes require an atomic Redis commit plus idempotent derived-state outbox and
    recovery.
  - Vector presence and metadata equality do not prove semantic freshness. Bind every
    vector to the exact embedding input hash, model, dimensions, and Redis revision.
  - A candidate generator may be duplicated across runtimes; duplicate authority may
    not. Revalidate candidates and construct the merge only in the Worker.
  - Queue messages must be bounded and terminal-result cached so retries cannot fan
    out into corpus-scale work.
  - Queue live mode must freeze maintenance rather than fall back to the legacy full-corpus
    cron.
  - Staging tooling uses STAGING_* variables; strict verifier runs must explicitly
    map them to the worker's UPSTASH_* names to avoid the repo-root instance.
  - The root .env contains multiple Redis and Vector instances; production verification
    must bind the exact first measured-raven URL/token pair used by the Worker and
    never trust dotenv precedence.
  - 'The live uncapped baseline must be rerun after each cohort: this production cohort
    changed M4 from 1324 clusters covering 5196 entries to 1322 covering 5192, while
    the quality gate remains red on the expected backlog-share threshold.'
  - 'A production canary is not evidence of a cohort: apply and rollback the canary,
    then apply a separate reviewed bounded cohort and verify strict consistency before
    recording G9 complete.'
  - 'The 2026-07-14 scheduled-equivalent full run completed in 93 seconds with one
    bounded mark_contested apply, retier changes on 117 entries, two verdicts applied,
    and five insight proposals enqueued; the live cron itself remains trigger-only.'
  - Automatic semantic maintenance must live in the external planner workflow; the
    sleep-report workflow must never repair a missing night by calling the legacy
    corpus-scale scheduled-governed endpoint.
  - 'Staging exposed a version-skew failure: the queue apply route existed but the
    rollback route fell through to an HTTP 200 HTML landing page. Deploying staging
    Worker e8b2455c-cf2b-4db7-9bfb-05acdc86fc12 restored the route, and the driver
    now rejects non-JSON operator responses explicitly.'
  - 'G8 automatic-driver proof passed on staging in run nsm-20260714T213116Z-31892992:
    one semantic pair applied, strict Redis/Vector consistency passed, the duplicate
    cluster count fell from one to zero, rollback restored both active entries, and
    strict consistency passed again. Evidence is in nightly_semantic_maintenance_20260714T213215+0000.json.'
  - 'The initial recurring production budget is five successful merges in one verified
    cohort with four audit query workers. Scale only from observed clean nightly records;
    do not start at the larger manual ceiling.'
  - 'The 2026-07-18 production canary on corrected score semantics applied one previously
    held pair with zero holds, zero rollbacks, a clean cohort barrier, and strict consistency
    at zero issues; the operator then authorized a recurring target of 100 successful merges,
    selected from up to 300 candidates, while retaining verification cohorts of five.'
  - 'G9 one-merge production canary passed in GitHub run 29370313379 and durable run
    nsm-20260714T214000Z-1320d5c1: seven candidates were held as incomplete, one
    applied, strict consistency stayed at zero issues, and the uncapped backlog fell
    from 1322 clusters/5192 entries to 1321/5190.'
  - Persist the verified cohort record before starting the slower read-only post-audit;
    otherwise a healthy run looks stuck in applying during that scan and an interrupted
    runner loses useful barrier telemetry.
---

## Context

Repository: `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system`. The production service is `cloudflare-mcp/mcp-server/`; root `mcp-server/` is legacy and forbidden. At authoring time, local `main`, `origin/main`, and `IBROKEIT.md` agree at commit `77f875643290b34e3e05d48b66e54cf8b4c7aa7b`.

The incident report is directionally correct: adding a second targeted semantic proposal inside the already corpus-scale scheduled Worker caused four failed nightly runs, followed by partial Redis/Vector divergence. The current kill switch is correct and must remain: `shared/memory_policy.json` has `dedup.SEMANTIC_SLICE_SIZE = 0`, checked before its corpus load. Live `/health` on 2026-07-14 reported the latest Dream `completed`; a fresh `make verify-memory-full` checked 12,029 active entries (12,003 knowledge and 26 project), 8,063 archived entries, fetched 12,029 vectors, and reported zero issues. The uncapped 2026-07-13 audit reported 1,348 duplicate clusters covering 5,454 entries; its 1,160 tight clusters cover 3,346 entries. The dry-run drain packed those into 17 cohorts but cannot use the current proposal endpoint reliably.

Two corrections are required before trusting the incident framing. First, Cloudflare's current published paid-Worker default is 10,000 subrequests per invocation, not approximately 1,000, and is configurable; memory remains 128 MB. The observed production error is real, but the deployed account limit was not proven by the handoff, so implementation must instrument actual usage and remain bounded independently of provider defaults. Second, `applyDuplicateMergePlan` currently changes canonical Redis content and calls `persistEntry` without a new embedding; that path only patches Vector metadata. `verify_memory_consistency.py` checks presence and metadata but not the text that produced the vector, so today's zero issues does not rule out stale semantic geometry.

The required architecture is a hybrid with one authority. A remote Python planner bulk-fetches ids/vectors and generates only candidate clusters, using blockwise offline similarity for full audits plus a durable dirty-entry feed for daily changes. A new authenticated submission surface stores versioned candidate manifests and enqueues one bounded cluster per Cloudflare Queue message. The Worker re-reads current Redis entries/revisions and vectors, expands/rejects incomplete or oversized components under current policy, selects the canonical entry, constructs and grades the merge, and applies it. Client-supplied winners or merged entry bodies are invalid.

The mutation boundary is Redis-first and journaled. The Worker computes the intended post-state and conservation result without writes, then one Lua CAS atomically commits all Redis entry/snapshot/index-bookkeeping changes together with the mutation journal and derived-state outbox. The queue does not acknowledge completion until canonical re-embedding/upsert, loser-vector deletion, bounded thin-index patching, and targeted verification finish. Retries are idempotent; a recovery job resumes in-progress journals/outbox entries; exhausted work is visible in a DLQ. Redis remains the source of truth throughout.

This contract also retires the rest of the monolithic nightly rather than preserving adjacent time bombs. After cutover, the Cloudflare cron only enqueues work. Lexical dedup uses dirty fingerprint buckets; retier uses a maintained salience sorted index and bounded cursor jobs; archive/promotion, judge consumption, vector reconciliation, thin-index patches, and recovery are isolated. Secondary indexes are rebuildable accelerators reconciled against Redis, never authority. The safe rollback is to freeze maintenance, not to re-enable the old corpus-scale cron.

## Build Loop vs Product Loop

The build loop can prove that external planners have no merge authority; protected types are guarded at the common boundary; Redis merges and rollbacks are atomic and revision-fenced; queue retries are idempotent; derived-state recovery converges; vectors are bound to embedding input/model/dimensions/revision; retrieval filters stale derived rows through Redis; every message has fixed cost on a 20k-key fixture; the cron is trigger-only; and staging performs a real enqueue, apply, verify, retry/DLQ, and rollback without touching production.

The build loop cannot prove that deduplication improves Arjun's long-run retrieval quality or that every future duplicate will be discovered. Those are product-loop outcomes. Production must therefore advance through an explicitly approved one-cluster canary and verified cohorts, compare uncapped audit membership before and after, keep retrieval probes and strict consistency green, and observe two weekly full audits plus fourteen daily incremental runs before claiming the recurring mechanism has scaled. A falling cluster count alone is not success if recall, protected-memory behavior, journal age, DLQ depth, or embedding freshness regresses.

## Verification Narrative

A fresh Build Mode agent first reproduces the two unguarded current defects with tests: a non-scheduled automatic apply can reach a protected loser, and a duplicate merge can change normalized embedding input while leaving the stored vector semantically old. It then resolves each TODO to repository-native commands and presents the preflight estimate before changing product code.

Local deterministic verification runs the focused authority/protection suite; atomic Lua, queue-redelivery, crash-boundary, rollback, outbox, retrieval-fencing, embedding-hash, thin-index, planner, dirty-index, and cron-cutover suites; a production-shaped 20k-key cost harness with explicit counters; Worker typecheck and full Vitest; Python tests for planner/verifier/ingestion index updates; generated Cloudflare binding checks; strict fixture consistency; scope enforcement; and the required independent review. No gate may infer cost safety from a handful-entry fixture.

With explicit permission, staging creates separate Redis/Vector/Queue/DLQ resources and seeds: one valid lexical cluster, one valid semantic paraphrase cluster, one oversized/incomplete cluster, and one protected-loser cluster. A real remote planner submits candidates. Queue delivery must apply the valid clusters once, hold invalid/protected clusters, re-embed the canonical with matching revision/hash, survive a forced transient failure, expose and replay one forced DLQ item, pass strict consistency, and roll one merge back to exact snapshots. Logs must contain measured budgets rather than fabricated zeros.

Production requires separate explicit permission. Capture `/health`, strict full consistency, uncapped duplicate audit, pending journal/outbox age, DLQ depth, and retrieval probes before mutation. Apply and verify one tight cluster, roll it back, verify again, then apply the first bounded reviewed cohort. Stop immediately unless every targeted check and the expanded `make verify-memory-full` report zero issues. Only then resume checkpointed cohorts. Re-run the uncapped audit and reconcile cluster membership to actual current Redis revisions; do not treat a capped audit or raw count decrease as proof. Keep `SEMANTIC_SLICE_SIZE=0` throughout and prove the live cron never calls the old monolithic maintenance functions.
