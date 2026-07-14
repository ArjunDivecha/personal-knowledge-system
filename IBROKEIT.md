# IBROKEIT.md — nightly Dream outage, what's fixed, what's still broken

**Handoff for GPT-5.6-sol.** Written by Claude (Opus 4.8), 2026-07-14, after I broke
production and partially fixed it. Read this cold; it is meant to be self-contained.

**INPUT FILES (referenced, all absolute):**
- `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/cloudflare-mcp/mcp-server/src/dream.ts`
- `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/cloudflare-mcp/mcp-server/src/index.ts`
- `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/cloudflare-mcp/mcp-server/src/semanticCursor.ts`
- `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/cloudflare-mcp/mcp-server/src/mergeGates.ts`
- `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/shared/memory_policy.json`
- `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/scripts/audit_memory_quality.py`
- `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/scripts/drain_semantic_backlog.py`
- `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/scripts/repair_vector_drift.py`
- `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/contracts/semantic-consolidation.spec.md`
- `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/cloudflare-mcp/mcp-server/test/dreamProposalScoping.test.ts`

**OUTPUT FILES:** none. This is a design/handoff document.

---

## 0. TL;DR

I added a "bounded semantic slice" to the nightly Dream cron. It blew Cloudflare's
**~1000-subrequest-per-Worker-invocation** limit. The nightly died for **four days**
(2026-07-11 → 07-14), and because it crashed **mid-apply** it left Redis and the
vector index out of step.

**Now:** nightly is RESTORED and verified green. Drift repaired. But I restored it by
**disabling the semantic consolidation feature entirely** (`SEMANTIC_SLICE_SIZE=0`).

**So the actual job is still undone:** ~46% of the memory corpus is duplicate entries,
and nothing drains them. Your job is to design/build the thing that does — correctly,
this time.

A 5-model council red-teamed my proposed fix and returned **VERDICT: revise (confidence
high)**. Their findings are in §6 and they matter — they caught two hard ceilings I
hadn't even considered, and one factual claim of mine that was wrong.

---

## 1. The system

Personal Knowledge System (PKS) — Arjun's cross-session AI long-term memory. **This is
live infrastructure; every other AI session reads from it.**

- **Cloudflare Worker** (`cloudflare-mcp/mcp-server/`) — the MCP server and the *only*
  write gateway. Production: `mcp.dancing-ganesh.com`, worker `arjun-knowledge-mcp`.
  Deploy: `cd cloudflare-mcp/mcp-server && npm run deploy`.
- **Upstash Redis** (REST) — **source of truth**. ~19.7k keys, ~11.7k active entries
  (`knowledge:ke_*`, `project:pe_*`), rest archived.
- **Upstash Vector** (REST) — embeddings (`text-embedding-3-large`, 3072-dim) plus a
  small *derived copy* of entry metadata used for search filtering. **Rebuildable from
  Redis.**
- **Nightly cron** `10 7 * * *` (UTC) → Worker `scheduled()` → `runScheduledGovernedDream`
  (`index.ts`). Also triggerable via `POST /ops/dream/run_scheduled_governed`.

### The hard constraint that killed me
**Every Redis and Vector call from the Worker is an HTTP subrequest.** Cloudflare caps
these at **~1000 per invocation**, and the cap is **GLOBAL to the invocation** — once
exhausted, *every subsequent* subrequest fails. This is why a `try/catch` around the
offending phase did **not** save the job: the budget was already spent, so the `grade`,
`apply`, and `index-rebuild` phases that followed all failed too.

---

## 2. What the nightly does (one Worker invocation)

1. **retier cycle** — quarantine/demote, percentile re-tier. *Loads full corpus.*
2. **`runDreamProposal`** — lexical dedup, archive/promote candidates. *Loads full corpus.*
3. **bounded semantic slice** (← the thing I added, and what broke it). *Loads full corpus
   AGAIN*, purely to sort by `(injection_tier, id)` and pick N cursor-slice ids.
4. **grade** — deterministic hard gates.
5. **apply** — writes. Each op does CAS on `expected_revision` (re-reads entries).
6. **thin-index rebuild**.

### The cost, precisely

`loadEntryBatchByType(redis, type, idFilter?)` — `dream.ts` L1823:
- `SCAN {type}:*` over **all ~19.7k keys** (including archived)
- `mget` all entries
- `mget` all access-count keys
- `mget` all last-accessed keys
- compute salience per entry

…and it's called for **both** `"knowledge"` and `"project"`. The nightly therefore loads
the **entire corpus three separate times**.

`makeVectorNeighborFn` (`dream.ts`, near `prefetchEntryVectors`) cost **2 vector
subrequests per candidate**:
- `index.fetch([id])` for the entry's own vector
- `index.query({vector})` for its neighbours

**Upstash SDK gotcha:** `query` takes a **single payload — there is no batch query**.
`fetch` **does** accept an array. So per-candidate neighbour search is inherently
~1 subrequest per candidate.

A 200-entry slice therefore cost ~400 vector subrequests *on its own*, on top of three
full-corpus Redis loads. → **"Too many subrequests by single Worker invocation."**

---

## 3. The damage (all repaired, but read this — it defines the safety bar)

The crashed runs died **mid-apply**, leaving Redis ↔ Vector drift:
- **14 entries ACTIVE in Redis with NO vector at all** → silently invisible to semantic
  search. Real, undetectable-by-the-user retrieval loss.
- **89 entries with stale vector metadata** (e.g. vector says `injection_tier: 1`, Redis
  says `2`) → degraded ranking/filtering.
- `make verify-memory-full` went from **0 issues** (2026-06-26 baseline) → **104**.

Repaired via `scripts/repair_vector_drift.py`: re-derives each flagged entry's embedding
+ metadata **from Redis** (source of truth) and upserts the vector. Never writes Redis;
skips archived entries (which correctly have no vector). **104 → 0.**

**The lesson to carry:** any new design must be **crash-safe mid-apply**, not merely
cheap. A partial apply that leaves the vector index inconsistent is worse than a job that
doesn't run.

---

## 4. Current state (verified on production, not inferred)

**Nightly is RESTORED:**
```
status: completed          HTTP 200 in 81s   (was: HTTP 500, "Too many subrequests")
semantic_slice: skipped_reason=semantic_slice_disabled:SEMANTIC_SLICE_SIZE=0
30 duplicate_merge applied, 0 held, verification passed: true
thin index rebuilt;  make verify-memory-full → issues=0
```

**What I shipped to get there:**
1. Pushed the candidate-id filter **down into** `loadEntryBatchByType` so a *targeted*
   proposal reads only the keys it asked for (no `SCAN`; `mget` only those ids).
   Previously `runDreamProposal` loaded the **whole corpus and only then filtered** to
   `candidate_ids` — so `candidate_ids` bought **nothing**. Guarded by
   `test/dreamProposalScoping.test.ts`, which asserts **COST** (keys read, scan count),
   not just the result, and which **fails against the old code**.
2. `prefetchEntryVectors` — batch `index.fetch` 100 ids at a time and seed the
   neighbour-fn cache (200 fetch subrequests → 2). *(The query half cannot be batched —
   see §2.)*
3. **`dedup.SEMANTIC_SLICE_SIZE = 0`** in `shared/memory_policy.json` — a kill-switch that
   short-circuits `runBoundedSemanticSlicePass` (`index.ts` L1426) **BEFORE any corpus
   load**. The check *must* precede the load, because **the load IS the cost**.

**Suites:** Worker 327/327, Python 374/374, typecheck clean. All pushed to `origin/main`.

### ⚠️ THEREFORE: semantic consolidation is SHIPPED BUT DISABLED
The backlog does **not** drain. This is the open problem.

**What IS still live and working** (shipped in the same change; do not lose these):
- **merge-conservation gates** — `validateMergeConservation` (`dream.ts` L4927), inside
  the apply path. Hard-fails any merge that would drop evidence.
- **protected-type hold** — `explicit_save` / `professional_identity` /
  `stated_preference` may never be an automatic merge **loser** (INV1).
- cap coupling (scheduled `duplicate_merge` limit tied to gates being active).

---

## 5. The actual open problem

**Duplicate backlog** (measured 2026-07-13, full uncapped audit, cosine ≥ 0.95):
- **1,348 clusters covering 5,454 entries — ~46% of the corpus.**
- Of those, **1,160 "tight" clusters (2–6 members) covering 3,346 entries** are the safe
  merge candidates.
- **188 oversized clusters (>6 members, 2,108 entries)** are excluded by
  `SEMANTIC_MAX_CLUSTER_SIZE` — the chaining pathology the 0.95 threshold exists to avoid.
  (History: at cosine 0.86 this chained into a single **1,980-member mega-cluster**.
  Don't lower the threshold.)
- `make audit-memory-quality` fails its quality gate on this (duplicate share 0.458 vs a
  0.2 threshold).

**Reproduce the scope:**
```bash
distillation/venv/bin/python scripts/audit_memory_quality.py \
    --skip-recall --skip-temporal --max-dup-queries 13000
# → report at scripts/reports/audit_memory_quality_<STAMP>.json
# → m4_duplicates.all_tight_clusters  (I added this: FULL cluster membership)
```
⚠️ **Must be uncapped.** A capped scan (`query_capped: true`) yields an incomplete cluster
list, and draining a silent subset *looks* like success.

### The inflow is closed (but see §6 — I was wrong about what that implies)
Ingestion-time dedup (`admission_dedup`) went **live** on 2026-07-13. New near-duplicates
are now merged at ingestion instead of created. Evidence: a shadow run of two
already-ingested repos extracted 316 entries of which **263 were near-duplicates**
(median cosine 0.951).

---

## 6. 🔴 COUNCIL RED-TEAM — READ THIS BEFORE DESIGNING ANYTHING

I proposed a fix and had it adversarially reviewed by 5 frontier models from 5 labs.
**VERDICT: revise. CONFIDENCE: high.** They were right and I was wrong.

**My proposed (now-dead) plan was:**
- (A) Load the corpus **once** per invocation and thread it through all three consumers.
- (B) Maintain a lightweight Redis secondary index (sorted set of active ids +
  `(tier, id)`) so the slice can pick its cursor slice without loading everything.
- (C) Re-enable the slice at size 25–50 after *measuring* subrequest headroom.
- (D) *Alternative:* move the semantic drain **off** the Worker into Python, using the
  Worker's governed `apply` endpoint only for the final write.

**What the council said:**

1. **Category error.** All 5 agreed: corpus-scale work inside a request-scoped Worker with
   a hard subrequest ceiling is **the wrong place, full stop**. (A)/(B)/(C) are
   constant-factor optimization against a ceiling that keeps moving — they *delay*
   re-failure, they don't prevent it. (C) specifically is "a re-break with a delay timer."

2. **My weakest assumption — and I'd told Arjun this as fact:**
   > *"The backlog is now a FIXED pool, not growing."*
   
   **All five independently flagged this as wrong.** Merging **changes vector geometry**,
   and re-ingestion re-embeds — **new candidates keep appearing**. This is **not**
   one-and-done. Do not build a one-shot script and call it solved; this needs a
   **durable, repeatable** mechanism.

3. **Two ceilings I never even considered:**
   - **V8 heap (~128MB).** Loading a growing ~20k-entry JSON corpus into a Worker will
     **OOM** — *even if you fix subrequests entirely*. My plan (A) walks straight into this.
   - **Upstash 429 rate limits.** These will likely bite **before** the 1000-subrequest cap
     is even reached. The subrequest limit may be the *secondary* constraint.

4. **Split on (D)** — 3 of 5 called it a dangerous trap that "splits the truth" (two
   implementations of *"what is a duplicate"* that will drift). 1 said it's viable **iff**
   Python is relegated to a **candidate generator** that submits to the Worker's governed
   `apply` for CAS + gate enforcement.

5. **Their recommended direction:**
   - **Abandon the monolithic cron.** Use **Cloudflare Queues** or **Durable Object
     alarms**: have the cron trigger a batching orchestrator that chains many **small,
     independent, governed invocations** — each comfortably under every ceiling.
   - **Externalize *planning*, not *authority*.** If Python is involved: bulk-export
     embeddings and do **all-pairs similarity offline** (this avoids O(n) vector
     subrequests entirely), then submit **final merge plans** to the Worker's `apply`
     endpoint, where the conservation gates and CAS are **strictly enforced**.

Full log:
`/Users/arjundivecha/Dropbox/AAA Backup/A Working/Council Skill/runs/20260714T065822.json`

---

## 7. Constraints you must respect

**Safety (non-negotiable — these are contract invariants):**
- **Redis is the source of truth.** Vector is derived and rebuildable. Never let Vector
  win.
- **All writes go through the Worker's governed apply path.** That's where
  `validateMergeConservation` runs. **Do not reimplement merging in Python** — that
  bypasses the very gates that make merges lossless.
- **Protected types are never automatic merge losers**: `explicit_save`,
  `professional_identity`, `stated_preference` (policy:
  `dream_thresholds.archive_protected_context_types`).
  ⚠️ **TRAP:** this hold is enforced **only** in `buildScheduledGovernedDecision`
  (`index.ts` L1317) — i.e. the **scheduled** path. The **manual** `/ops/dream/apply`
  endpoint does **NOT** enforce it. Any external driver must re-check it client-side.
  (`scripts/drain_semantic_backlog.py` already does; see `protected_losers()`.)
- **Every merge must be individually reversible** (`rollback_dream_apply`).
- **Crash-safe mid-apply.** See §3.
- **`make verify-memory-full` must be 0 issues** after any batch. This is the stop-on-fail
  gate — and it's the check the *previous* attempt at this drain (2026-06-08) died on,
  after only 35 merges.

**Operational:**
- Operator endpoints are rate-limited to **12 calls/hour, PER ENDPOINT** (`proposal`,
  `grade`, `apply` each get their own budget).
- `/ops/dream/proposal` accepts **≤200** `candidate_ids`; `/ops/dream/apply` accepts
  **≤100** `operation_ids`.
- ⚠️ **`/ops/dream/proposal` currently times out even with 24 candidates** — because
  `runDreamProposal` still loads the full corpus for the *unfiltered* parts of its work.
  My scoping fix helped the *load*, but the endpoint is still not a viable planning API at
  corpus scale. **This is why my drain script (below) cannot run.**
- `run_scheduled_governed` **de-duplicates by UTC day** (`getScheduledGovernedBoundaryKey`,
  72h TTL). To force a fresh run, pass a future `scheduled_time`. It also takes a
  single-flight lock (`dream:lock`) — a crashed run leaves it held until TTL (~23 min).
- Credentials: `ingestion/.env` (`UPSTASH_*`, `DREAM_OPERATOR_TOKEN`, `OPENAI_API_KEY`).
  ⚠️ The **repo-root `.env` has duplicate `UPSTASH_REDIS_REST_URL` keys** and
  python-dotenv resolves to the **WRONG** (California-law-chatbot) instance. **Use
  `ingestion/.env`.**

---

## 8. Assets already built (reuse, don't rewrite)

- **`scripts/audit_memory_quality.py`** — read-only. I extended it to emit
  `m4_duplicates.all_tight_clusters` (full membership of every 2–6-member cluster). Its NN
  scan is **fast and proven** (~12k queries in ~15 min). **Reuse this for clustering.**
- **`scripts/drain_semantic_backlog.py`** — batched drain: clusters kept intact within a
  batch, protected-type guard, `verify-memory-full` after every batch, stop-on-fail,
  resumable checkpoint, rate-limit aware, dry-run double-gated.
  **STATUS: BLOCKED** — it drives `/ops/dream/proposal`, which times out (§7). The
  *structure* is sound and worth keeping; the *planning mechanism* is what must change.
- **`scripts/repair_vector_drift.py`** — repairs Redis↔Vector drift. Vector-only writes.
  Keep this; you may well need it again.
- **`test/dreamProposalScoping.test.ts`** — asserts **cost** (keys read, scan count).
  **This is the pattern to copy.**

### The single biggest testing lesson
**Every test in this repo runs against a fixture corpus of a handful of entries. Not one
could have caught this bug, because the bug is purely a function of corpus SIZE.**
A feature whose cost scales with the corpus needs a test that asserts the **COST**
(subrequests / keys read / bytes loaded), not just the result. Otherwise it passes 327/327
and dies in production.

---

## 9. Your job

Design and build a **durable, repeatable** semantic consolidation mechanism that:
1. Drains the existing backlog (~1,160 tight clusters / 3,346 entries), **and**
2. Keeps draining as new candidates appear (they will — see §6.2), **and**
3. Never again exceeds a Worker ceiling — **subrequests, V8 heap, or Upstash rate limits**,
   **and**
4. Preserves every safety invariant in §7 (conservation gates, protected types,
   reversibility, crash-safety, `verify-memory-full` = 0).

The council's steer: **Queues / Durable Object alarms chaining many small governed
invocations**, and/or **externalize planning (offline all-pairs similarity) while keeping
authority (apply + gates + CAS) in the Worker**.

Open questions I could not resolve and which are genuinely yours to decide:
- Is there a way to submit a **pre-computed merge plan** to a governed apply endpoint
  without creating a second, drift-prone implementation of "what is a duplicate"? (This is
  the 3-vs-1 split in §6.4 — the crux of the design.)
- Should the **retier cycle and lexical proposal** *also* be moved off the monolithic cron?
  They each load the full corpus too, and the V8/rate-limit ceilings apply to them as well
  — the nightly is arguably *already* living on borrowed time even with my feature disabled.

**Do not trust my framing over the evidence.** I got the "fixed pool" claim wrong, I missed
two hard ceilings, and I broke this system once already. Verify everything against the
running system.
