# Upstash `knowledge-embeddings` capacity review — 2026-09-10

**Read-only review. Nothing was changed or deleted.**

---

## Bottom line

1. **This is not a capacity problem, and a bigger plan will not fix it.**
   82% of the index is orphaned data that no serving path reads.
2. **The runway is 2–3 days, not "a couple of weeks."**
3. **The leak has one cause: every *failed* rebuild run abandons its staged
   namespace.** Successful runs clean up after themselves correctly. Orphans
   track failures almost one-for-one, day by day.

---

## Measured state (live, 2026-09-10)

| Metric | Value | Source |
|---|---|---|
| Vectors in index | 506,966 | `/info` |
| Quota (`max_vector_count`) | 660,000 | management API |
| **Used** | **76.8%** | derived |
| Headroom | 153,034 | derived |
| Namespaces | 61 × `sf_*` + `(default)` | `/info` |
| Dimension | 3,072 | `/info` |
| `pendingVectorCount` | 0 | `/info` |

Serving pointer `sf:current_generation` = `sf_20260910T162508Z`, promoted today
16:40 UTC. **Serving is healthy and current.**

### Where the vectors are

| Set | Namespaces | Vectors | Share |
|---|---:|---:|---:|
| Live + 2 rollback (the retention policy's keep-set) | 3 | 72,582 | 14.3% |
| **Orphaned** | **58** | **416,136** | **82.1%** |
| `(default)` — legacy `ke_` index, excluded below | 1 | 18,248 | 3.6% |

Of the 58 orphans, **54 (402,550 vectors) were staged and never promoted** —
their `sf:manifest:<gen>` key exists but carries no `published_at`, which
`promote_generation` is the only thing that writes. The remaining 4 are
stragglers from the 2026-08-09 cutover.

**Deleting the 58 takes the index to 90,830 vectors = 13.8% of quota.**

---

## Root cause: the failure path has no cleanup

Retention itself works. `_record_and_prune_generations`
(`ingestion/source_first/publisher.py:296`, `retain=3`) is a sliding window over
`sf:generation_history`, and it prunes correctly — verified directly: the
generation promoted on 09-09 20:56 staged, promoted, and has since been pruned
cleanly, namespace and Redis keys both gone.

The problem is that prune is only reachable from `promote_generation()`
(`publisher.py:288`). The workflow is stage → verify → gate → promote
(`.github/workflows/source-first-rebuild.yml:61-88`). **A run that dies at any
step before promote has already written its ~24,000 vectors, and nothing ever
deletes them.** The generation never enters the history chain, so the sliding
window cannot see it — not now, not ever.

### The evidence: orphans track failures, day for day

300 runs, 2026-08-13 → 2026-09-10. 247 success, 44 failure, 9 cancelled.

| Day | Runs | Failed | Orphan namespaces |
|---|---:|---:|---:|
| 2026-08-13 | 14 | 5 | 5 |
| 2026-08-14 | 11 | 0 | 0 |
| 2026-08-15 | 12 | 12 | 12 |
| 2026-08-16 | 12 | 3 | 3 |
| 2026-08-17 → 08-20 | 48 | 2 | 0 |
| 2026-08-21 | 13 | 8 | 9 |
| 2026-08-22 → 09-03 | 90 | 0 | 0 |
| 2026-09-04 | 14 | 0 | 4 |
| 2026-09-05 | 18 | 7 | 8 |
| 2026-09-06 | 16 | 2 | 3 |
| 2026-09-09 | 14 | 4 | 1 |

Thirteen consecutive clean days produced zero orphans. Every burst of orphans
sits on a day with failures. (09-04 is the one mismatch — likely the cancelled
runs, which abandon a namespace the same way.)

### What the failures actually are

Sampling the eight most recent:

- **`Run exact production retrieval policy against candidate`** — the quality
  gate rejecting the candidate. This is the expensive one: the candidate is
  fully built, so a whole ~24,000-vector namespace is stranded.
- **`Build and stage immutable candidate`** — dies mid-upsert, stranding a
  partial namespace.
- **`Set up job`** — self-hosted runner unavailable. Harmless; nothing staged.
- **`Upload build evidence`** — after promotion, so that generation is in the
  chain and gets pruned normally.

The gate is doing its job. What's missing is that a rejected candidate is never
cleaned up.

---

## Rate and runway

Recent generations are ~24,000 vectors each, up from ~3,500 before the ChatGPT
export integration (`5cb7b41`).

| Basis | Rate | Runway on 153,034 headroom |
|---|---:|---:|
| Calendar days 09-04 → 09-10 | 47,940/day | **3.2 days** |
| Mean over days with activity | 67,115/day | **2.3 days** |

Call it **2–3 days**. I won't give a sharper number than the data supports:
09-05 alone was 146,014 vectors across 8 generations while 09-07 and 09-08
produced none, so the mean is unstable either way.

Framed by the actual mechanism: growth ≈ *failed runs × ~24,000*. At the
observed ~15% failure rate over ~12 runs/day that is roughly 40,000 vectors/day
of pure leak. **Fixing the failure path removes essentially all of the growth.**

**A plan upgrade buys about two weeks and fixes nothing.**

## What actually happens at the cap

Not an outage. `--stage` upserts start failing, promotion stops, and the
Cloudflare worker keeps serving the last good generation
(`cloudflare-mcp/mcp-server/src/sourceFirst.ts:449` queries only
`namespace: <current generation>`). The symptom is a **silently frozen
knowledge base** — retrieval keeps answering, from progressively staler data.

---

## Recommendation

Order matters. A rebuild cannot undo a deletion — it only adds one ~24k
namespace. With 2–3 days of runway, buy the headroom first, then fix the code
without time pressure.

### 1. Delete the 58 orphaned namespaces — needs your approval

Recovers 416,136 vectors, 76.8% → 13.8%. Nothing reads them: the worker only
ever queries the current generation.

Safety conditions I'd apply:
- Re-read `sf:current_generation` and `sf:generation_history` immediately before
  deleting; exclude everything in them.
- Exclude any namespace younger than ~1 hour — a candidate mid-verify is
  transiently live.
- Confirm no rebuild is in flight (the workflow has a concurrency group, so a
  dispatch check is enough).
- Delete one namespace at a time, logging each.

### 2. Clean up on failure

Wrap stage-through-promote so any exit without promotion deletes the candidate
namespace it created. That closes the leak at source.

### 3. Add a reconciling sweep as the backstop

List namespaces from `/info`, keep `sf:current_generation` plus the retained
history, delete the rest. This catches the cases step 2 can't — a killed
runner, a cancelled run — and would have prevented all 58.

### 4. Do not upgrade the plan

It buys ~2 weeks and fixes nothing.

### Worth asking separately

Two days — 08-15 (12/12 runs failed) and 08-21 (8/13) — were near-total
failures of the rebuild. That's a reliability question independent of quota,
and it's not visible in this report's scope.

---

## Excluded from the deletion set

**`(default)`, 18,248 vectors** — the legacy `ke_` index. The current worker
never queries it; the pre-source-first server at `mcp-server/` does
(`mcp-server/src/storage/vector.ts`, no namespace argument = default), and that
tree was last touched **2026-02-05**. I don't know whether it's still deployed,
so I left it alone. Separate decision, not urgent — 3.6% of the index.

## Redis: checked, healthy

The same generations write keys to the paired Redis. **176.0 MB of a 3.0 GB
limit (5.7%)**, 6,329,812 keys. Not a constraint. Orphan Redis keys are still
there, but cleaning them means a SCAN over 6.3M keys — slow, and worth doing as
a separate low-priority pass. The vector namespaces are the pressure point and
are one API call each.

---

## Two corrections worth recording

**`.env` has a duplicate that points at the wrong database.**
`UPSTASH_REDIS_REST_URL` appears twice — line 10 is `knowledge-embeddings`,
line 75 is the **California law chatbot**. `python-dotenv` takes the *last*
occurrence, so any script using `dotenv_values()` on this file silently talks to
the wrong database. It bit this review: an intermediate check appeared to show
the serving pointer unset and the orphan manifests already deleted. Both
readings were discarded. Every number here is from `measured-raven-41051`, the
same database `ingestion/.env` gives the publisher. Arjun's stated convention is
first-key-wins, the opposite of what `dotenv_values` does.

**A wrong intermediate conclusion, discarded.** Because promoted generations get
pruned, the surviving manifests carrying `published_at` are a biased sample —
mostly just the current chain. Reading them at face value suggested serving had
been frozen on the 08-09 generation for 32 days. Checking an actual run showed
09-09 20:56 staged and promoted normally. **Serving was not frozen**; the
sample was.

---

*Files referenced:*
- `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/ingestion/source_first/publisher.py`
- `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/.github/workflows/source-first-rebuild.yml`
- `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/cloudflare-mcp/mcp-server/src/sourceFirst.ts`
- `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/.env`
