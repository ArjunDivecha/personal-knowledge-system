# PKS Memory Operating System — Nightly Maximalist Rewrite PRD

**Version:** 0.4 (nightly maximalist)
**Status:** Draft
**Date:** 2026-05-07

> **Implementation note (2026-06-09):** R10 (multi-vector embeddings + LLM-as-judge reranking)
> described in §9 and the roadmap table is **NOT YET IMPLEMENTED**. The current production search
> uses deterministic score-based ranking (semantic similarity × salience × recency × source weight
> × tier multiplier). File paths like `src/retrieval/llm-judge-reranker.ts` do not exist.
> Everything from v0.1–v0.3 (R1–R9) is committed and live; R10 is planned.
**Supersedes:** v0.3
**Constraint:** Dream runs once per night. No compute, time, or cost constraints *within* that run. Read-path (retrieval) is demand-driven and unconstrained.

This is v0.3 with cost ceilings stripped from the nightly job. The design optimizes for *truth, durability, and recall quality* over compute efficiency. Where extra compute compounds correctness (ensemble adjudication, multi-window synthesis, full-backlog processing), it goes in. Where extra compute is just bigger numbers without better outcomes (more LLM calls in a deterministic policy check), it does not.

---

## Part A — What the Nightly Constraint Means

Practically: the Dream job has from roughly midnight to operator-wakeup (~6–8 hours) to do everything. Everything = synthesis across all temporal windows, full contested-backlog adjudication, promotion, decay, archival, verification, eval harness, validation gates. No pieces deferred for tomorrow's run unless they fail today's grader.

Three corollaries follow:

1. **Backlog clearance, not throttling.** v0.3 capped archives at 10/run because of blast radius. With propose-grade-apply, blast radius is bounded by the grader, not by a count. Apply *all* graded archives, *all* graded promotions, *all* graded decays in one night.
2. **Ensemble everything sensitive.** Adjudication and synthesis become multi-model + multi-pass because nights are long enough for that and disagreement-as-information is more valuable than time-to-completion.
3. **Read-path is not throttled.** Retrieval is demand-driven and serves the operator interactively. Multi-vector embeddings and LLM-as-judge reranking apply to every search, not just the nightly pass.

---

## Part B — What v0.3 Throttled, Lifted Now

| v0.3 decision | Throttle reason | v0.4 |
|---|---|---|
| Synthesis weekly cadence (Sundays) | "~7× cost reduction" | **Nightly**, single pass spanning all temporal windows |
| Synthesis 30-day session window | Single-window for cost | **Multi-window in one pass**: 24h, 7d, 30d, 90d, all-time — emerge patterns at every timescale (§3) |
| Single Sonnet 4.6 call per synthesis bucket | Cost | **Five-extractor ensemble + critic pass** (§3) |
| Adjudicator: 20 clusters/run cap, single Sonnet call | Cost | **No cap, ensemble + multi-pass** — full 127 backlog cleared first night (§4) |
| Promotion limit 10/run | "Blast radius" | Limit removed; grader controls safety; promote everything graded valid (§5) |
| Archive limit 10/run | "Blast radius" | Same; clear the 676 backlog if grader approves (§5) |
| Eval harness: 50 operator-seeded golden queries | Operator time | **Operator-seeded + thousands auto-generated from operator's actual chat history**; LLM-as-judge per query (§6) |
| Reconsolidation only on retrieval | Compute | Retrieval-time still happens; **plus** nightly reconsolidation pass that revisits every recently-accessed entry for context-type reassessment (§7) |
| Knowledge graph (Neo4j) deferred | "Orthogonal" | **Shipped** as third storage layer with graph-traversal retrieval (§8) |
| Single embedding model | Cost / storage | **Multi-vector**: four models in parallel, query-time fusion (§9 — read-path) |
| Search ranks by similarity + salience + recency | Adequate | **+ LLM-as-judge reranking** of top 30 → top 10 on every search (§9 — read-path) |
| Shadow mode: 14-day comparison window | Time-to-cutover | **Permanent shadow** — old + new lifecycles run nightly forever as continuous regression signal (§10) |
| 30-day session window an open question | Punt | **Resolved** — multi-window obviates the question (§3) |
| Provenance gate ≥2 sources ≥2 days | Hallucination defense | **Kept**, plus operator-simulator gate (§3) |
| Adjudicator entry-bounded context | Soundness (kept) | **Kept** — soundness, not cost |
| Deterministic grader stays cheap | Adequate | **Kept** — adding LLM here adds noise, not signal |

The point: lifting cost doesn't mean LLM-everywhere. Some places extra compute compounds correctness. Other places it adds noise.

---

## Part C — Foundations from v0.3 (Unchanged)

These are correctness-driven, not cost-driven, so they stand verbatim from v0.3:

**R0 hotfixes ship first, non-negotiable:**
- `scripts/check_overnight_dream_run.py` — `UTC = timezone.utc` moved to top of file
- `distillation/models/entries.py` — `Evolution` parsing uses `.get()` with empty-string defaults across `delta`, `trigger`, `from_view`, `to_view`, `date`
- `docs/pks-memory-upgrade-checklist.md` — reconcile cap statements
- Move `test_mcp*.py`, `test_sse_connection.py` to `legacy/`

**Lifecycle envelope (v0.3 §11):**
- Snapshot → Propose → Grade → Apply → Verify → Publish
- Per-phase artifacts at `dream:run:{run_id}:{phase}`
- Idempotent transactional apply, rollback by run_id
- New write tools: `run_dream_proposal`, `grade_dream_proposal`, `apply_dream_proposal`, `rollback_dream_run`

**Schema additions (v0.3 §15):** `synthesis_origin`, `last_operator_override`, `tier3_admission_date`, `adjudication_history`, `state_confidence`, `contested_reason_codes`, `revision`.

**Plus v0.4 schema additions:**

```python
ensemble_agreement: Optional[dict]      # per-model verdicts: {"opus": "agree", "gpt5": "agree", "gemini": "disagree", ...}
multi_window_evidence: Optional[dict]   # which synthesis windows surfaced this: {"7d": [conv_ids], "30d": [...]}
operator_simulator_score: Optional[float]  # how Arjun-shaped is this entry? 0.0–1.0
graph_neighbors: list[str]              # related entry IDs via Neo4j
embedding_versions: dict[str, str]      # {"openai-3-large": vec_id, "voyage-3": vec_id, ...}
adversarial_grader_findings: list[str]  # red-team grader objections at apply time
```

**Validation ledger (v0.3 §17):**
- `/validation` endpoint separate from `/health`
- Gates: typecheck, runtime tests, python checker, overnight dream run, full strict consistency, thin-index parity, vector parity, eval harness MRR
- Runs once per night as part of Dream lifecycle, results posted to `validation:last`

---

## Part D — The Nightly Run Plan

A single Dream invocation (cron `10 7 * * *` UTC, ~midnight Pacific) does the following in order. Estimated wall-clock based on current production data: 4–6 hours.

### Stage 0: Snapshot (5 minutes)

Capture immutable view of:
- All knowledge entries
- All project entries
- All side-key access counters and last-accessed timestamps
- Last 90 days of session exports (.pks/agent-context, Twitter ingestion, Gmail ingestion)
- Current thin index, validation ledger status
- Last 7 days of operator override actions (`set_context_type`, `restore_archived`, manual `consolidate_entries`)

Snapshot persisted at `dream:run:{run_id}:snapshot` with explicit pointer; downstream stages read this pointer, never live state.

### Stage 1: Propose (45–90 minutes)

Six proposal generators run in parallel within Stage 1:

#### 1a. Multi-Window Synthesis

Five temporal windows, processed in parallel:

| Window | Use |
|---|---|
| 24h | Just-resolved frictions, immediate workflow patterns |
| 7d | Emerging sprint-level patterns |
| 30d | Stable workflow convergence |
| 90d | Project-level patterns, multi-month evolution |
| all-time | Identity-level patterns, decade-long evolution |

For each window, sessions bucketed per-repo plus a cross-project bucket. Per bucket, **five-extractor ensemble** runs in parallel:

1. **Claude Opus 4.7** — strongest reasoning, primary
2. **Claude Sonnet 4.6** — different RLHF lineage
3. **GPT-5** (current frontier) — different training corpus
4. **Gemini 2.5 Pro** — different architecture
5. **Operator Simulator** (`arjunsuperllm`, the REER challenger) — *"if I were Arjun describing this pattern, would the phrasing land?"*

Aggregation:
- Pattern emitted only if ≥3 of 5 extractors independently surface it (semantic match via embedding similarity ≥ 0.85)
- 5/5 → `confidence=high`, `injection_tier=1`
- 4/5 → `confidence=medium`, `injection_tier=2`
- 3/5 → `confidence=low`, `injection_tier=2`
- <3/5 → discarded

**Critic pass.** Single Opus 4.7 call across all candidate patterns: *"For each pattern, identify the strongest counter-evidence from the operator's session history. Reject any pattern where counter-evidence is comparable to supporting evidence."* This is the v0.3 hallucination defense, formalized and made adversarial.

**Provenance gates retained.** ≥2 distinct `source_conversation_id`s, ≥2 distinct days, density-weighted (pattern across 6/30 days outranks 12× on 1 day). These are correctness rules, not cost rules.

**Cross-window deduplication.** A pattern surfacing in both `7d` and `30d` strengthens the proposal (`multi_window_evidence` records both); doesn't double-count. A pattern surfacing only in `24h` and not in any longer window has lower confidence and a flag indicating it may be transient.

**Output.** Proposals of type `synthesize_create`. `synthesis_origin = "{run_id}:{window}"`. All five window streams unified before grading.

#### 1b. Promotion Proposal Generator

Multi-rule, multi-tier promotion engine. Rules in `shared/memory_policy.json`:

```json
"promotion_rules": [
  {"from": {"tier": 3}, "to": {"tier": 2},
   "require": {"access_count_30d": 3, "mention_count": 2}},
  {"from": {"tier": 2}, "to": {"tier": 1},
   "require": {"access_count_60d": 5, "salience_min_pct": 50}},
  {"from": {"context_type": "task_query"},
   "to": {"context_type": "recurring_pattern"},
   "require": {"mention_count": 3, "distinct_conversations": 3}},
  {"from": {"context_type": "explicit_save"},
   "to": {"context_type": "stated_preference"},
   "require": {"access_count": 4, "linguistic_match": "preference_shape"}},
  {"from": {"context_type": "active_project"},
   "to": {"context_type": "professional_identity"},
   "require": {"engagement_days": 180, "salience_top_n": 25}}
]
```

Hysteresis: promote at the rule's threshold; demote (by the parallel `demotion_rules`) only at zero accesses. Sticky operator overrides: 30-day cooldown after manual `set_context_type` downgrade.

**No cap.** Every entry meeting a rule generates a `promote` proposal.

#### 1c. Decay Proposal Generator

```json
"demotion_rules": [
  {"from": {"tier": 1}, "to": {"tier": 2},
   "require": {"access_count_60d": 0, "salience_below_tier_floor": true}},
  {"from": {"tier": 2}, "to": {"tier": 3},
   "require": {"access_count_90d": 0, "salience_below_tier_floor": true}}
]
```

Plus the 14-day Tier-3 admission buffer: an entry must have `tier3_admission_date` ≥ 14 days old before it becomes archive-eligible. This makes the Tier 1→2→3→archive flow graceful, not a cliff.

#### 1d. Contradiction-Detection with Tightened Threshold

Threshold tuning from v0.3 §R5a:

```json
"contradiction_detection": {
  "max_topic_similarity": 0.10,
  "require_lexical_signal": true,
  "lexical_signals": ["always|never", "must|must not", "use|do not use",
                      "outperform|underperform", "bullish|bearish",
                      "buy|sell", "increase|decrease"]
}
```

**One-time backlog re-evaluation.** The 127 currently-contested entries get re-scored against the new threshold. Those that no longer qualify revert to `state=active` automatically (filed as `revert_contested` proposals, graded as low blast-radius). Those that still qualify proceed to the adjudicator in 1e.

#### 1e. Adjudicator (Outcomes-style, ensemble + multi-pass)

For each contested cluster surviving the tightened threshold:

**Pass 1: Independent verdicts (parallel, 4 models)**
- Opus 4.7: `{verdict: supersede|scope|unresolvable, evidence}`
- Sonnet 4.6: same shape
- GPT-5: same shape
- Gemini 2.5: same shape
- Operator Simulator: same shape, plus *"is this contradiction the kind Arjun cares about, or is the disagreement irrelevant to his actual work?"*

**Pass 2: Cross-examination**
Each model sees the others' verdicts (anonymized) and identifies the strongest counter-argument it disagrees with. Generates rebuttals.

**Pass 3: Adversarial red team**
Separate Opus call: *"These models concluded X. Find the strongest reason this verdict is wrong. Argue the case for the opposite verdict."*

**Pass 4: Final verdict**
Opus call sees: cluster, Pass-1 verdicts, Pass-2 cross-examination, Pass-3 adversarial argument. Issues final verdict with explicit acknowledgment of the strongest counter-argument.

**Acceptance rules:**
- Verdict applied only if Pass-1 majority + Pass-4 final agree
- Discrepancy → defer to operator review with full transcript
- Any model invoking case (c) "unresolvable" → defaults to case (c) regardless of majority. *Preserving disagreement is always safer than collapsing it.*

**Strict isolation preserved.** No model in any pass sees README, other knowledge entries, operator's recent activity, or current chat state. Entry-bounded context is a soundness rule.

**No cap.** All 127 contested entries go through the full pipeline first night. ~$1.50 per cluster fully adjudicated → ~$200 to clear the backlog. Trivial.

#### 1f. Duplicate-Merge Proposals

Existing token-Jaccard logic from `dream.ts` (similarity ≥ 0.3 with opposing-marker veto) preserved. No change — this layer is already sound.

### Stage 2: Grade (15–30 minutes)

Two-layer grader:

#### 2a. Deterministic Policy Grader (fast)

For every proposal:
- `archive` requires salience < threshold AND mention_count == 1 AND access_count == 0
- `promote` requires meeting the per-rule policy thresholds
- `demote` requires zero accesses for the rule window
- `mark_contested` requires both semantic + lexical signal (post-tightening)
- `synthesize_create` requires ≥2 sources spanning ≥2 days, plus operator-simulator score ≥ 0.6
- Sticky operator override check: any entry with `last_operator_override` within 30 days for the relevant action type → reject

Output: `{passed: bool, violations: Violation[], blast_radius: BlastRadius}`. Violations preserved for operator review, not silently dropped.

#### 2b. Adversarial Red-Team Grader (LLM, slower)

Separate Opus call sees the full proposal set: *"Find the proposals most likely to be wrong. For each: what evidence is missing? What operator behavior would this conflict with? What's the strongest case against this change?"*

Findings stored in `adversarial_grader_findings`. Proposals with severe red-team objections downgraded to `confidence=low` or rejected entirely.

This is the single highest-leverage compute spend in the new architecture. The adversarial grader is the difference between "the system thinks this is right" and "the system has tried hard to prove this is wrong and failed."

### Stage 3: Apply (60–120 minutes)

Transactional. Per proposal: load entries, apply mutation, write before/after snapshots to `dream:run:{run_id}:apply` with rollback metadata, update Redis + Vector + Neo4j atomically.

**Mutation log per change:** entry IDs, before/after revisions, proposal type, rollback metadata.

**Rollback by run_id:** `rollback_dream_run` undoes every mutation from that run, restoring all touched entries from before-snapshots in reverse order.

**Apply order matters:**
1. `revert_contested` (cleanup wrong contradictions first)
2. `duplicate_merge` (consolidate before promoting/demoting)
3. `adjudicate_resolve` (settle disputes)
4. `promote` then `demote` (tier moves)
5. `synthesize_create` (new entries enter at end)
6. `archive` (last, after everything else has settled)

### Stage 4: Verify (30–45 minutes)

After Apply, comprehensive verification:

- Thin-index totals match active Redis counts per type
- Vector metadata parity for every entry (full mode, not sampled)
- Neo4j ↔ Redis consistency check
- Side-key access counters consistent with metadata
- No archived entry returned by default search (sample 100 archived IDs, query each)
- Reconsolidation idempotency: re-running access-counter increments should be no-op

Failures here halt the run and trigger automatic rollback via Stage 3's metadata.

### Stage 5: Eval (30–60 minutes)

Two parallel eval streams:

#### 5a. Operator-Seeded Golden Queries

50 hand-curated queries with expected top-3 entry IDs. Run against staging post-Apply. Compute P@3, MRR, NDCG@10. Compare against rolling 7-day baseline. Any golden query falling out of top-10 fails the eval gate.

#### 5b. Auto-Generated Eval Set

Auto-generate ~2,000 synthetic queries from the operator's last 90 days of chat history:
- Sample 200 questions Arjun actually asked
- Have Opus rephrase each into 5–10 variants (shorter, longer, different framing, different keywords)
- For each variant, the expected top result is whatever the unmodified retrieval pipeline returned for the original query *before* this Dream run
- Apply post-Dream pipeline; measure how many variants still surface the same top result

This catches semantic drift across Dream runs that 50 hand-curated queries would miss. The auto-set regenerates monthly to track moving operator interests.

#### 5c. LLM-as-Judge Relevance Scoring

For a sample of 500 queries (mix of golden + auto-generated), independent Opus call: *"Query: X. Top 5 results returned: A, B, C, D, E. For each result, score relevance 0–10. Justify."*

Mean LLM-judge relevance score becomes a tracked metric alongside MRR. Useful because P@3 and MRR measure rank-position; LLM-as-judge measures *did this actually answer the question*.

### Stage 6: Publish (5 minutes)

Update pointers:
- `dream:last_run` → run_id (atomic)
- `validation:last` → eval results
- `validation:gate_status` → green/yellow/red
- Append run summary to `dream:run_history` (capped at 365 entries — one year of nightly runs)
- Emit run record to `dream:run:{run_id}:events` (final event with `completed_at` timestamp)

---

## Part E — Read-Path Improvements (Demand-Driven, Not Nightly)

These run on every retrieval, not on the nightly schedule.

### §9. Multi-Vector Embedding Retrieval

Every entry stores four embeddings in parallel:

1. `text-embedding-3-large` (OpenAI, 3072d) — current default
2. `voyage-3-large` (Voyage, 1024d) — strong on technical/scientific text
3. `cohere-embed-v3` (Cohere, 1024d) — strong cross-lingual; helps with mixed English/Hindi/financial-jargon
4. `e5-mistral-7b-instruct` (open-weight, 4096d, runs locally) — control sample with different lineage

Query-time:
- Query embedded by all four
- Top-k retrieved from each vector store (k=20 each)
- Reciprocal rank fusion combines results into top 30
- LLM-as-judge reranks top 30 → top 10 (next section)

Storage cost negligible at 2,500-entry scale. Empirical recall improvement on benchmark sets is 8–15%; for specialized domains (quant finance + AI tooling) the gain skews higher.

### §10. LLM-as-Judge Reranking

For every retrieval call:

1. Top 30 from multi-vector fusion
2. Single Sonnet 4.6 call: *"User query: [Q]. Here are 30 candidate entries with summaries. Rank top 10 by actual relevance. For each excluded entry, briefly state why."*
3. Reranked top 10 returned to caller
4. Excluded entries' "why excluded" reasons logged: if the same entry is excluded for the same kind of query repeatedly, that's a soft signal it may belong in a different `context_type` (feeds into next-night promotion/demotion proposals)

Latency adds ~800ms per retrieval. Acceptable for chat use; programmatic high-volume callers can opt out via flag.

### §11. Knowledge Graph (Neo4j) Layer

Ship as third storage layer alongside Redis and Upstash Vector.

**Schema:**
- Nodes: `KnowledgeEntry`, `ProjectEntry`, `Repo`, `Conversation`, `Concept` (auto-extracted), `Person` (operator's contacts)
- Relationships: `SUPERSEDES`, `CONTRADICTS`, `IMPLEMENTS`, `EVIDENCED_BY`, `BUILT_FOR`, `MENTIONS`, `RELATES_TO`, `EVOLVED_FROM`

**Sync:** Apply phase mutations patch Neo4j atomically with Redis + Vector. Nightly verification (Stage 4) catches drift. Redis is canonical; Neo4j is reconstructable.

**New retrieval modes:**
- `graph_traverse(entry_id, depth=2, relationship_filter=[...])` — graph walk from a starting entry
- `concept_neighborhood(concept_query)` — entries clustered around a concept
- `evolution_chain(entry_id)` — temporal walk via `EVOLVED_FROM` to see how a position changed

These complement (don't replace) similarity retrieval. Particularly useful for "what else is connected to this?" questions where pure similarity misses structural relationships.

---

## Part F — Phasing

Same R0–R9 spine as v0.3, parallelized where possible. Nightly maximalist features layer onto the lifecycle envelope after R4.

| Phase | Items | Duration | Parallel with |
|---|---|---|---|
| **R0** | Bug fixes, doc reconciliation, probe quarantine | 2 days | — |
| **R1** | Run artifact schema, validation ledger, new read tools | 1 week | — |
| **R2** | Refactor `runDreamCycle` → six-stage pipeline (preserves current behavior) | 2 weeks | R1 last week |
| **R3** | Deterministic policy grader; new write tools | 1 week | R2 last week |
| **R4** | Transactional apply + rollback by run_id; staging drill | 1 week | — |
| **R5a** | Contradiction threshold tuning + backlog re-eval | 3 days | — |
| **R5b** | Ensemble adjudicator (4 models + operator-sim, 4 passes) | 3 weeks | R5c, R5d |
| **R5c** | Multi-rule promotion engine | 1 week | R5b, R5d |
| **R5d** | Decay flow with Tier-3 admission buffer | 1 week | R5b, R5c |
| **R6** | Multi-window ensemble synthesis (5 windows × 5 extractors + critic) | 4 weeks | R5* |
| **R7** | Eval harness — operator-seeded + auto-generated + LLM-as-judge | 2 weeks | — |
| **R8** | Adversarial red-team grader | 1 week | After R3 |
| **R9** | Knowledge graph (Neo4j) layer | 3 weeks | After R4 |
| **R10** | Multi-vector embeddings + LLM-as-judge reranking | 2 weeks | After R9 |
| **R11** | Permanent shadow mode for cutover | 2 weeks | All others first |

Total wall-clock: ~14 weeks with parallelization (vs ~14 weeks sequentially in v0.3, but with a much larger feature set delivered).

---

## Part G — Cost Profile

Nightly run, fully unconstrained:

| Stage | Component | Estimated cost/night |
|---|---|---|
| Stage 1a | Multi-window synthesis: 5 windows × ~10 buckets × 5 extractors × Opus/Sonnet/GPT-5/Gemini calls | $20–40 |
| Stage 1a | Critic pass | $5–10 |
| Stage 1e | Adjudicator: ~10–30 contested clusters/night × 4 passes × 4 models | $30–80 (steady state; first night clearing 127 backlog ≈ $200) |
| Stage 2b | Adversarial red-team grader | $5–10 |
| Stage 5c | LLM-as-judge eval scoring (500 queries × Opus) | $10–20 |
| Read-path | LLM-as-judge reranking (per retrieval × ~50 retrievals/day × Sonnet) | $1–3/day |
| **Total** | | **$70–165/night, ~$2,000–5,000/month steady state** |

Plus first-night backlog clearance overhead of ~$200 for the contested-entry adjudication blitz.

This is materially more than v0.2's ~$50/month. The trade is: dramatically better truth, durability, and recall quality at ~$2K–5K/month vs ~$50/month with materially worse outcomes. At Arjun's professional scale and the value of correctly recalling decades of investment context plus active-project nuance, this is straightforwardly worth it.

---

## Part H — Risks (Updated)

| Risk | Severity | Mitigation |
|---|---|---|
| Ensemble disagreement is *itself* noisy | Medium | Conservative aggregation rules (≥3/5 for synthesis emit; majority+final for adjudicator); when uncertain, defer to operator review with full transcripts |
| Models in ensemble correlated despite different lineages | Medium | Operator simulator brings genuinely orthogonal signal (trained on Arjun's actual writing); adversarial red team explicitly searches for ensemble blind spots |
| Multi-window synthesis floods proposal set | Low | Cross-window dedup before grading; provenance gates filter aggressively |
| LLM-as-judge reranker introduces latency operator notices | Low | ~800ms tolerable in chat; opt-out for programmatic callers; cache rerank results for repeated queries |
| Knowledge graph drifts from Redis | Medium | Stage 4 verification catches drift; Redis is canonical; Neo4j fully reconstructable |
| Adversarial grader rejects too aggressively | Medium | Findings stored not auto-applied; deterministic grader is primary gate; adversarial is supplementary |
| 4–6 hour nightly window slips beyond operator wakeup | Low | Stage parallelization keeps wall-clock bounded; stages can run on Cloudflare Workers with parallel invocation; long-context calls (Opus 200K) parallelize across windows |
| Multi-vector storage explosion | Low | 4× current vector storage at 2,500 entries is trivial; capacity plans accommodate 10× growth |
| Continuous shadow mode reveals divergences forever | Low | That's the point — divergence is information; investigate by category, not aggregate |

---

## Part I — Success Metrics (90 days post-R11)

- **R0**: All Makefile gates pass on every CI run
- **Synthesis**: 50–150 synthesized entries (multi-window emits more than v0.3's 30–80); ≥60% accessed at least once; ≥20 promoted to `confidence=high` via 5/5 ensemble agreement
- **Adjudication**: 127-entry backlog cleared first night; new contradictions resolved within 24 hours
- **Promotion**: ≥10 promotion events/week sustained; Tier 2 count rises from 31 to ≥200; Tier-3 backlog (731) drops to ≤300 via promotion + decay
- **Decay**: 691-entry decay backlog cleared; entries flow Tier 1→2→3→archive without cliff
- **Archive**: 676-entry backlog cleared first week
- **Contradictions**: New contradictions per run drops from 40 to ≤5 (tightened threshold)
- **Eval harness MRR**: stable or improving relative to pre-R11 baseline
- **LLM-as-judge mean relevance**: ≥7.5/10 on golden queries; ≥7.0/10 on auto-generated set
- **Operator overrides**: <5% of automated changes reverted by operator within 30 days (sanity check on Dream judgment)
- **Cost**: $2,000–5,000/month steady state, well under the no-budget assumption
- **Reliability**: 7 consecutive clean nights; rollback drill executed end-to-end successfully on staging at least once
- **Multi-vector retrieval**: ≥10% recall improvement over single-embedding baseline measured by LLM-as-judge
- **Graph retrieval**: graph-traversal queries serve ≥5% of total retrieval traffic within 60 days

---

## Part J — Open Questions

Reduced to two genuinely empirical questions:

1. **Operator simulator weight in ensembles.** Should `arjunsuperllm` count equally with frontier models in the 5-extractor synthesis vote? It has different failure modes (overfits to operator's actual word patterns). v0.4 commits to equal weighting initially; revisit after 30 days based on agreement-with-other-models measurements.

2. **LLM-as-judge bias on reranking.** Sonnet may systematically favor certain answer shapes regardless of relevance. v0.4 commits to Sonnet 4.6 initially; cross-check periodically by running the same rerank with Opus 4.7 and comparing top-10 overlap. If overlap < 80%, switch reranker to Opus.

Everything else from v0.1, v0.2, v0.3 is now a committed design decision.

---

## Part K — What Did Not Get Bigger

Worth being explicit about what doesn't change with constraints lifted:

- **Cadence**: nightly only (your constraint)
- **Operator override stickiness**: still 30 days; operator authority is correctness, not cost
- **Provenance gates**: still ≥2 sources ≥2 days; correctness, not cost
- **Adjudicator entry-bounded context**: still strict; correctness, not cost
- **Reversibility**: still per-entry snapshot + per-run rollback; correctness, not cost
- **Existing read tools**: still backward-compatible; UX, not cost
- **Storage backend**: still Cloudflare + Upstash + (now) Neo4j; "good enough", not cost-driven
- **Deterministic policy grader stays deterministic**: adding LLM there adds noise, not signal

The pattern: where extra compute compounds correctness (ensemble adjudication, multi-window synthesis, full-backlog processing, LLM-as-judge, adversarial grader), extra compute goes in. Where extra compute is just bigger numbers without better outcomes, it does not.

---

## Part L — Files to Touch

R0 (unchanged from v0.3):
- `scripts/check_overnight_dream_run.py`
- `distillation/models/entries.py`
- `docs/pks-memory-upgrade-checklist.md`
- Move `test_mcp*.py`, `test_sse_connection.py` to `legacy/`

R1–R4 (lifecycle envelope, unchanged from v0.3):
- `cloudflare-mcp/mcp-server/src/dream/snapshot.ts`
- `cloudflare-mcp/mcp-server/src/dream/propose.ts`
- `cloudflare-mcp/mcp-server/src/dream/grade.ts`
- `cloudflare-mcp/mcp-server/src/dream/apply.ts`
- `cloudflare-mcp/mcp-server/src/dream/verify.ts`
- `cloudflare-mcp/mcp-server/src/dream/publish.ts`
- `cloudflare-mcp/mcp-server/src/dream/lifecycle.ts`
- `cloudflare-mcp/mcp-server/src/dream/rollback.ts`
- `cloudflare-mcp/mcp-server/src/validation.ts`

R5 (content layer):
- `cloudflare-mcp/mcp-server/src/dream/contradiction-detection.ts` (R5a)
- `cloudflare-mcp/mcp-server/src/dream/adjudicate.ts` (R5b — ensemble + multi-pass)
- `cloudflare-mcp/mcp-server/src/dream/promote.ts` (R5c)
- `cloudflare-mcp/mcp-server/src/dream/decay.ts` (R5d)

R6 (synthesis):
- `cloudflare-mcp/mcp-server/src/synthesis/extractors/` — one file per extractor model
- `cloudflare-mcp/mcp-server/src/synthesis/aggregator.ts`
- `cloudflare-mcp/mcp-server/src/synthesis/critic.ts`
- `cloudflare-mcp/mcp-server/src/synthesis/operator-simulator.ts`
- `cloudflare-mcp/mcp-server/src/synthesis/multi-window.ts`

R7 (eval harness):
- `cloudflare-mcp/mcp-server/test/golden-queries.json`
- `cloudflare-mcp/mcp-server/src/eval/auto-generator.ts`
- `cloudflare-mcp/mcp-server/src/eval/llm-judge.ts`
- `cloudflare-mcp/mcp-server/src/eval/regression-detector.ts`

R8 (adversarial grader):
- `cloudflare-mcp/mcp-server/src/dream/adversarial-grader.ts`

R9 (Neo4j):
- `cloudflare-mcp/mcp-server/src/graph/neo4j-client.ts`
- `cloudflare-mcp/mcp-server/src/graph/sync.ts`
- `cloudflare-mcp/mcp-server/src/graph/traversal.ts`
- `distillation/graph/seed.py` — one-time graph seeding from existing Redis state

R10 (multi-vector + reranker):
- `cloudflare-mcp/mcp-server/src/retrieval/multi-vector.ts`
- `cloudflare-mcp/mcp-server/src/retrieval/fusion.ts`
- `cloudflare-mcp/mcp-server/src/retrieval/llm-judge-reranker.ts`

R11 (shadow mode):
- `cloudflare-mcp/mcp-server/src/dream/shadow.ts`
- `.github/workflows/dream-shadow-diff.yml`

Schema:
- `distillation/models/entries.py` — additions per Part C
- `shared/memory_policy.json` — promotion rules, demotion rules, contradiction thresholds, ensemble configuration, multi-window synthesis configuration

Tooling:
- `Makefile` additions: `dream-eval`, `dream-synthesize-dry-run`, `dream-adjudicate-dry-run`, `dream-decay-dry-run`, `rethreshold-contradictions`, `dream-shadow-diff`, `rollback-dream-run`, `rebuild-graph`, `regenerate-eval-set`

---

## Part M — Bottom Line

v0.3 was constrained by cost and threw away capability to fit a budget. v0.4 says: budget isn't the constraint — *one nightly run* is the constraint, and within that run we spend whatever is needed for the highest-quality memory operating system possible.

The five highest-leverage changes over v0.3:

1. **Ensemble adjudication.** Single-model adjudication is a correctness ceiling. Four frontier models + operator simulator + adversarial red team is genuinely more sound. The 127-entry backlog is cleared in one night with full provenance.
2. **Multi-window synthesis.** Patterns at 24h, 7d, 30d, 90d, all-time emerged in one nightly pass, with 5-model ensemble + critic. Catches both immediate friction and decade-long evolution.
3. **Adversarial red-team grader.** Single highest-leverage compute spend. Difference between "the system thinks this is right" and "the system tried to prove this wrong and failed."
4. **Multi-vector retrieval + LLM-as-judge.** Read-path quality compounds for every operator query, every day. ~$1–3/day for materially better recall.
5. **Knowledge graph layer.** Enables structural retrieval (graph traversal, evolution chains) that pure semantic similarity cannot.

R0 still ships first because no other improvement is meaningful until validation gates work.
