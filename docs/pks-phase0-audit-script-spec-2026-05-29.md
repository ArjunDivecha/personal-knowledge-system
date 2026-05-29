# Phase 0 — Memory Quality Audit Script & Guardrails: Implementation Spec

- **Status:** Ready for implementation
- **Author:** Claude, 2026-05-29
- **Parent PRD:** `docs/pks-memory-quality-remediation-prd-2026-05-29.md` (Section 6, R0.1–R0.3)
- **Implementer:** Claude Code
- **Scope:** Read-only measurement + a quality gate. **No mutations of any kind** — no `set`, no vector `upsert`, no archive/merge. This phase only observes.

---

## 0. Why this ships first

The system currently has **no quality metric** — the validation ledger only checks Redis ↔ Vector ↔ thin-index *consistency*, which duplicates and bloat pass trivially. Every later phase (dedup, salience, tiering, throughput) needs a before/after baseline and a regression guard. Phase 0 builds exactly that and nothing more.

---

## 1. Deliverables

1. `scripts/audit_memory_quality.py` — read-only metrics collector.
2. `tests/fixtures/recall_probes.json` — fixed recall oracle (the M6 probe set).
3. `verify_memory_quality` — a validation-ledger gate.
4. `make audit-memory-quality` (and `make verify-memory-quality`) — `Makefile` targets.
5. Unit tests for the pure logic (clustering + metric calculators).

---

## 2. `scripts/audit_memory_quality.py`

### 2.1 Inputs & reuse
- Reuse the existing storage clients used elsewhere in `distillation/` (Redis + Upstash Vector). Do **not** introduce new connection logic.
- Mirror the Dream scanning pattern from `cloudflare-mcp/mcp-server/src/dream.ts`: `SCAN` with a fixed `count` (≈200) and **batched `MGET`** (batch ≈25). The store is ~4–9k entries; the script must complete in a few minutes and never load the whole keyspace into one request.
- Active entries = `knowledge:*` and `project:*` where `metadata.archived !== true`.

### 2.2 Metrics (exact definitions)

**M1 — Tier distribution.** Count and share of `metadata.injection_tier ∈ {1,2,3}` over active entries. Report counts + percentages.

**M2 — Tier-2 share.** The Tier-2 percentage from M1, surfaced separately (it is the frozen-at-31 signal).

**M3 — Salience degeneracy.**
- Histogram of `metadata.salience_score` over active entries (bin width 0.01).
- The **top-N (N=15) most frequent exact `salience_score` values**, each with its count and share of active entries.
- `max_single_value_share` = the largest such share (this is the M3 metric vs the ≤2% target).

**M4 — Duplicate-cluster estimate (read-only).**
- For each active entry, query Upstash Vector for nearest neighbours **of the same `type`**, `topK = DEDUP_NEIGHBOR_K` (≈10), keeping neighbours with cosine ≥ `COSINE_DUP_THRESHOLD` (from `shared/memory_policy.json`, default 0.86).
- Build clusters by **union-find** over the resulting neighbour edges.
- Report: number of clusters with ≥2 members; total entries contained in multi-member clusters; the 10 largest clusters as samples (`{canonical_guess_id, member_ids, member_domains, max_pairwise_cosine}`).
- This is an **estimate for measurement only** — it never merges. Phase 1 reuses the same candidate logic under governance.
- Batch the vector queries; cap total queries and log if capped.

**M5 — Growth.**
- From the Dream run ledger (`dream:runs:index` + `dream:run:*` records), compute the net change in active-topic count over the trailing 14 days, plus total intake vs total archived in that window.
- If ledger history is insufficient, report what is available and mark the window as partial.

**M6 — Recall@k.**
- For each probe in `recall_probes.json`, call the production `search(query, limit=k)` (k=5) and check whether any `expect_any_of` target appears in the top-k (match by exact `id` or case-insensitive `domain` substring).
- Report `recall_at_5` (fraction of probes that hit) and a per-probe pass/fail list.

**M7 — Access-signal coverage.**
- Fraction of active entries with `access_count > 0` **and** `last_accessed != null`.
- Cross-check: among the entries that the M6 probes *did* return, how many have populated access signals (this directly tests the P5 "reinforcement loop dead" hypothesis).

### 2.3 Output
- Write `scripts/reports/audit_memory_quality_<ISO8601>.json` with the schema below; print a compact human summary to stdout.

```json
{
  "schema_version": 1,
  "generated_at": "<ISO8601>",
  "active_counts": { "knowledge": 0, "project": 0, "total": 0 },
  "m1_tiers": { "tier_1": 0, "tier_2": 0, "tier_3": 0, "tier_1_share": 0.0, "tier_2_share": 0.0 },
  "m3_salience": {
    "max_single_value_share": 0.0,
    "top_values": [ { "value": 0.3306, "count": 0, "share": 0.0 } ],
    "histogram": [ { "bin": 0.33, "count": 0 } ]
  },
  "m4_duplicates": {
    "cosine_threshold": 0.86,
    "neighbor_k": 10,
    "multi_member_clusters": 0,
    "entries_in_clusters": 0,
    "largest_clusters": [ { "member_ids": [], "member_domains": [], "max_pairwise_cosine": 0.0 } ],
    "query_capped": false
  },
  "m5_growth": { "window_days": 14, "net_active_delta": 0, "intake": 0, "archived": 0, "window_partial": false },
  "m6_recall": { "recall_at_5": 0.0, "probes": [ { "query": "", "hit": false, "returned_ids": [] } ] },
  "m7_access": { "active_with_access_share": 0.0, "probe_returned_with_access_share": 0.0 }
}
```

### 2.4 Constraints
- Strictly read-only. Add an assertion/guard that the script holds no write client, or that any write method is unused.
- Safe to run against production; recommend first run against the staging fixture.

---

## 3. `tests/fixtures/recall_probes.json`

The M6 oracle and the precursor to the T9 fidelity regression.

**Schema:** array of objects:
```json
[
  { "query": "demographic inflation pressure DIP factor", "expect_any_of": ["DIP", "demographic inflation", "Juselius"], "notes": "ASADO flagship; currently returns 0 DIP entries" },
  { "query": "InMobi IPO advisory", "expect_any_of": ["InMobi"], "notes": "current advisory work" },
  { "query": "T2 factor timing IC saturation", "expect_any_of": ["IC saturation", "T2", "factor timing"], "notes": "" },
  { "query": "LoopPilot autonomous experiment loop", "expect_any_of": ["LoopPilot"], "notes": "known duplicate cluster" }
]
```
Seed ~20 probes spanning current work (ASADO/DIP, InMobi, T2/GDELT, LoopPilot, Pattern CNN / Jiang-Kelly-Xiu, PKS itself). **Operator to review/extend** — accuracy of the `expect_any_of` targets matters; flag any guesses.

---

## 4. `verify_memory_quality` gate

- Emit a record matching the existing gate shape returned by `get_validation_status` (`schema_version`, `generated_at`, `gate`, `passed`, `status`, `issues[]`, `report_path`, `details`).
- **Fail** if any of:
  - `m1_tiers.tier_1_share > THRESHOLD_TIER1`
  - `m4_duplicates.entries_in_clusters / active_total > THRESHOLD_DUP`
  - `m6_recall.recall_at_5 < THRESHOLD_RECALL`
- Start thresholds **generous** so the gate is meaningful but not noise, then tighten toward the PRD targets:
  - `THRESHOLD_TIER1`: 0.40 → 0.25
  - `THRESHOLD_DUP`: 0.20 → 0.05
  - `THRESHOLD_RECALL`: 0.60 → 0.80
- On current production data this gate should **fail** (M1 ≈ 0.75). That is the correct initial signal.
- Write into the same validation ledger the other gates use.

---

## 5. Config (single source of truth)

Add to `shared/memory_policy.json` (read by Phase 0 audit/gate **and** Phase 1 dedup):
- `COSINE_DUP_THRESHOLD` (0.86)
- `DEDUP_NEIGHBOR_K` (10)
- A `quality_gate` block: `{ threshold_tier1, threshold_dup, threshold_recall }`.

---

## 6. Makefile

- `make audit-memory-quality` → runs the script against the configured (prod by default) store, writes the JSON report, prints the summary.
- `make verify-memory-quality` → runs the gate and records the ledger entry.
- Keep naming/style consistent with the existing surface (`verify-memory-full`, `check-overnight-dream`, `staging-smoke`).

---

## 7. Tests

- Unit-test the **union-find clustering** and each **metric calculator** with synthetic inputs (no network): e.g., feed three synthetic entries with pairwise cosines above/below threshold and assert cluster membership; feed a salience list and assert `max_single_value_share`.
- Run the full script read-only against the staging fixture before any production run.

---

## 8. Acceptance criteria

- `make audit-memory-quality` prints all seven metrics and writes a conforming JSON report.
- `verify_memory_quality` appears in the validation ledger and **fails on current production data** (M1 breach).
- Unit tests for clustering + calculators pass.
- A second run after Phase 1 shows movement on M1/M3/M4 (the before/after the whole effort is measured against).

---

## 9. Out of scope

- Any mutation (the actual dedup merge is Phase 1).
- Changing salience, tiering, or archive behavior.
- The T9 source→entry fidelity test (separate; needs raw conversation exports).
