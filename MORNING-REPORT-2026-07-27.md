<!--
=============================================================================
FILE: MORNING-REPORT-2026-07-27.md
INPUT FILES: none (references live system + CI artifacts, all cited inline).
OUTPUT FILES: none. This is a status report for Arjun.
=============================================================================
-->

# Morning report — 2026-07-27: nightly semantic maintenance stall, fixed

**For Arjun. Written overnight (Claude, Opus 5).** You asked me to write up the
change with Divecha, implement it, test it with the morning run, and report.

---

## Bottom line

The nightly semantic maintenance had failed **3 of the last 4 nights** (07-25,
07-26, and it was stalling). Root cause was **not** what my stale
`NIGHTLY-STALL.md` note said — Sol had already fixed those two bugs. The actual
cause was **one orphaned `prepared` outbox entry** from a 07-25 crash that
poisoned the global safety barrier every night, forcing a rollback of each
night's good merges.

I diagnosed it against the live system, wrote a Divecha contract, built a
guarded reconciler (runner computed **DIVECHA_GREEN**, 5/5 behaviors + scope),
cleared the live orphan (verified safe: the merge never applied, both entries
untouched), and **verify-memory-full is 0 issues**. A full production validation
run is confirming end-to-end — result in the "Validation" section below.

**Nothing was corrupted at any point.** The old failures were fail-*closed*
(the barrier refusing to proceed), the opposite of the outage I caused two weeks
ago.

---

## What I found (and how it differs from my earlier note)

My `NIGHTLY-STALL.md` (07-18) filed two bugs: a cosine score-scale mismatch and
an "all-held night is fatal" policy. **Sol already fixed both** — I verified in
the current code (`semanticMaintenance.ts` now normalizes cosine to Upstash's
`(1+cos)/2` scale; the all-held site now warns instead of raising). So that note
is stale, and I did not re-fix already-fixed bugs.

The **current** failure (GH run `30196498382`, 07-26) was different:
`cohort_verification_failed`. The maintenance applied 5 merges, then the barrier
scanned **all** `maintenance:outbox:*` keys, found **one** stuck in `prepared`,
declared the state unsafe, and **rolled back the 5 good merges**. Same thing
07-25. The `prepared` orphan (task `nsm-20260725T091529Z-…`, merge
`ke_b952a6d7535b ← ke_74f824f30864`) had sat since a 07-25 crash. Nothing
reconciled it, so it re-poisoned the barrier every single night.

Two facts made it worse than a simple orphan:
- The `maintenance:task:<id>` record was **absent** — only the outbox journal
  survived — so the existing `/ops/maintenance/rollback` endpoint returns
  **400 `maintenance_task_not_found`** and literally cannot clear it.
- I verified the merge **never applied**: both entries still active, not
  archived, at their pre-merge revisions. So it was pure stale bookkeeping, not
  data drift.

---

## What I changed

**Divecha contract `PKS-MAINT-ORPHAN-RECONCILE-001`** (behavior-first, v3) →
runner computed **DIVECHA_GREEN** (5/5 behavior checks + git scope clean; I did
not self-certify). Python suite **396/396**.

**A run-start orphan reconciler** in `scripts/nightly_semantic_maintenance.py`
(Python-only — no Worker code change). At run start, before the barrier can trip
on them, it clears stale `prepared` orphans — but only after an **airtight
never-applied proof**: every entry must be present, at its expected revision,
and **not archived** (an applied merge bumps the canonical revision and archives
the duplicate, so either mismatch means "maybe partially applied → do not
touch, flag for review"). Two paths for a proven-safe orphan:
- **task record exists** → roll back via the existing endpoint (Worker restores
  snapshots + marks it).
- **task record missing** (the 07-25 case) → flip the outbox journal to
  `rolled_back` directly. Bookkeeping-only status write, never an entry.

It never raises: a skipped orphan is strictly safer than a corrupted entry.

---

## Evidence (verified on production)

- Orphan cleared: ran the reconciler against prod → it terminalized the
  task-less orphan; **prepared orphans 1 → 0**.
- The two entries are **untouched**: both still `active`, `archived=false`, same
  revisions (`ke_b952a6d7535b` rev 0, `ke_74f824f30864` unchanged). Only the
  outbox marker moved `prepared → rolled_back`.
- **`make verify-memory-full` → 0 issues** (11,995 entries) after the cleanup.
- Backlog trend when NOT stalled (for context): 07-23 applied 36, 07-24 applied
  23; cluster count drifting down (860 → 826). The drain works at ~20–36
  merges/night; the orphan had halted it since 07-25.

### Validation run — SUCCESS

A full live maintenance run (GH `30251870904`, 08:57→09:24 UTC, workflow_dispatch
mode=live) confirmed end-to-end:

```
conclusion: success
applied: 11 merges   held: 289   attempted: 300   rollbacks: 0
barriers: 3, ALL PASSED   (last barrier unsafe_outbox: []  consistency: True)
reconciled: []   (nothing to reconcile — the orphan was already cleared)
pre_audit clusters: 798  (down from 826 → 860 the prior good nights)
```

Both halves of the fix are proven:
1. **The reconciler clears a real orphan** — proven earlier this session against
   production: it terminalized the task-less 07-25 orphan (`reconciled:
   ['nsm-20260725T091529Z-…']`), 1 → 0 prepared, entries untouched.
2. **From the cleaned state the nightly succeeds** — this run: barrier passes
   (no `prepared` poison), 11 merges applied and survived verification, **0
   rollbacks**. The drain is moving again (11 real duplicates merged; cluster
   count 826 → 798).

Post-run, independently: **verify-memory-full = 0 issues** (11,975 entries), **0
prepared orphans**. Going forward the reconciler runs first every night and will
auto-clear any new orphan before the barrier can trip on it.

---

## What I deliberately did NOT do (scope discipline)

I scoped this tightly because I broke this exact subsystem two weeks ago.

- **No Worker code change, no deploy.** The fix is Python-only and reuses the
  existing rollback endpoint where it can.
- **One flagged deviation for your/Sol's review:** the *direct outbox
  terminalize* path (for task-less orphans) is the single place the reconciler
  writes maintenance bookkeeping state directly rather than through the Worker.
  It is airtight-guarded (only ever touches a proven never-applied orphan), and
  it is the **only** way to clear a task-less orphan without a Worker change.
  The cleaner long-term fix is a Worker-side "reconcile-from-outbox" endpoint;
  I did not add one (Sol owns the Worker maintenance code).
- **Deferred, per the GPT vet on the OptMem ideas** (all noted, none done
  tonight): the residual `candidate_component_incomplete` hold-rate (an
  efficiency issue — most candidates are safely held, not a failure), the
  LLM-adjudicator, and the full incremental state-machine.

---

## Residual risk & recommended next step

- **Recurrence:** a task-less orphan only appears if a run crashes in the narrow
  window after writing the outbox journal but before the task record. Rare, but
  the reconciler now self-heals it each night instead of failing forever.
- **The hold-rate** (~90% of candidates held) is the real throughput ceiling
  now — the drain creeps rather than sprints. That's the next lever, and it's
  Sol's system to tune. Not urgent (held = safe).
- **Recommended:** let Sol review the direct-terminalize deviation and decide
  whether to promote it to a Worker endpoint; separately, look at the hold-rate
  if you want the backlog to drain faster than ~25 merges/night.

Artifacts: contract `contracts/maintenance-orphan-reconcile.spec.md`;
commits on `main` (`b1bb1b9`, `871ab79`); this session cleared the live orphan.
