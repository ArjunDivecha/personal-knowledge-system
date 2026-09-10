# Upstash `knowledge-embeddings` capacity review — 2026-09-10

**Read-only review. Nothing was changed or deleted.**

---

## Bottom line

1. **This is not a capacity problem, and a bigger plan will not fix it.**
   82% of the index is orphaned data that no serving path reads.
2. **The runway is 2–3 days, not "a couple of weeks."**
3. **Retention code already exists and has never deleted anything.** This is a
   broken feature, not a missing one — which changes the fix.

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

Serving pointer `sf:current_generation` = `sf_20260910T162508Z`.
`sf:generation_history` = today's three generations.

### Where the vectors are

| Set | Namespaces | Vectors | Share |
|---|---:|---:|---:|
| Live + 2 rollback (the retention policy's own keep-set) | 3 | 72,582 | 14.3% |
| **Orphaned generations** | **58** | **416,136** | **82.1%** |
| `(default)` — legacy `ke_` index, excluded below | 1 | 18,248 | 3.6% |

**Deleting the 58 orphans takes the index to 90,830 vectors = 13.8% of quota.**

---

## Root cause: retention runs, but can only ever see three generations

`ingestion/source_first/publisher.py:296` — `_record_and_prune_generations(...,
retain: int = 3)`, added 2026-08-10 in `345e5ea`, the first source-first commit.
So all 58 orphans accumulated *with retention in place*.

The bug is at `publisher.py:315-316`:

```python
keep  = history[:retain]
prune = history[retain:]
```

`history` is rebuilt each run from `sf:generation_history`, **which the previous
run already truncated to 3**. So the prune candidate list is only ever the one
or two entries that fall off the end of that chain. It is never reconciled
against the actual namespace inventory. Any generation that leaves the chain
without being pruned — a staged-but-never-promoted run, or any break in the
chain — becomes invisible to the cleanup path permanently.

**Verified, not inferred:** every orphan sampled still has its
`sf:manifest:<generation>` Redis key present (`exists = 1` for all 8 sampled
across 08-08 → 09-09). Prune deletes that key. It was never called on them.

Two things I ruled out:
- The `hasattr` guard at `publisher.py:320` — tested against the real
  `upstash_redis.Redis` / `upstash_vector.Index`; all three attributes present,
  so the block is not silently skipping.
- Prune is only reachable from `promote_generation()` (`publisher.py:288`).
  A run that stages but never promotes leaves its namespace with no cleanup
  path at all.

---

## Rate and runway

Recent generations are ~24,000 vectors each (up from ~3,500 before the ChatGPT
export integration, `5cb7b41`).

| Basis | Rate | Runway on 153,034 headroom |
|---|---:|---:|
| Calendar days 09-04 → 09-10 | 47,940/day | **3.2 days** |
| Mean over days with activity (5) | 67,115/day | **2.3 days** |

Call it **2–3 days**. I am not going to give a sharper number than the data
supports: 09-05 alone was 146,014 vectors across 8 generations, and 09-07/09-08
produced none, so the mean is unstable either way. Today alone added 72,582
across 3 generations — the highest-rate day in the window.

**At a plan upgrade's scale this still doesn't help.** Doubling the quota buys
roughly two more weeks at the current rate, then the same wall.

## What actually happens at the cap

Not an outage. `--stage` upserts start failing, promotion stops, and the
Cloudflare worker keeps serving the last good generation
(`cloudflare-mcp/mcp-server/src/sourceFirst.ts:449` queries only
`namespace: <current generation>`). So the symptom is **a frozen knowledge
base** — retrieval keeps answering, silently, from stale data. That pattern
already showed up on 08-12/08-13.

---

## Recommendation

**Order matters, and it's the opposite of what I first drafted.** A rebuild
cannot undo a deletion — it only adds one ~24k namespace. With 2–3 days of
runway, buy the headroom first, then fix the code without time pressure.

### 1. Delete the 58 orphaned namespaces — needs your approval

Recovers 416,136 vectors, taking the index from 76.8% → 13.8%. These are not
read by anything: the worker only ever queries the current generation.

Safety conditions I would apply:
- Re-read `sf:current_generation` and `sf:generation_history` immediately
  before deleting, and exclude everything in them.
- Exclude any namespace younger than ~1 hour — a candidate mid-verify is
  transiently live.
- Confirm no rebuild is in flight.
- Delete one namespace at a time, logging each.
- Also delete the matching `sf:<generation>:*` and `sf:manifest:<generation>`
  Redis keys, which prune would have removed.

### 2. Fix retention so this cannot recur

Reconcile against the real inventory rather than the truncated chain: list
namespaces from `/info`, keep `sf:current_generation` plus the retained
history, delete the rest. That closes the staged-but-never-promoted leak too.

### 3. Do not upgrade the plan

It buys ~2 weeks and fixes nothing.

---

## Excluded from the deletion set

**`(default)`, 18,248 vectors** — this is the legacy `ke_` index. The current
worker never queries it; the pre-source-first server at `mcp-server/` does
(`mcp-server/src/storage/vector.ts`, no namespace argument = default), and that
tree was last touched **2026-02-05**. I don't know whether it's still deployed
anywhere, so I left it alone. It's a separate decision and it isn't urgent —
it's 3.6% of the index.

## Redis: checked, healthy

I checked the paired Redis, since the same generations write keys there.
**176.0 MB used of a 3.0 GB limit (5.7%)**, 6,329,812 keys. Not a constraint.
The orphans' Redis keys are still there, but they are not the pressure point.

---

## One correction worth recording

`/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/.env`
lists `UPSTASH_REDIS_REST_URL` **twice** — line 10 is `knowledge-embeddings`,
line 75 is the **California law chatbot**. `python-dotenv` takes the *last*
occurrence, so any script using `dotenv_values()` on this file silently talks to
the wrong database. It bit this review: an intermediate check appeared to show
the serving pointer unset and the orphan manifest keys already deleted. Both
readings were against the wrong database and are discarded; every number above
is from `measured-raven-41051` (`knowledge-embeddings`), the same database
`ingestion/.env` gives the publisher. Arjun's stated convention is first-key-
wins, which is the opposite of what `dotenv_values` does.

---

*Files referenced:*
- `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/ingestion/source_first/publisher.py`
- `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/cloudflare-mcp/mcp-server/src/sourceFirst.ts`
- `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/.env`
