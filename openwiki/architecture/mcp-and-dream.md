---
type: "Reference"
title: "Cloudflare MCP and Dream control plane"
description: "Production Cloudflare Worker MCP server for retrieval, salience scoring, Dream lifecycle, judge queue, scheduled async Dream, semantic candidate authority with stale-row guard, and lossless merge gates."
---

# Cloudflare MCP and Dream control plane

`cloudflare-mcp/mcp-server/` is the current production runtime for retrieval and memory governance.

It is a Cloudflare Worker implementation that uses the MCP SDK, Upstash Redis, Upstash Vector, OpenAI embeddings, and a small set of policy modules to shape both search and mutation behavior.

## What lives here

This server is the live control plane for:

- MCP tool serving
- retrieval ranking and policy shaping
- salience computation
- Dream proposal generation, grading, apply, rollback, and verification
- judge queue management
- scheduled async Dream support
- OpenAI-compatible read surfaces
- Twitter and GitHub helper flows — see [Tweets reader subsystem](#tweets-reader-subsystem) for the Worker-side tweet URL reader

The top-level `AGENTS.md` says this is the current production path and should be inspected first for live MCP behavior.

## Main entrypoints

### `src/index.ts`

The main server file wires together:

- authentication and OAuth handling
- read-only and mutating MCP tools
- retrieval helpers
- Dream operations
- judge queue utilities
- tripwires and rate limiting
- source-specific helpers such as tweets and GitHub lookup

The file is large because it owns the full tool surface, but its major responsibilities are still fairly coherent: route, authorize, rank, and delegate to the right memory operation.

### `src/dream.ts`

Contains the core Dream logic and memory mutation helpers.

Recent history and code evidence show this module handles:

- proposal generation
- grading
- bounded apply
- rollback
- duplicate merge and contradiction handling
- scheduled governance operations
- insight synthesis support for content-bearing verdicts

### `src/judgeQueue.ts`

Manages the Worker-side judge queue.

The queue exists so borderline Dream operations can be judged offline and settled later. It now includes an `insight_synthesis` op type, which is the content-bearing verdict path introduced in the recent commit history.

### `src/scheduledDreamAsync.ts`

Implements the async scheduled Dream contract used by the newer orchestrator work.

It enforces:

- idempotent starts
- a per-date Redis lock
- mode gating for shadow vs live
- terminal status validation
- separation between accepted status and executed behavior

### Policy and helper modules

Relevant policy/helper files include:

- `src/salience.ts` — v1 salience: recency decay, mention frequency, type weighting, signal-flag multipliers
- `src/salience_v2.ts` — five-component additive score (usage, evidence, recency, authority, corroboration); TypeScript twin of `distillation/utils/salience_v2.py`, locked in lockstep by `shared/salience_v2_fixtures.json`
- `src/retrievalPolicy.ts` — query-time cross-context and quarantine penalties
- `src/phase8Retrieval.ts` — phase-specific retrieval gates
- `src/phase9OutcomeGate.ts` — outcome-quality gating for Dream operations
- `src/precedence.ts` — authority-then-durability claim comparator; TypeScript twin of `distillation/utils/precedence.py`, locked by `shared/precedence_fixtures.json`
- `src/mmr.ts` — greedy Maximal Marginal Relevance diversity selection for query-time top-K
- `src/tripwires.ts` — anomaly kill switches (see [Tripwires](#tripwires))
- `src/consolidation.ts` — `formatConsolidationNote` timestamped audit-note formatter
- `src/embeddingFreshness.ts` — `text-embedding-3-large` (3072-dim) model/dimension constants and embedding-metadata freshness check

These modules are where the repo encodes its memory-specific heuristics, thresholds, and safety rules.

## Retrieval behavior

Retrieval is not a plain vector lookup.

The code uses additional policy shaping such as:

- topic bucket classification
- cross-context penalties
- quarantine suppression
- salience scoring and source weighting
- phase-specific retrieval gates

The goal is to keep unrelated memories from surfacing uninvited while still allowing them to be found on direct query.

`retrievalPolicy.ts` documents the rationale for this in code comments: the primary fix for unwanted cross-topic surfacing is to score down unrelated items rather than hard-filtering them.

### Salience v2, MMR, and the RANKING_V2 flag

The retrieval path has a shadow-to-live ranking upgrade tracked by contract
`PKS-INJECTION-RANKING-002` (see `contracts/injection-ranking-v2.spec.md`).

- `salience_v2.ts` computes a five-component additive score
  (`0.30*usage + 0.25*evidence + 0.20*recency + 0.15*authority + 0.10*corroboration`)
  during the nightly Dream pass and writes it to `metadata.salience_v2`. It is
  **not consulted by live ranking or tiering** until the `RANKING_V2` env var is
  set to `on` (Phase B). The weights and recency half-lives live in the
  `salience_v2` block of `shared/memory_policy.json`.
- `mmr.ts` is the Phase B query-time selector. `selectSearchTopResults` in
  `index.ts` switches between the legacy "sort + slice top-K" path and greedy
  MMR diversity selection based on the same `RANKING_V2` flag. INV4 guarantees
  the single best match is never displaced: the first pick is always
  `argmax(finalScore)` with no diversity penalty; only picks #2+ apply the MMR
  penalty, per-domain cap, and token budget.
- `precedence.ts` feeds the `authority` component: it resolves which of two
  conflicting claims wins on an authority-then-durability lattice (user > behavioral
  > assistant > inferred), with recency only as the final tiebreak.

Both `salience_v2.ts` and `precedence.ts` must stay semantically identical to
their Python twins (`distillation/utils/salience_v2.py`,
`distillation/utils/precedence.py`); the shared fixture tables
(`shared/salience_v2_fixtures.json`, `shared/precedence_fixtures.json`) are
replayed by both the Vitest suite and the Python unittest suite to enforce that
lockstep.

`RANKING_V2` is also a tripwire kill switch: if the MMR path causes a retrieval
collapse, `tripwires.ts` can flip a Redis kill flag that overrides the env var
back off.

## Dream governance

Dream is the repository's maintenance and consolidation system.

The current code and recent docs show it is no longer just a simple archive job. It now includes:

- candidate discovery
- proposal creation
- deterministic grading
- revision-pinned evidence support sets for every proposed mutation
- bounded live apply
- rollback mechanics
- duplicate merge handling
- contradiction handling
- judge queue interplay
- outcome-quality gating
- content-bearing insight synthesis in the judge path, with at least three
  in-cluster supporting entry IDs persisted on the resulting memory

The evidence gate is structural rather than an LLM score. Every operation's
`evidence.support_entry_ids` must be non-empty, contained in the proposal
snapshot, backed by `candidate_revisions`, and cover every entry the operation
touches. Insight verdicts use the same principle: support IDs must come from
the judged cluster, and an append anchor must itself be in the support set.

This is the place to look when the repo changes what counts as durable memory, how memories are consolidated, or when a nightly maintenance action becomes safe to apply.

## Semantic candidate authority and stale-row guard

Externally planned semantic duplicate candidates (from the Python planner) are
not trusted. `processSemanticCandidateTask` in
`cloudflare-mcp/mcp-server/src/dream.ts` is the Worker-side queue authority: it
re-reads current Redis entries and vectors, revalidates the cluster against
current policy and revisions, chooses the canonical winner, and only then
delegates to the common merge boundary. A terminal result is cached before
acknowledgement so at-least-once queue delivery is harmless.

The candidate guard enforces, in order:

1. `validateCandidateCluster` (in `semanticMaintenance.ts`) recomputes the
   connected component from freshly loaded vectors using the Upstash COSINE
   score scale and rejects disconnected, oversized, cross-type, archived, or
   vector-missing clusters.
2. The stale-row guard: for each candidate vector, the Worker queries Vector
   for neighbours, then filters every hit through `isCurrentActiveEntry` (a
   Redis existence + non-archived check) before considering it a current
   same-type neighbour. Vector can retain stale rows after a Redis entry is
   removed or archived, so a hit is safety-relevant only after this Redis check.
   `isCurrentOmittedNeighbor` (in `semanticMaintenance.ts`) then decides
   whether a current, same-type, above-threshold neighbour was omitted from
   the submitted candidate; if so the task is held with reason
   `candidate_component_incomplete`.
3. Protected context types (`explicit_save`, `professional_identity`,
   `stated_preference`) are blocked at the common automatic apply boundary
   (`assertAutomaticMergeAllowed`); a cluster with more than one protected
   member is held with reason `multiple_protected_members`.

This guard embodies `contracts/durable-semantic-consolidation-v2.spec.md`
INV1 (the Worker re-reads current Redis and Vector and rejects stale or
incomplete clusters) and the maintenance-side form of INV10 (Vector hits are
validated against current Redis state before being trusted).

```mermaid
flowchart TD
    A["External planner submits candidate cluster ids"] --> B["processSemanticCandidateTask re-reads Redis + Vector"]
    B --> C["validateCandidateCluster recomputes connected component"]
    C -->|"disconnected / oversized / cross-type / archived"| Held["held with reason"]
    C -->|"valid component"| D["Query Vector for each candidate's neighbours"]
    D --> E["isCurrentActiveEntry Redis check per neighbour hit"]
    E --> F["isCurrentOmittedNeighbor over Redis-verified current set"]
    F -->|"current same-type neighbour omitted"| Held
    F -->|"no omitted current neighbour"| G["Protected-type + canonical selection"]
    G -->|"multiple protected members"| Held
    G -->|"ok"| H["applyDuplicateMergePlan via common merge boundary"]
```

The candidate validation flow: an externally planned cluster is revalidated against current Redis and Vector before any merge is authorized.

When changing this guard, the focused test is
`cloudflare-mcp/mcp-server/test/durableSemanticConsolidation.test.ts`
("ignores stale or cross-type vector neighbours"), which pins that a stale
vector hit (not in the Redis-verified `currentEntryIds` set) is ignored and a
live same-type hit is not. The minimal validation is the Worker Vitest suite
(`make worker-test`, i.e. `npm run test:worker` / `vitest run
--no-file-parallelism` in `cloudflare-mcp/mcp-server`); for a narrow run target
the contract's G3 gate uses `test/semantic-dedup.test.ts`.

## Hard merge gates and the semantic cursor

Two pure-logic modules back the lossless-merge and starvation-free-sweep
contracts behind semantic consolidation.

`cloudflare-mcp/mcp-server/src/mergeGates.ts` is the "Ring 1" hard gate for
every duplicate merge. It is wired unconditionally into
`applyDuplicateMergePlan` (the single choke point for both the governed nightly
path and the legacy operator path) and provides:

- `collapseNearDuplicateInsights` — deterministic Jaccard word-overlap collapse
  of near-identical insights, returning a drop-to-retained receipt mapping so no
  insight disappears without a receipt (INV5).
- `validateMergeConservation` — an independent post-hoc gate that recomputes
  the expected merge output from the parent entries alone and compares it
  against what the merge actually produced; a failing merge is never persisted
  (INV3).

The scheduled duplicate-merge cap is coupled to these gates by code, not merely
by convention: `resolveScheduledDuplicateMergeLimit` in `index.ts` clamps the
cap to 10 unless `merge_hard_gates_active` is literally `true` in
`shared/memory_policy.json`. The policy currently has
`merge_hard_gates_active: true` and `scheduled_duplicate_merge_limit: 50`, so
the gates are live and the cap is 50. Raising the cap requires flipping the
gate flag in the same change.

`cloudflare-mcp/mcp-server/src/semanticCursor.ts` is the rolling cursor that
bounds the nightly semantic-dedup slice to at most 200 candidates and 400
vector queries per run (Worker subrequest budget). It persists only a numeric
position and cycle bookkeeping in a single Redis key; the caller re-derives the
sorted candidate id list every run. `advanceSemanticCursor` is called only
after a completed slice, so a crashed mid-sweep run retries the same slice next
night (starvation-free, INV4). The slice is currently disabled:
`SEMANTIC_SLICE_SIZE: 0` in `shared/memory_policy.json` short-circuits the pass
before it loads anything, after the semantic slice's extra full-corpus load blew
the Cloudflare subrequest cap and killed the nightly Dream (see the policy's
`_subrequest_note`). Do not raise it until the nightly stops loading the whole
corpus multiple times and a real run has measured subrequest headroom.

The contracts behind both modules live in
`contracts/semantic-consolidation.spec.md` (PKS-SEMANTIC-CONSOLIDATION-001) and
`contracts/durable-semantic-consolidation-v2.spec.md`
(PKS-DURABLE-SEMANTIC-CONSOLIDATION-002). Focused tests:
`test/mergeGates.test.ts` (lossless-merge fixture library, G2 gate) and
`test/semanticCursor.test.ts` plus `test/semantic-dedup.test.ts` (bounds and
cursor coverage, G3 gate).

## Maintenance journal and queue

Two small modules back the durability and at-least-once-delivery contract for
governed semantic consolidation.

`cloudflare-mcp/mcp-server/src/maintenanceQueue.ts` defines the Cloudflare
Queue message schema (`MaintenanceMessage`, `schema_version: 1`) and the task
kinds that flow through the `DREAM_MAINTENANCE_QUEUE` queue binding:
`semantic_candidate`, `vector_outbox`, `recovery`, `lexical_bucket`,
`retier_cursor`, and `thin_index_patch`. `validateMaintenanceMessage` enforces
identity, kind, candidate-id bounds (2–6, matching
`MAX_MAINTENANCE_CLUSTER_SIZE`), and timestamp validity before a message is
trusted. `maintenanceRetryDelaySeconds` provides bounded exponential backoff up
to `MAX_MAINTENANCE_ATTEMPTS` (5). These are the queue-level invariants that let
`processSemanticCandidateTask` cache a terminal result before acknowledgement
so at-least-once delivery is harmless.

`cloudflare-mcp/mcp-server/src/maintenanceJournal.ts` is the atomic-commit
journal that backs `applyDuplicateMergePlan`. `buildMaintenanceJournal` records
the canonical id, duplicate ids, expected revisions, vector outbox key, and
before-snapshots. `ATOMIC_MERGE_COMMIT_LUA` is the Redis Lua CAS script the
production adapter runs in a single invocation: it checks the journal status
and every entry's expected `revision` before committing, so a concurrent write
to any touched entry fails the whole merge atomically. This is the structural
mechanism behind the "no derived-store acknowledgement is valid before the CAS
script returns 1" contract.

## Tripwires

`cloudflare-mcp/mcp-server/src/tripwires.ts` is the automated safety scaffold
for Dream and forgetting. It defines three Redis-backed kill switches:
`DREAM_AUTO_APPLY_MODE`, `RETRIEVAL_POLICY_MODE`, and `RANKING_V2`. Because
Cloudflare Workers cannot modify their own env vars at runtime, the effective
mode is the more restrictive of the operator env var (the ON switch) and the
Redis kill flag (the OFF override set by a tripwire firing).

The tripwires fire on two anomaly patterns over a 14-day median baseline:
a destructive-action spike (`DESTRUCTIVE_SPIKE_MULTIPLIER = 3`) and a retrieval
collapse (`RETRIEVAL_COLLAPSE_RATIO = 0.7`), each requiring two consecutive
breach days (`CONSECUTIVE_DAYS_REQUIRED = 2`). A separate
`HARD_DELETE_DAILY_CAP_DEFAULT = 5` caps irreversible hard-delete operations
per day. When a tripwire fires, the relevant kill flag flips and the affected
mode is forced off until the operator manually clears the Redis flag.

## Read/write tool surface

`AGENTS.md` notes the production server exposes read-only tools such as index, context, deep retrieval, search, and health, and write tools that require `mcp:write` scope.

If you are changing one of these tools, verify whether the change affects:

- auth scopes
- OpenAI-compatible routes
- retrieval ranking
- judge queue settlement
- Dream apply/revert flows
- tests in `cloudflare-mcp/mcp-server/test/`

## Tweets reader subsystem

`cloudflare-mcp/mcp-server/src/tweets/` is the production surface behind the
`read_tweet`, `read_thread`, and `health` MCP tools. It is distinct from the
Python Twitter/X ingestion pipeline (`ingestion/twitter/run.py`, see
[Ingestion and distillation workflow](../workflows/ingestion-and-distillation.md)):
the Worker tweets reader resolves a public X/Twitter URL pasted by an operator at
query time and returns normalized post/thread content, while the ingestion
pipeline pulls the operator's own timeline into durable memory.

The subsystem is organized into five modules under `src/tweets/`:

- `url-parser.ts` — normalizes operator-supplied URLs into a canonical
  `https://x.com/<user>/status/<id>` form. Accepts `x.com`, `twitter.com`,
  `fixupx.com`, `fxtwitter.com`, `vxtwitter.com`, mobile/m prefixes, `nitter.*`
  hosts, and `t.co` short links (the latter resolved to a final URL before
  parsing). Rejects non-Twitter hosts and malformed IDs with a typed
  `TweetReaderError` (`invalid_url`).
- `types.ts` — the shared `ReadTweetOutput` / `ReadThreadOutput` /
  `NormalizedTweetUrl` / `TweetMedia` types and the `TweetReaderError` /
  `TweetUpstreamError` error classes. `source_api` records which upstream served
  the result (`fxtwitter`, `vxtwitter`, or `adhx`).
- `fetchers.ts` — the multi-upstream fallback chain. `fetchTweetWithFallback`
  tries `fetchFxTweet` → `fetchVxTweet` → `fetchAdhxTweet` in order, continuing
  only on recoverable upstream errors (`not_found`, `protected`, or a
  `TweetUpstreamError`); a hard client error (`invalid_url`) aborts immediately.
  `fetchFxThread` resolves a self-reply thread via the fxtwitter API.
  `checkTweetUpstreams` is a lightweight liveness probe used by the `health`
  tool to report FxTwitter/VxTwitter/ADHX upstream status.
- `article-flattener.ts` — `flattenArticle` converts X Article (long-form post)
  Draft.js block structures into Markdown body text so article-bearing tweets
  surface as readable content instead of raw block JSON.
- `cache.ts` — `getTweetCacheKey` derives a deterministic Redis cache key from
  the tweet id and the media-alt-text inclusion flag.

When changing this subsystem, the focused tests are
`cloudflare-mcp/mcp-server/test/url-parser.test.ts` (URL canonicalization,
`t.co` resolution, host rejection) and
`cloudflare-mcp/mcp-server/test/article-flattener.test.ts` (Draft.js → Markdown
flattening, already-flattened passthrough). The minimal validation is the
Worker Vitest suite (`make worker-test`); a narrow run target is
`vitest run test/url-parser.test.ts test/article-flattener.test.ts` in
`cloudflare-mcp/mcp-server`.

## Important caution

There are two MCP implementations in the repo.

- `cloudflare-mcp/mcp-server/` is the current production Worker path.
- `mcp-server/` is the older Vercel-style implementation.

Do not treat them as interchangeable. They share intent, but their runtime environment and current responsibilities differ.

## Main source anchors

- `cloudflare-mcp/mcp-server/src/index.ts`
- `cloudflare-mcp/mcp-server/src/dream.ts`
- `cloudflare-mcp/mcp-server/src/judgeQueue.ts`
- `cloudflare-mcp/mcp-server/src/scheduledDreamAsync.ts`
- `cloudflare-mcp/mcp-server/src/salience.ts`
- `cloudflare-mcp/mcp-server/src/salience_v2.ts`
- `cloudflare-mcp/mcp-server/src/retrievalPolicy.ts`
- `cloudflare-mcp/mcp-server/src/phase8Retrieval.ts`
- `cloudflare-mcp/mcp-server/src/phase9OutcomeGate.ts`
- `cloudflare-mcp/mcp-server/src/precedence.ts`
- `cloudflare-mcp/mcp-server/src/mmr.ts`
- `cloudflare-mcp/mcp-server/src/tripwires.ts`
- `cloudflare-mcp/mcp-server/src/semanticMaintenance.ts`
- `cloudflare-mcp/mcp-server/src/mergeGates.ts`
- `cloudflare-mcp/mcp-server/src/semanticCursor.ts`
- `cloudflare-mcp/mcp-server/src/maintenanceJournal.ts`
- `cloudflare-mcp/mcp-server/src/maintenanceQueue.ts`
- `cloudflare-mcp/mcp-server/src/embeddingFreshness.ts`
- `cloudflare-mcp/mcp-server/src/consolidation.ts`
- `cloudflare-mcp/mcp-server/src/tweets/url-parser.ts`
- `cloudflare-mcp/mcp-server/src/tweets/fetchers.ts`
- `cloudflare-mcp/mcp-server/src/tweets/article-flattener.ts`
- `cloudflare-mcp/mcp-server/src/tweets/types.ts`
- `cloudflare-mcp/mcp-server/src/tweets/cache.ts`
- `cloudflare-mcp/mcp-server/test/`
- `cloudflare-mcp/mcp-server/package.json`
