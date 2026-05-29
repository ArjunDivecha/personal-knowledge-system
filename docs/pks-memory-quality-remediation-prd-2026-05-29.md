# PKS Memory Quality Remediation — PRD

- **Status:** Draft for implementation
- **Author:** Generated from a white-box audit of the live system (Claude), 2026-05-29
- **Target repo:** `ArjunDivecha/personal-knowledge-system`
- **Implementer:** Claude Code
- **Related docs:** `README.md` (forgetting design + "Next" roadmap), `docs/pks-memory-upgrade-checklist.md`, `docs/pks-memory-upgrade-phase0-audit-2026-03-26.md`, `docs/testing-matrix.md`
- **Relationship to roadmap:** This executes the README "Next" items — *richer Dream replay logic beyond deterministic duplicate/contradiction heuristics*, *ingestion hardening and source-fusion improvements*, and *broader ranking coverage*. It is **compatible with and prerequisite to** Phase 7 (evidence-log / compiled-view separation): a corpus that is de-duplicated and correctly counted is far cheaper to migrate.

---

## 1. Summary

The live memory system has accreted faster than it consolidates, and its prioritization signals have collapsed. As of 2026-05-29 the store holds **4,265 active topics** (up from 2,516 on 2026-05-08) with **3,189 in Tier 1, 31 in Tier 2, 1,079 in Tier 3**, and **4,612 archived**. Three independent failures compound:

1. **Duplication / no entity resolution** — the same topic exists as many near-identical entries (e.g. 4× LoopPilot-architecture, 3× Karpathy-Loop, 4× Pattern-CNN).
2. **Degenerate salience** — hundreds of entries share an identical `salience_score` (0.3306, 0.3329, 0.2163…); the score does not discriminate.
3. **Collapsed tiering** — 75% of entries sit in Tier 1 (always-injected); Tier 2 has been **frozen at exactly 31 entries since at least 2026-05-08**, i.e. zero net promotions in three weeks.

All three trace to **one root cause** (Section 3). The user-visible consequence is the failure the README's opening anecdote describes: the system surfaces the wrong things and **cannot retrieve current flagship work** — a live search for the ASADO "Demographic Inflation Pressure (DIP)" factor returned zero DIP entries, only tangential items, every one at identical salience `0.2163`.

This PRD specifies a phased remediation that routes every mutation through the **existing Dream governance path** (`run_dream_proposal → grade_dream_proposal → apply_dream_proposal` with rollback). No schema redesign is required; all changes are compatible with the planned Phase 7 schema work.

---

## 2. Problem & Evidence (from audit)

All evidence below is from the live production system and the current `main` of the repo.

| # | Symptom (measured) | Mechanism in code |
|---|---|---|
| P1 | Duplicate clusters survive nightly Dream indefinitely | `getDuplicateFingerprint()` (`cloudflare-mcp/mcp-server/src/dream.ts`) keys de-dup on the **exact normalized `domain` string**. `buildReplayPlans()` groups only entries whose fingerprints are byte-identical after lowercasing/punctuation-stripping. LLM-written `domain` titles are never byte-identical across conversations, so semantically identical entries are never grouped. |
| P2 | `mention_count` is 1 for ~all entries | `mention_count` is only incremented when a merge unions `source_conversations` (`mergeCanonicalEntry`). Merges never fire (P1), so recurrence is never counted. |
| P3 | Salience degenerate (100s identical) | `compute_salience()` (`distillation/utils/salience.py`, mirrored in `salience.ts`): `raw = confidence · decay · combined_multiplier · freq_boost + retrieval_boost`. `freq_boost = min(1, ln(1+mention)/ln(21))` is a **constant ≈ 0.228** when `mention ≡ 1`. With `confidence` in 3 buckets, `decay ≈ 1` for recent/infinite-half-life entries, and `retrieval_boost ≈ 0` (P5), the formula collapses to a tiny discrete set of products. |
| P4 | Tier 1 holds 75%; Tier 2 frozen at 31 | Tier is assigned by `default_injection_tier(context_type)` — a static lookup in `shared/memory_policy.json`, not a function of salience. The distillation classifier over-assigns `active_project` / `professional_identity` / `explicit_save` (all → Tier 1). The only path to Tier 2 (`recurring_pattern`) is `promoteEntry`, gated by `isPromotionCandidate` which requires `context_type == task_query` **and** `mention_count ≥ threshold` **and** (`access_count > 0` or `>1 source conversation`) — all dead signals (P2, P5). Result: promotions ≈ 0, Tier 2 stuck at 31. |
| P5 | Reinforcement loop appears dead | README states reconsolidation-on-retrieval is live (Phase 4): "every meaningful retrieval … access count increments; last accessed updates." But every entry returned by live `search` shows `access_count: 0` and `last_accessed: null`, so `retrieval_boost` contributes 0. Either the write-on-retrieval path is not firing on the `search` route or it is not persisting to `entry_access:{id}` / `entry_last_accessed:{id}`. **Requires investigation in `cloudflare-mcp/mcp-server/src/index.ts` (`reconsolidateEntry`).** |
| P6 | Unbounded growth | Dream's archive path is doubly throttled: `isArchiveCandidate` only admits `task_query`/`passing_reference` entries (almost nothing qualifies), and the scheduled governed run is capped at **10 archives/night** (`archive_limit: 10`). Net intake (~80/day over the last 3 weeks) ≫ prune. Layer-2 quarantine/demote (`applyLayer2QuarantineAndDemote`) only fires below salience **0.15**, but the degenerate cluster sits at **0.33**, so the demotion safety valve cannot reach the Tier-1 glut. |
| P7 | Projects: 34/34 "active", several last touched 2024 | No project-aging rule; `status` is never transitioned off "active" by maintenance. |
| P8 | Validation is blind to quality | The validation ledger gates (`verify_memory_full_strict`, `check_overnight_dream`, `staging_e2e`) check **consistency** (Redis ↔ Vector ↔ thin-index counts match) and were last green **8 days stale**. None measures dedup rate, tier distribution, salience spread, or retrieval recall. Duplicates pass consistency trivially. |

### Trend evidence (precise)

| Date | Active topics | Tier 1 | Tier 2 | Tier 3 | Archived |
|---|---|---|---|---|---|
| 2026-05-08 (README) | 2,516 | 1,798 | **31** | 721 | 4,434 |
| 2026-05-29 (audit) | 4,265 | 3,189 | **31** | 1,079 | 4,612 |

Tier 2 is **identical (31)** across three weeks while everything else grew — the clearest possible signal that the promotion pathway is non-functional, not merely slow.

---

## 3. Root Cause

**There is no working entity resolution in the system.** Every ingestion run writes fresh entries with a slightly different `domain` title, `mention_count = 1`, and an over-eager `context_type`. De-duplication is *lexical* (exact normalized title) when it needs to be *semantic* — even though a 3072-dim embedding (`text-embedding-3-large`) already exists for every active entry in Upstash Vector and is never consulted for de-dup.

Everything else cascades from this single defect:

```
no semantic entity resolution
        │
        ├─► duplicate entries survive (P1)
        │
        ├─► mention_count pinned at 1 (P2)
        │        │
        │        ├─► freq_boost constant ─► salience degenerate (P3)
        │        └─► promotion never fires ─► Tier 2 frozen (P4)
        │
        ├─► classifier over-labels ─► Tier 1 glut (P4)
        │
        └─► prune can't keep pace + demotion valve unreachable ─► unbounded growth (P6)
```

The highest-leverage intervention, by a wide margin, is to **make de-duplication semantic** (Phase 1). It directly fixes P1, restores `mention_count` (P2) which un-flattens salience (P3) and unblocks promotion (P4), and shrinks the base.

---

## 4. Goals / Non-Goals

### Goals
- Restore semantic entity resolution (de-dup) using existing embeddings, gated through Dream governance.
- Un-flatten `salience_score` so it discriminates across the corpus.
- Rebalance tiers so Tier 1 reflects genuine priority and Tier 2 is populated.
- Bound active-store growth (prune keeps pace with intake).
- Repair the retrieval→reinforcement loop so `access_count` / `last_accessed` actually drive salience.
- Add quality gates so regressions are caught automatically.

### Non-Goals
- Changing the storage backend (Redis/Upstash Vector) or the embedding model.
- The Phase 7 evidence-log / compiled-view schema migration (tracked separately; this work is compatible with it).
- MCP surface / OAuth changes.
- UI work.

---

## 5. Success Metrics

Each maps to an audit test (T1–T9, Section 13) and becomes a guardrail gate (Phase 0).

| Metric | Current | Target |
|---|---|---|
| M1 Tier-1 share of active entries | ~75% | ≤ 25% |
| M2 Tier-2 share | ~0.7% (31) | ≥ 10% and rising over time |
| M3 Max share of entries sharing one `salience_score` value | 100s identical | ≤ 2% of active entries at any single value |
| M4 Duplicate clusters (entries that should be one canonical) | many | reduced ≥ 80% on the labeled fixture; Dream proposes > 0 merges when dupes exist |
| M5 Net active-store growth over a rolling 14-day window | ≈ +80/day | ≤ 0 (prune ≥ intake) |
| M6 Recall@5 on a fixed probe set of current projects (DIP factor, InMobi, T2/LoopPilot, etc.) | DIP = 0 | ≥ 0.8 |
| M7 Access-signal coverage: fraction of retrieved entries with `access_count > 0` after retrieval | ~0 | ≈ 1.0 for entries retrieved in the last N days |

---

## 6. Phase 0 — Measurement & Guardrails (prerequisite)

You cannot fix what you cannot measure, and the system currently has **no quality metric**. Build this first so every later phase has a before/after.

**Requirements**
- **R0.1** Add a read-only audit script (`scripts/audit_memory_quality.py`, reusing the existing storage clients in `distillation/`) that computes and prints: tier distribution; a `salience_score` histogram plus the top-N most-shared exact values and the count at each; an estimated duplicate-cluster count (via embedding nearest-neighbours over Upstash Vector, cosine ≥ a configurable threshold); active-store growth from the Dream run ledger; Recall@k against a fixed query set; and access-signal coverage. Output JSON to `scripts/reports/` to match the existing report convention.
- **R0.2** Add a fixed **probe set** file (`tests/fixtures/recall_probes.json`) of ~20 current-work queries with the entry IDs/domains that *should* surface (DIP factor, InMobi advisory, T2 IC-saturation, GDELT→ASADO, etc.). This is the M6 oracle and the basis for the T9 recall regression.
- **R0.3** Add a validation-ledger gate `verify_memory_quality` (alongside `verify_memory_consistency` / `check_overnight_dream`) that **fails** when M1 > threshold or M4 > threshold. Wire a `make audit-memory-quality` target into the root `Makefile` command surface.

**Acceptance**
- `make audit-memory-quality` prints all seven metrics and writes a JSON report.
- The gate appears in the validation ledger and can fail on current data (it should fail M1 today).

---

## 7. Phase 1 — Semantic Entity Resolution (keystone)

**Current behavior.** `getDuplicateFingerprint()` returns the normalized `domain` string; `buildReplayPlans()` groups entries by exact fingerprint equality; `entriesAreCompatibleDuplicates()` then applies a token-similarity floor (≥ 0.3) and an opposing-marker veto; canonical chosen by `compareCanonicalPriority`; merge performed by `mergeCanonicalEntry` (which already unions `source_conversations`, `key_insights`, `positions`, `evolution`, etc., and recomputes `mention_count` from the unioned `source_conversations` length). The bright-line vs borderline split (`isDuplicateMergeBorderline`, `DREAM_OPUS_MODE`, judge queue) already exists.

**Target behavior.** Generate merge candidates by **embedding similarity**, not title equality, while preserving every existing safety gate.

**Requirements**
- **R1.1 Semantic candidate generation.** Add a candidate-generation step in the Dream replay phase that, for each active entry, queries Upstash Vector for nearest neighbours **of the same `type`** with cosine ≥ `COSINE_DUP_THRESHOLD`, top-k = `DEDUP_NEIGHBOR_K`. Use the existing stored embeddings; **do not re-embed**. (The retrieval path already issues vector queries, so the capability exists.)
- **R1.2 Keep the lexical pre-pass.** Exact-fingerprint matches remain a valid, cheap signal and a fast path; retain them and union with the semantic candidates.
- **R1.3 Grouping.** Build candidate groups via union-find / connected components over the neighbour graph above threshold (so A~B~C collapse into one group).
- **R1.4 Confirmation gate (reuse existing).** For every candidate pair, still require `entriesAreCompatibleDuplicates()` to pass: the **opposing-marker veto** routes contradictory pairs to `mark_contested` (existing contradiction path) instead of merging; the narrative token-similarity floor remains a confirmation check. A high embedding score must **not** override a contradiction.
- **R1.5 Canonical + merge (reuse existing).** Use `compareCanonicalPriority` to pick canonical and `mergeCanonicalEntry` to merge. No change to merge mechanics needed.
- **R1.6 Judge-gate semantic-only matches.** Treat any match that is **semantic-only** (no exact-fingerprint agreement) as **borderline** by default, so it enqueues to the judge / requires operator confirmation rather than auto-applying. Bright-line auto-apply remains limited to high-confidence matches with zero access on all duplicates. This keeps blast radius controlled during ramp; relax later once precision is validated.
- **R1.7 `mention_count` backfill.** Add a one-time backfill (`scripts/backfill_mention_count.py`) that recomputes `mention_count` from unioned `source_conversations` for entries that already have multiple sources but were never merged. Run after the first dedup pass.
- **R1.8 Policy constants** (in `shared/memory_policy.json`, the shared source of truth): `COSINE_DUP_THRESHOLD` (start **0.86**, tune on fixture), `COSINE_CONTEST_BAND` (e.g. 0.80–0.86 → apply the contradiction check more strictly before grouping), `DEDUP_NEIGHBOR_K` (e.g. 10), `SEMANTIC_MERGE_REQUIRES_JUDGE` (bool, default true).

**Acceptance**
- On a labeled fixture (`tests/fixtures/duplicate_clusters.json` seeded with the known LoopPilot/Karpathy/Pattern clusters), candidate generation groups each cluster; after governed apply each collapses to **one** canonical with `mention_count = #merged` and unioned insights/sources.
- No merge occurs across an opposing-marker pair; such pairs become `contested`.
- A dry-run proposal (`run_dream_proposal`) on current production data reports **> 0** `duplicate_merge` operations.
- Rollback drill: `apply_dream_proposal` then `rollback_dream_apply` restores all merged entries (existing rollback already supports `duplicate_merge`).

**Tests**
- Unit: candidate grouping (union-find), the contradiction veto, canonical selection.
- Integration: full `run_dream_proposal → grade_dream_proposal → apply_dream_proposal → rollback_dream_apply` on the staging fixture via the existing `staging_e2e` harness / `make staging-smoke`.

**Safety:** governed path only; dry-run first; judge-gated for semantic-only matches; existing caps and `recordDestructiveAction` tripwire apply.

---

## 8. Phase 2 — Salience De-degeneration

Depends on Phase 1 (which restores `mention_count` variance).

**Requirements**
- **R2.1** Re-run the salience backfill after Phase 1 so `freq_boost` spreads across the now-varied `mention_count`.
- **R2.2** Do not rely on `retrieval_boost` until Phase 5 confirms access signals populate.
- **R2.3** Add at least one **continuous** component so salience is not a discrete lookup even at `mention_count = 1`. Recommended levers, all already in the schema and cheap to compute: (a) source breadth = distinct `source_conversations`; (b) graph centrality = count of inbound/related `related_knowledge` links; (c) `key_insights` count. Pick one primary lever (recommend source-breadth) and specify its weight in `memory_policy.json`. Keep the existing `max_combined_salience_multiplier` cap.
- **R2.4** Recalibrate every salience-keyed threshold against the **new** distribution rather than leaving absolute constants: `dream_thresholds.archive_candidate_salience`, `dream_thresholds.decay_candidate_salience`, the `classifyBucket` 0.35 cutoff, and `LAYER2_QUARANTINE_SALIENCE_THRESHOLD` (0.15). Prefer expressing these as **percentiles of the live distribution** computed at run time, or re-pick constants after measuring the post-Phase-1 histogram.

**Acceptance**
- M3 met: no single `salience_score` value shared by > 2% of active entries.
- The `classifyBucket` buckets (stable/active/weak/decay) are non-degenerate (no bucket holds ~everything).

---

## 9. Phase 3 — Tier Assignment by Salience, not static `context_type`

**Current.** Tier = `default_injection_tier(context_type)` static map → 75% Tier 1.

**Requirements**
- **R3.1 (recommended, lower-risk).** Compute tier from **salience percentile** at thin-index rebuild time: top X% → Tier 1, next Y% → Tier 2, remainder → Tier 3 (X, Y in `memory_policy.json`). Keep `context_type` as a **floor**: protected identity types (`professional_identity`, `stated_preference`) never fall below Tier 2 regardless of transient salience, so durable identity is not demoted by a quiet month.
- **R3.2 (complementary).** Tighten the distillation classifier (`distillation/pipeline/index.py` and its prompt) so it stops defaulting to `active_project`/`professional_identity`. Most ingested items are not core identity. Lower priority than R3.1 and touches the Python ingestion side.
- **R3.3 Migration.** One-time governed re-tier pass (caps + rollback), or fold into the next Dream run.

**Acceptance**
- M1 (Tier-1 ≤ 25%) and M2 (Tier-2 ≥ 10%) met.
- Identity-floor entries verified still Tier 1/2.
- Dry-run + rollback verified before the live re-tier.

---

## 10. Phase 4 — Throughput (bound growth)

**Requirements**
- **R4.1** Parameterize and raise the nightly archive cap; ramp **10 → ~100** under the governed+graded+rollback path, monitored by the Phase 0 metrics and the `recordDestructiveAction` tripwire.
- **R4.2** Broaden `isArchiveCandidate`: admit any low-salience (below the Phase-2 percentile threshold), zero-access, single-source entry regardless of `context_type`, **except** identity-floor types. Edit the predicate in `dream.ts`.
- **R4.3** Lift `LAYER2_QUARANTINE_SALIENCE_THRESHOLD` into the live distribution (percentile-based) so quarantine/demotion can act on the bulk of entries; keep the 3-night quarantine / 10-night demote streak logic.
- **R4.4** Track M5 (net growth ≤ 0 over 14 days).

**Acceptance**
- Over a 14-day window, prune ≥ intake (M5).
- Layer-2 quarantine and tier demotions actually fire (> 0 in run records).
- Ramp is gradual; each step independently reversible.

---

## 11. Phase 5 — Reinforcement Wiring (retrieval → salience)

**Evidence.** README claims reconsolidation-on-retrieval is live, but audited `search` results show `access_count: 0` / `last_accessed: null`, so `retrieval_boost` is dead in practice.

**Requirements**
- **R5.1** Investigate `cloudflare-mcp/mcp-server/src/index.ts` (`reconsolidateEntry`) and the `search` / `get_context` / `get_deep` routes: determine whether each served retrieval writes `entry_access:{id}` and `entry_last_accessed:{id}`, and whether `search` (the highest-volume path) is wired at all. *(The implementer must read index.ts; this PRD does not assume the exact bug.)*
- **R5.2** Ensure every meaningful retrieval increments `access_count`, sets `last_accessed`, and lifts `injection_quarantine` (existing `liftQuarantineMetadata`).
- **R5.3** Confirm the next Dream/rebuild `compute_salience` picks up the new signals (so `retrieval_boost` becomes live).

**Acceptance**
- M7: after a retrieval, the entry's `access_count > 0` and `last_accessed` is set; salience reflects `retrieval_boost`.

---

## 12. Phase 6 — Project Lifecycle + Quality Gates

**Requirements**
- **R6.1** Project aging: transition `status` active → dormant after N days untouched (policy-configurable); honor phase text that already says "complete". Resolve the 34/34-active, several-stale-since-2024 condition.
- **R6.2** Promote the Phase 0 `verify_memory_quality` gate to a hard nightly gate with thresholds tied to M1–M7.

**Acceptance**
- Stale "active" projects reclassified; quality gate enforced nightly.

---

## 13. Test Plan

Adopt the audit taxonomy as a regression suite, layered onto the existing fixture / staging-E2E / canary structure documented in `docs/testing-matrix.md`.

| ID | Test | Becomes |
|---|---|---|
| T1 | Tier distribution ≤ thresholds | `verify_memory_quality` gate (M1/M2) |
| T2 | Salience spread (no value > 2%) | audit metric (M3) |
| T3 | Dedup efficacy (merges fire; clusters reduced) | fixture + staging assertion (M4) |
| T4 | Growth bounded | rolling metric from Dream ledger (M5) |
| T5 | Recall@k on probe set | `recall_probes.json` regression (M6) |
| T6 | Access-signal coverage | audit metric (M7) |
| T7 | Project lifecycle | gate on stale-active projects |
| T8 | Validation freshness + quality coverage | ledger freshness check |
| T9 | **Fidelity (source → entry)** | **separate task, not yet run.** Requires the raw conversation exports (Claude/ChatGPT in Dropbox). Sample N source conversations, trace to resulting entries, measure recall (content that should be an entry but isn't) and precision (entries asserting content not in source). Specify before Phase 7. |

Reuse: `make worker-test`, `make verify-memory-full`, `make staging-smoke`, `make dream-live-canary`.

---

## 14. Rollout & Safety

- **Every** mutation goes through `run_dream_proposal → grade_dream_proposal → apply_dream_proposal`, with `rollback_dream_apply` proven each phase.
- Dry-run first on every phase; inspect the proposal before any live apply.
- Ramp caps gradually (Phase 4); never exceed a per-night destructive cap; monitor `recordDestructiveAction`.
- Each phase is independently revertible and independently shippable.
- No schema change in Phases 0–6 — fully compatible with the planned Phase 7 evidence-log / compiled-view migration.

---

## 15. File & Change Map (for Claude Code)

| File | Phases | Change |
|---|---|---|
| `shared/memory_policy.json` | 1–4 | New constants: dedup thresholds, salience continuous-lever weight, tier percentiles, recalibrated dream/Layer-2 thresholds. **Single source of truth — keep Python and TS in sync.** |
| `cloudflare-mcp/mcp-server/src/dream.ts` | 1,3,4 | Semantic candidate generation (new fn); modify `buildReplayPlans`, keep `entriesAreCompatibleDuplicates` veto; widen `isArchiveCandidate`; tier-by-percentile in `rebuildThinIndex*`; raise scheduled archive cap; lift Layer-2 threshold. |
| `cloudflare-mcp/mcp-server/src/salience.ts` + `distillation/utils/salience.py` | 2,3 | `compute_salience` continuous lever; tier-by-percentile helper. **Two implementations must stay in lockstep** — constants live in `memory_policy.json`. |
| `cloudflare-mcp/mcp-server/src/index.ts` | 5 | Audit + repair retrieval→access write path. *(Read first.)* |
| `distillation/pipeline/index.py` | 3 (optional) | Tighten classifier so it stops over-assigning Tier-1 context types. |
| `scripts/` | 0,1 | `audit_memory_quality.py`, `verify_memory_quality` gate, `backfill_mention_count.py`, salience/re-tier backfills. |
| `tests/fixtures/` + `tests/python/` | 0,1,3 | `duplicate_clusters.json`, `recall_probes.json`; regression tests; staging assertions. |
| `Makefile` | 0 | `audit-memory-quality` target. |

---

## 16. Open Questions / Operator Decisions

1. `COSINE_DUP_THRESHOLD` starting value (proposed 0.86) — tune on the labeled fixture; what false-merge rate is acceptable?
2. Tier percentile splits X/Y for Phase 3 (e.g. top 15% Tier 1, next 25% Tier 2?).
3. Identity-floor list — which `context_type`s are protected from demotion/archive (proposed: `professional_identity`, `stated_preference`).
4. Archive-cap ramp schedule (10 → 100 over how many nights?).
5. Phase 3 Option B (classifier tightening) now or deferred?
6. Should bright-line auto-apply of semantic merges ever be enabled, or do all semantic merges stay judge/operator-gated indefinitely?

---

## 17. Out of Scope

- Storage backend or embedding-model changes.
- Phase 7 evidence-log / compiled-view schema migration (separate; this PRD is a prerequisite enabler).
- MCP surface / OAuth / UI changes.

---

## 18. Suggested Implementation Order

1. **Phase 0** (measurement) — ship first; establishes baseline and gates.
2. **Phase 1** (semantic dedup) — the keystone; largest single quality gain.
3. **Phase 2** (salience) — mechanical follow-on once `mention_count` is real.
4. **Phase 3** (tiering) — rebalances injection.
5. **Phase 4** (throughput) — bounds growth.
6. **Phase 5** (reinforcement) — closes the use-matters loop.
7. **Phase 6** (lifecycle + gates) — locks in the wins.

Phases 0–1 deliver most of the user-visible improvement (retrieval stops surfacing duplicate noise and starts finding current work). Ship them, re-measure against Section 5, then proceed.
