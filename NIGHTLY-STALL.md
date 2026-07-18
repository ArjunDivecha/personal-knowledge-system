# NIGHTLY-STALL.md — the nightly semantic maintenance is stalling (for Sol)

**From:** Claude (Opus 4.8), 2026-07-18. **For:** GPT-5.6-sol, who built the
queue-based `nightly_semantic_maintenance` system after my `IBROKEIT.md` handoff.

**This is your system, not mine — I'm reporting, not fixing.** Arjun asked me to
check last night's run; this is what I found. Everything below is verified against
the live system and the CI artifacts, not inferred.

**INPUT FILES (referenced, absolute):**
- `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/scripts/nightly_semantic_maintenance.py`
- `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/cloudflare-mcp/mcp-server/src/semanticMaintenance.ts`
- `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/scripts/audit_memory_quality.py`
- `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/.github/workflows/nightly-semantic-maintenance.yml`

**OUTPUT FILES:** none. Design/bug-report document.

---

## 0. TL;DR

Last night's `Nightly Semantic Maintenance` run (GH Actions run `29569811844`,
2026-07-17) **FAILED**. It failed *safely* — `verify-memory-consistency: 0 issues`
before and after, zero rollbacks, nothing corrupted. But it is **progressively
stalling**, and last night it hit zero throughput and errored out.

The backlog is barely draining, and on the current trajectory it stops entirely.

**There are two separate bugs.** One is why candidates get held (a real
clustering-definition mismatch). The other is why an all-held night is a hard
FAILURE instead of a safe no-op (a script policy choice). Fix both.

---

## 1. Evidence — the three-night trend

| night (run_date) | GH status | applied | held | rollbacks | verify |
|---|---|---|---|---|---|
| 2026-07-15 | success | 5 | 33 | 0 | 0 issues |
| 2026-07-16 | success | 1 | 99 | 0 | 0 issues |
| **2026-07-17** | **FAILURE** | **0** | **100** | 0 | 0 issues |

`applied` collapses 5 → 1 → 0; `held` climbs 33 → 99 → 100. Last night **100/100
candidates held**, so `scripts/nightly_semantic_maintenance.py:482`
(`raise RuntimeError("semantic_maintenance_stalled:no_candidate_applied")`) fired
and the job exited 1.

Every one of the 100 held tasks had the same reason: **`candidate_component_incomplete`**.

Backlog is essentially not moving: ~6 merges applied across three nights against a
backlog of **~1,319 clusters / 5,121 entries (≈42% of the corpus)** — measured by
last night's own pre-audit. Live corpus right now: **12,167 entries, verify = 0
issues** (healthy).

This is exactly the failure the council flagged in `IBROKEIT.md` §6: *"merging
creates new vector geometries… not one-and-done."* The easy clusters merged on
nights 1–2; what remains is the geometry your two clustering definitions disagree
about (see §2), so the held-rate climbs to 100%.

---

## 2. BUG 1 (root cause of the holds): two different definitions of "a cluster"

`candidate_component_incomplete` is emitted by the Worker at
`cloudflare-mcp/mcp-server/src/semanticMaintenance.ts:100`. The Worker re-validates
every planner submission by recomputing the connected component itself and
demanding an **exact** match:

```ts
const component = connectedComponent(entries, threshold);
if (component.length !== ids.length || component.some((id) => !ids.includes(id))) {
    return { ok: false, reason: "candidate_component_incomplete", component };
}
```

The problem is that the planner and the Worker compute "the component" **two
different ways**, and they legitimately disagree:

- **Planner** (`scripts/audit_memory_quality.py`, consumed via
  `load_audit_clusters` → `select_candidate_clusters`): builds a **top-k
  nearest-neighbour graph over the WHOLE corpus** and takes connected components.
  So entries A and B land in the same cluster if there's *any chain* of ≥0.95
  edges connecting them — e.g. `A~C ≥ 0.95` and `C~B ≥ 0.95`, even if `A~B < 0.95`
  directly.

- **Worker** (`semanticMaintenance.ts` `connectedComponent`): computes **all-pairs
  cosine among ONLY the submitted candidate ids** (the 2–6 members), edges ≥ 0.95.
  It never sees the bridging member C (it wasn't submitted).

**Consequence:** for any cluster held together by a *chain* rather than direct
pairwise edges, the Worker computes `{A}` and `{B}` as two separate singletons,
gets `component.length (1) != ids.length (2)`, and holds it. **By construction,
chain-connected clusters can never be applied.** As nights 1–2 consume the
directly-connected clusters, only chain-connected ones remain → held-rate → 100%.
The trend in §1 is the signature of exactly this.

(This is the "two implementations of *what is a duplicate* that drift" risk the
council named. It didn't drift by accident — the two definitions were never the
same.)

### Fix options (your design call — I'm not choosing for you)
1. **Make the planner submit only Worker-complete components.** Before submitting,
   re-cluster each candidate set under the *Worker's* rule (all-pairs ≥ threshold
   among just those ids) and split any set that isn't a single clique/component.
   Only submit sets the Worker will accept. Cheapest, keeps the Worker as sole
   authority, no Worker change.
2. **Give the Worker the bridge.** Submit the chain including bridging members so
   the Worker's all-pairs recompute reproduces the same component. Risk: pulls more
   members per merge, and the bridge member may itself bridge to yet more — you can
   re-grow the mega-cluster the 0.95 threshold exists to prevent. Bound it hard.
3. **Relax the Worker's exact-match to subset-safe.** Accept a submission if it is
   a connected sub-component under the Worker's rule (every submitted id reachable
   from every other via ≥threshold edges *among the submitted set*), rather than
   demanding it equals the planner's full component. Changes the safety contract —
   think carefully; the exact-match is currently what stops the planner over-reaching.

⚠️ Whatever you pick, keep the invariant from `IBROKEIT.md` §7: **the Worker stays
the duplicate authority and the conservation/protected-type gates stay in the apply
path.** Don't move merge authority into Python to dodge this.

---

## 3. BUG 2 (why it's a hard failure): an all-held night should not exit 1

`scripts/nightly_semantic_maintenance.py:482`:
```python
if clusters and applied == 0:
    raise RuntimeError("semantic_maintenance_stalled:no_candidate_applied")
```

A night where **every** candidate is *held* is a **safe, expected** outcome — the
Worker refused to apply anything, the corpus is untouched, verify is clean. Right
now that raises and the GH job goes red, which reads as an outage and will page /
alarm every night once §2 bites. It also means the final no-op barrier / post-audit
after line 482 never runs.

At minimum, distinguish **"all held, zero applied, verify still clean"** (safe:
warn, exit 0, still run the post-audit and persist state) from a genuine fault
(apply error, verification failure, rollback). A stall that makes *no progress*
across N consecutive nights is worth alerting on — but a single all-held night is
not a failure, and treating it as one hides the real signal (§2) behind a generic
red X.

---

## 4. How to reproduce / inspect

```bash
# last night's failed run + its evidence artifact
gh run view 29569811844
gh run download 29569811844 -D /tmp/nsm
python3 -c "import json,collections; d=json.load(open([f for f in __import__('glob').glob('/tmp/nsm/**/nightly_semantic_maintenance_*.json',recursive=True)][0])); print(d['status'], d['applied_count'], d['held_count']); print(collections.Counter(t['reason'] for t in d['tasks']))"
# → failed 0 100   Counter({'candidate_component_incomplete': 100})

# current backlog scope (uncapped — a capped scan silently under-reports)
distillation/venv/bin/python scripts/audit_memory_quality.py --skip-recall --skip-temporal --max-dup-queries 13000
```

Credentials: `ingestion/.env` (NOT repo-root `.env` — it has duplicate
`UPSTASH_REDIS_REST_URL` keys and resolves to the wrong instance; see
`IBROKEIT.md` §7).

---

## 5. What is NOT wrong (so you don't chase ghosts)

- **Not corruption.** verify-memory-consistency = 0 issues, live, right now. Your
  fail-closed design worked — this is the opposite of the outage I caused.
- **Not the old Dream cron.** `DREAM_QUEUE_MODE=live` correctly makes the Cloudflare
  `scheduled()` handler a no-op; `dream:last_run` being stuck at 07-14 is expected,
  not a bug.
- **Not rollbacks.** Zero rollbacks all three nights. Nothing was applied then undone.

The system is *safe*. It just isn't *making progress*, and it's shouting FAILURE
about a safe state. Fix §2 (throughput) and §3 (don't cry wolf).
