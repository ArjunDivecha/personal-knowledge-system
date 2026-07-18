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
similarity-score scale mismatch). The other is why an all-held night is a hard
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

The first version of this report attributed the holds to transitive clustering.
Live task records and direct vector comparisons disproved that explanation. The
verified root cause is the score-scale mismatch in §2.

---

## 2. BUG 1 (root cause of the holds): the same threshold on two score scales

`candidate_component_incomplete` is emitted by the Worker when its local
revalidation cannot reproduce the planner edge. The original diagnosis above was
wrong about why: both sides accept transitive connected components over the submitted
members. The mismatch is the numerical meaning of `0.95`.

```ts
const component = connectedComponent(entries, threshold);
if (component.length !== ids.length || component.some((id) => !ids.includes(id))) {
    return { ok: false, reason: "candidate_component_incomplete", component };
}
```

- **Planner:** compares Upstash Vector's COSINE index `score` to `0.95`. Upstash maps
  raw cosine from `[-1, 1]` to `[0, 1]` as `(1 + cosine) / 2`.
- **Worker:** fetches the vectors, computes raw cosine in `semanticMaintenance.ts`,
  and compares that raw value to the same `0.95` threshold.

Direct production evidence from the first failed pair:

```text
candidate: ke_001ad7cc0791 ↔ ke_0b7e608cc6a3
Upstash query score: 0.9565817  (planner accepts)
raw vector cosine:   0.9131645  (Worker rejects)
```

Across all 100 held pairs, **100/100** had normalized Upstash score ≥0.95, while
only **2/100** had raw cosine ≥0.95. Durable Worker task records showed 98 holds
with a singleton `component`, confirming the local edge check as the dominant
branch; two were held later by the omitted-current-neighbour check.

The fix is deterministic: use the Upstash COSINE score scale for every local
planner/Worker comparison, while retaining the Worker's current-component,
conservation, protected-type, CAS, and rollback authority.

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
