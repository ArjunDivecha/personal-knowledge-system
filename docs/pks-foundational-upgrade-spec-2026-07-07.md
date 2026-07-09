# PKS / Dream Foundational Upgrade Spec

**Date:** 2026-07-07
**Author:** Fable 5 (commissioned via `docs/fable-pks-dream-upgrade-brief.md`)
**Status:** Design — no live-store mutation was performed while producing this document. All write tools were left untouched.
**Grounding:** Live MCP reads (`get_index`, `list_dream_runs`, `get_dream_summary`, `get_validation_status`, three `get_deep` entry reads) on 2026-07-07, plus line-level reading of the production Worker (`cloudflare-mcp/mcp-server/src/`), the Python ingestion/distillation pipelines, `shared/memory_policy.json`, `scripts/run_eval.py`, and `FABLE.md`. Every mechanism claim below carries a `file:line` reference.
**Companion contracts:** Six Divecha implementation contracts in `contracts/*.spec.md` (plus the pre-existing P0 contract from `FABLE.md`). Each design section names its contract. The contracts are the executable half of this spec: hand them to cheaper models in Build Mode (or through the `build-ladder` skill) and the gates — not the model — decide when the work is done.

---

## 1. Phase 0 Diagnosis

### 1.1 One-page verdict

The store is **not sick in the way the brief hypothesized**. It is sick in a more mechanical and more fixable way:

1. **Salience never collapsed — it was never discriminative.** `computeSalience` (`salience.ts:93-173`) is a pure function of a handful of *discrete* inputs: a 3-value confidence that is almost always `"medium"` (0.7), a mention-count boost that is a step function stuck at its first step for most entries, a decay term that is **exactly 1.0 for 4 of the 7 context types** (infinite half-lives, `memory_policy.json:22-25`), and a type-multiplier lookup. Two entries sharing {context type, confidence, mention bucket, same-day `last_seen`} produce **bit-identical** scores; the 4-decimal rounding then makes the ties exact. The 0.48–0.49 band is simply the arithmetic of `0.7 × decay × type_mult × freqBoost(2)`; the 1.0 pins are the `min(1.0, raw)` clamp (`salience.ts:162`), whose basin is wide — many structurally different entries saturate to the same ceiling. Worst of all, the one *continuous, meaningful* signal the formula defines — `retrievalBoost` from `last_accessed` — is **dead code in practice**: the `search` tool never writes `last_accessed` back on a hit (only merge helpers do, `index.ts:703`, `dream.ts:487`). Every entry I read has `access_count: 0`. The system is scoring memories while blind to whether they are ever used.

2. **Consolidation loses to ingestion ~100:1, and the bottleneck is *neither* grading *nor* apply mechanics — it is two composed throughput failures.**
   - **(a) Proposal generation is lexical-only on the automated path.** `runDreamProposal` enables semantic dedup only for manual candidate-filtered runs (`dream.ts:3053`, comment at `:3050-3052`); the nightly path matches **exact normalized titles** (`getDuplicateFingerprint`, `dream.ts:592-598`). Different-title duplicates — the categoryMapping.js cluster, the "PKS architecture" pile — are structurally invisible to it. Live confirmation: every recent run reports `semantic_only_merges: 0, semantic_dedup.enabled: false`. Semantic dedup has run against the corpus **exactly once** (2026-06-08, manual): it found **459 clusters spanning 1,338 entries**, applied 35 merges, aborted on a `verify-memory-full` failure, and was never resumed.
   - **(c′) What lexical matching does find is throttled below the inflow rate.** Recent nightly runs each proposed 92→100→133→136→158→161 duplicate merges — all passing grade — but `SCHEDULED_DREAM_DUPLICATE_MERGE_LIMIT=10` (`index.ts:70-84`) applies only 10/night, holding ~148. Meanwhile the corpus grew **8,701 → 10,291 entries in 48 hours** (~800/day). The candidate backlog is growing monotonically.
   - **The duplicate factory is ingestion itself.** `ke_871c4c235a94` and `ke_f4f7bbfb8411` were distilled from the *same two source files* (curate-my-world's `CLAUDE.md` and `.claude/CLAUDE.md`) on different ingestion days, given different domain names, and each had already absorbed prior duplicates via merge. There is no retrieve-before-admit step anywhere in `ingestion/`; re-encountered knowledge mints a new entry every time. Consolidation is bailing a boat that ingestion keeps drilling.
   - **Grading is not a gate.** `gradeDreamProposalRecord` (`dream.ts:3183-3292`) is deterministic structural validation — ID allowlists, revision counts, rollback metadata present. No semantic check of any kind. The brief worried merges might be "failing the grade gate"; the truth is inverted — nothing semantic can fail it, which is a *safety* gap, not a throughput one (§3.3).

3. **Contested is a fossil state, not a live epistemic state.** `ke_4dbf732e757d` was marked contested daily through April by a detector that fired on *low* similarity between same-domain entries ("materially different views (similarity=0.07)") — a domain-name-collision heuristic, not contradiction detection. The entries it conflicted with were later **merged into it**; its `contradicts` links now point at absorbed entries, including **itself**. Recent runs detect 0 contradictions. The resolution path exists (`mark_contested` enqueues a judge item, `dream.ts:3510-3531`) but depends on `DREAM_OPUS_MODE`, default **off** — the last retier cycle applied 1 verdict and skipped 9. No code path reverts contested→active when counterparts disappear. Result: contested entries sit at tier 1 with salience 1.0, permanently.

4. **The project layer is immortal by construction.** A project's `status` is LLM-assigned once at extraction (`distillation/models/entries.py:584,729`, defaults `"active"`) and **no code path anywhere transitions it**. `active_project` has an infinite half-life (`memory_policy.json:25`) and sits in `identity_floor_context_types` (`:90`), so salience never decays and tier never falls below 2; Dream's archive candidacy requires salience < 0.05 (`dream.ts:1885-1889`) — unreachable. The M9 stale-project audit (`scripts/audit_memory_quality.py:809-878`) *detects* exactly this (90-day staleness threshold) but is detection-only; nothing acts on it. A 2024 one-shot chat tagged `active_project` at extraction is active forever.

5. **Observability is better than the brief assumed — and enforces nothing.** There is a run ledger, a validation-gate status document, and (as of 2026-07-06) a real 8-axis probe suite with 52 enabled probes and a working read-only eval runner. But `compare()` unconditionally returns 0 (`run_eval.py:274-296`), no baseline is committed, reports are gitignored, and the "green" quality gate in `get_validation_status` was last computed **2026-06-08** — when it already showed `duplicate_entries: 849, multi_member_clusters: 228`. Green means "passed a month ago", not "healthy now".

### 1.2 Verdicts on the brief's specific hypotheses

| Brief hypothesis | Verdict | Correction |
|---|---|---|
| ~10.5k topics, 26 projects, ~2,465 at tier 1 | **Confirmed** (10,521 / 26 / 2,488; T2 7,541, T3 518) | — |
| "Ranking *within* tier 1 does all the real work" | **Partly wrong** | Tier is not a retrieval partition — it enters `search` only as a ×1.15/×1.0/×0.85 multiplier (`memory_policy.json:76-78`; comment `index.ts:2708-2711`). All tiers compete on one score. Tier-1 membership matters most in `get_index` (static, tier-sorted, top-100) — and there, membership among thousands of exact ties is decided by **UUID string order** (`retrievalPolicy.ts:76-81`), i.e. randomly. The nightly retier churns ~400 entries on those coin flips. |
| "Scoring function has gone nearly flat / saturated" | **Confirmed, wrong mechanism** | Not drift or saturation over time — degenerate input cardinality from day one, plus a dead usage-feedback term and a wide 1.0 clamp basin (§1.1.1). |
| "Consolidation falling behind ingestion" | **Confirmed, localized differently** | Not "not proposing" *or* "failing grade" *or* "not applying" alone: different-title dups are never proposed (lexical-only nightly); same-title dups are proposed, pass a rubber-stamp grade, and are capped at 10/night vs ~800 new entries/day. Fix must hit **admission** (stop minting dups) + **proposal** (semantic sweep) + **cap** (raise once the merge gate is real). |
| "Contested knowledge injected at tier 1" | **Confirmed, worse** | The contested flags are largely artifacts of a bad April heuristic, are unresolvable as currently wired (judge off by default), and several have dangling/self-referential `contradicts` links. |
| "Project layer polluted by 2024 one-shots" | **Confirmed** | Structural: status is write-once, `active_project` is decay-immune, the stale-project detector is not wired to any action. |
| "Observability is minimal — weekly digest" | **Partly wrong** | Run ledger + gates + new probe suite exist. The real gap: nothing *enforces* — no committed baseline, no failing exit code, stale gate timestamps presented as current green. |

### 1.3 Two latent safety findings (not in the brief)

- **The duplicate-merge path has no protected-context-type check.** Archive candidacy and Layer-2 demotion protect `explicit_save`/`stated_preference`/`professional_identity`, but merge-loser selection (`compareCanonicalPriority`, `dream.ts:623-641`) ignores context type entirely. An explicit-save entry can be absorbed into another entry by an automated nightly merge today. (Fixed in the consolidation contract.)
- **Phase 9 outcome gate is an all-or-nothing kill switch if misconfigured.** If `DREAM_PHASE9_OUTCOME_GATE_ENABLED` is set without probes in Redis, *every* apply fails outright (`dream.ts:3719-3730`). Not visible in `wrangler.json`; confirm actual Worker vars before enabling it as part of §5.

---

## 2. Injection / Ranking Redesign

**Contract:** `contracts/injection-ranking-v2.spec.md` (`PKS-INJECTION-RANKING-002`) — with `contracts/usage-signal-loop.spec.md` (`PKS-USAGE-SIGNAL-001`) as its tiny, ship-first prerequisite.

### 2.1 What "good" looks like

Across the active corpus: salience tie rate at 4 decimals **< 1%** (today: the majority of the corpus ties), scores spread across deciles, and the score *means something* — an entry used weekly scores above a structurally identical entry never retrieved. At query time: the injected set is relevant (probe recall holds), non-redundant (no two near-duplicates in one result set), and fits a token budget instead of a fixed count.

### 2.2 Mechanism — three layers

**Layer 1: Close the usage loop (the prerequisite, ~1 day).** `search` and `get_context` write `last_accessed` / `access_count` back on returned hits — asynchronously (`ctx.waitUntil`), fail-open (a write failure must never affect the read path), bounded (only the ≤ `limit` returned entries, not the 60-candidate pool). This single change turns the formula's existing-but-dead `retrievalBoost` (`salience.ts:147`) into a live, continuous, honest signal; it is also the substrate the eval harness's session-uplift layer needs (§5.3), and the biological analogue is exact — retrieval *is* reconsolidation; memories touched in use should strengthen.

**Layer 2: Salience v2 — decomposed, additive, calibrated.** Replace the multiplicative-clamp scalar with a weighted sum of five persisted components, each in [0,1], stored alongside the final score so every ranking decision is inspectable:

```
salience_v2 = 0.30·usage + 0.25·evidence + 0.20·recency + 0.15·authority + 0.10·corroboration
  usage         = 0.5^(days_since_last_accessed / 60), 0 if never accessed
  evidence      = sat(distinct source_conversations, 6)·0.5 + sat(key_insights, 8)·0.3 + sat(distinct days seen, 10)·0.2
  recency       = 0.5^(days_since_last_seen / half_life[context_type])   — NO infinite half-lives except explicit_save;
                  active_project: 180d, professional_identity/stated_preference: 365d (identity floor keeps tier ≥ 2 regardless)
  authority     = lattice value from asserted_by/assertion_kind (§4.2): arjun decision 1.0 … inferred hypothesis 0.3
  corroboration = sat(mention_count, 20)                                 — made meaningful by admission-dedup (§3.2)
```

Design properties, chosen deliberately:
- **Additive, not multiplicative** — no clamp pile-up at 1.0; a component at zero dampens rather than annihilates, and the ceiling basin shrinks to genuinely-maximal entries.
- **Forgetting as a feature**: finite half-lives everywhere except explicit saves. An identity-class fact the user restates every few months keeps refreshing `last_seen`; one never restated and never retrieved *should* drift down — that is the adaptive forgetting the memory-science lens recommends, with the identity floor (tier ≥ 2) as the safety net, not an infinite score.
- **Tiebreak order** `(salience_v2, last_seen, evidence_count, id)` — UUID order only as the final resort, never the effective decider.
- Percentile tiering (`retrievalPolicy.ts:57-91`) is retained — it is budget-aware by construction — but only becomes meaningful once tie rate is < 1%; the contract gates on that.

**Layer 3: Query-time — diversity- and budget-aware selection.** Keep the current blend (`similarity·0.55 + recency·0.15 + salience·0.30`, `salience.ts:290-304`) and intent routing; change the *selection* from "sort, slice top-K" to greedy marginal-value selection over the 60-candidate pool:

```
pick argmax over remaining candidates of:
    final_score − 0.30 · max_cosine(candidate, already_selected)
subject to: ≤ 2 entries per domain-cluster; Σ est_tokens(selected) ≤ budget (default 3,000; caller-overridable)
stop when budget exhausted or limit reached
```

This is MMR with a per-domain cap and a token meter. The top-1 result is always the raw best match (diversity must never cost the best answer — invariant in the contract). Near-duplicate pile-ups — currently unguarded (`index.ts:2696-2760` has no diversity logic at all) — become structurally impossible, which also buys time while the consolidation backlog drains: even before duplicates are merged, they stop being *co-injected*.

### 2.3 Cheapest validation

- **Shadow mode first** (see §6, stage 2): compute `salience_v2` corpus-wide into a shadow field; a deterministic report script asserts tie rate, decile spread, and Gini vs v1. No behavior change, pure measurement.
- Replay the 52-probe suite in staging with the flag on: recall axes must hold within baseline tolerance; add a redundancy metric (mean pairwise cosine of each result set) that must strictly improve on probes with known duplicate clusters.
- Cost: an embedding-free shadow pass plus one probe replay — no LLM calls, minutes of compute.

---

## 3. Consolidation Redesign — Keep Up, Safely

**Contracts:** `contracts/admission-dedup.spec.md` (`PKS-ADMISSION-DEDUP-001`), `contracts/semantic-consolidation.spec.md` (`PKS-SEMANTIC-CONSOLIDATION-001`), `contracts/project-lifecycle.spec.md` (`PKS-PROJECT-LIFECYCLE-001`).

### 3.1 What "good" looks like

Steady state: duplicate-candidate count per nightly run **declining**, not growing (today: 92→161 and rising); `multi_member_clusters` from the quality audit trending to ~0; no merge ever silently drops a claim; anything ambiguous routes to Arjun, bounded to a few items a week.

### 3.2 Stop manufacturing duplicates: retrieve-before-admit

The single highest-leverage consolidation change is upstream of Dream entirely. At ingestion, before minting an entry, embed the candidate and query the vector store among **active** entries of the same type:

- **top-1 cosine ≥ 0.85** → route as an **evidence-append** to the existing entry: append the new `Evidence`, bump `mention_count`, refresh `last_seen`, add the source conversation. No new entry. (This also makes `corroboration` in salience v2 honest: repetition becomes reinforcement instead of duplication — the exact behavior a memory system *should* have.)
- **0.70 ≤ cosine < 0.85** → create the entry but link it `related` to the neighbor, marking it a future merge candidate.
- **< 0.70** → admit as new, unchanged.
- Contested or archived neighbors are never append targets (route as new; the contradiction path owns those).
- Ships behind a flag with a dry-run mode that logs decisions without writing, so the routing distribution is observable before it goes live.

This converts consolidation from a losing race into residual cleanup. It is the "retrieval before admission" item from the July re-ranked plan, and Phase 0 confirms it attacks the actual duplicate source.

### 3.3 Make Dream's proposals see what humans see: the nightly semantic slice

Reuse the *existing, tested* semantic machinery from the operator path (`buildReplayPlansWithSemantic`, `dream.ts:971`; caps already in policy: `COSINE_DUP_THRESHOLD 0.95`, `SEMANTIC_MAX_CLUSTER_SIZE 6`, `SEMANTIC_DEDUP_MAX_QUERIES 400`) inside the nightly run, bounded by a **persistent rolling cursor**: each night, the next ≤200 entries (by cursor order) get semantic neighbor queries; the cursor advances and wraps, so the full corpus is swept every ~50 nights even at worst case — and far faster once admission-dedup collapses the inflow. Priority order within the sweep: tier-1 entries and largest known clusters first. Subrequest limits are respected by construction (≤400 vector queries/night — the same bound the operator path already enforces).

Raise `SCHEDULED_DREAM_DUPLICATE_MERGE_LIMIT` from 10 → 50 **only after** the merge gate below ships; the cap is currently the only thing standing between a rubber-stamp grade and the corpus, and I would not touch it before the gate is real.

**Close the protection gap:** merge-loser selection must exclude protected context types (`explicit_save`, `stated_preference`, `professional_identity`) from being absorbed without judge/human approval — today `compareCanonicalPriority` (`dream.ts:623-641`) would happily archive an explicit save into a passing reference.

### 3.4 The lossless-merge grading rubric — the hard part

Reconsolidation is reconstructive; the failure mode is silent loss and confabulated gist. The current "grade" cannot catch either. The redesigned gate has three rings: deterministic hard gates (code, always run, cannot be argued with), judged gates (LLM, refuse-by-default), and routing rules (when a human decides). A merge applies only if **all three rings pass**.

**Ring 1 — Hard gates (deterministic, in `dream.ts` apply path):**
- **H1 Provenance conservation.** The merged entry's evidence set equals the union of the parents' evidence sets — checked as multiset equality on `(conversation_id, message_ids)` pairs. Zero tolerance.
- **H2 Metadata monotonicity.** `mention_count' = Σ parents`, `first_seen = min`, `last_seen = max`, `source_conversations = union`. Deterministic recomputation, compared field-by-field.
- **H3 Reversibility.** Losers archived (never deleted) with a merge receipt naming the winner, the operation id, and the pre-merge revisions; `rollbackDreamApply` restores the exact prior state. (Mostly exists — `dream.ts:4155` — the gate makes it a checked postcondition instead of a mechanism that is assumed to work.)
- **H4 Protected types.** No protected-context-type loser without an approval token (§3.3).

**Ring 2 — Judged gates (cheap model, e.g. Haiku/Sonnet, conservative prompt; any FAIL blocks):**
- **J1 Claim coverage (no loss).** Decompose each parent's `current_view` + `key_insights` into atomic claims; for each claim, the judge must point to the specific merged insight or view sentence that entails it. Any orphaned claim → fail. "I can't find it" defaults to fail, not pass.
- **J2 No new claims (no confabulation).** Every claim in the merged `current_view` must be entailed by at least one parent. A merged view that generalizes beyond its parents ("Arjun always uses conventional commits" from parents saying "in repos X and Y") fails J2 — generalization is the *insight-synthesis* pipeline's job, with its own provenance type, never a merge side effect.
- **J3 No contradiction laundering.** If any pair of parent claims conflict, the merge must either represent both as `positions` (with `as_of` and evidence each) or be rejected and routed to the contradiction path (§4). A merge that silently keeps one side fails.

**Ring 3 — Routing to Arjun (auto-apply forbidden when):** any parent is `explicit_save`/`stated_preference`/`professional_identity`; cluster size > 4; any judged gate non-unanimous across its (configurable, default 2) votes; or the entries carry `asserted_by: user` evidence on both sides of a J3 conflict. Routed items land in a weekly digest capped at ~10; everything else auto-applies. This bounds Arjun's attention cost while guaranteeing a human sees exactly the merges where judgment, precedence, or identity is at stake.

**On "lossless" and insight bloat:** today's merge is lossless by *concatenation* — `ke_4dbf732e757d` carries 9 near-identical paraphrased insights. That is not preservation, it is noise with provenance. Under the rubric, an insight may be dropped **iff** another retained insight entails it, and the merge receipt records the mapping (`dropped insight #7 ⊂ retained insight #2`). Information-equivalence, not multiset growth — this is the definition of lossless the gate enforces.

**Insight extraction (gist formation)** — the highest-value proposal type — keeps the just-shipped synthesis pipeline (`pks-dream-insight-synthesis-prd-2026-07-02.md`, live since 07-02, cap 5/run) but hardens it with the same machinery: synthesized insights are typed `derived`, carry member-entry provenance, must pass a J2-style entailment check (each synthesized claim supported by ≥2 members), and **never replace** their episodic members until the insight has survived N=30 days and accrued nonzero usage (§2's usage signal, again). Schema extraction from episodes, with the episodes retained until the schema proves itself — systems consolidation, implemented conservatively.

**Staleness archiving** is currently starved (archive candidacy needs salience < 0.05, unreachable for never-decaying types; recent runs: `archive_candidates: 0`). Replace the trigger with an inactivity definition: tier 3 AND no access AND no mention in 180 days, protected types and contested entries exempt (both already excluded, `dream.ts:1841-1890`). Archive-to-limit 50/night is already in place and fine once candidates exist.

### 3.5 Project lifecycle (small, separate contract)

Wire the existing M9 detector into the governed proposal path: a project with `status: "active"`, `last_touched` > 90 days, and no recent access generates a `project_status_transition` proposal (active→dormant), applied through the same grade/apply/receipt machinery as everything else, reversible via `restore_entry`. `get_index` labels dormant projects instead of presenting 2024 one-shots as live work. Explicitly pinned projects (explicit-save context) are exempt.

### 3.6 Cheapest validation

- Seed a staging corpus with known paraphrase-duplicate fixtures; assert the nightly path proposes semantic merges (`semantic_only_merges > 0`) within bounds.
- Merge gate: a fixture library of ~20 merge cases — clean duplicates, subset merges, lossy merges (a claim deleted), confabulated merges (a claim invented), contradiction-laundering merges — asserting H/J gates pass/fail each correctly. This library *is* the regression suite for consolidation safety forever after.
- Admission dedup: replay a fixed ingestion batch containing known re-observations in dry-run; assert 0 new entries for them and correct routing decisions in the log.

---

## 4. Contradiction Resolution — First-Class Path

**Contract:** `contracts/contradiction-lifecycle.spec.md` (`PKS-CONTRADICTION-LIFECYCLE-001`).

### 4.1 The provenance backbone (prerequisite, cheap, load-bearing for everything)

The schema today discards the one fact the precedence model needs most: **who asserted it**. `message.role` is available throughout extraction (`filter.py:114-125`, `extract.py:155` — explicit-save extraction already requires `role == "user"`) and is then thrown away; `Evidence` persists only `conversation_id + message_ids + snippet` (`entries.py:139-146`). Add to `Evidence` (and surface to entry level as the max over evidence):

```
asserted_by:     user | assistant | inferred      (from message role / extractor provenance)
assertion_kind:  decision | preference | correction | fact | hypothesis
```

New entries populate it at extraction time — near-zero marginal cost since the role is in hand. Backfill for existing entries is possible later by re-fetching `message_ids`, but is deliberately **not** required: the precedence model treats missing as `inferred/hypothesis` (lowest rank), which is the epistemically safe default for unattributed old knowledge.

### 4.2 The precedence lattice (never naive recency)

Two ordered axes, evaluated in order:

```
authority:   arjun_explicit (correction, decision, explicit_save)      rank 4
           > arjun_behavioral (repeated observed behavior across repos) rank 3
           > assistant_asserted (assistant claim, unchallenged)         rank 2
           > inferred_pattern / hypothesis (extractor generalization)   rank 1

durability:  decision > preference > fact (dated, world may change) > hypothesis
```

Resolution rules, in order:
1. **Scope check first** (judge): are the two claims actually about the same scope? The April fossils are the cautionary tale — "uses conventional commits" vs "uses emoji headers" is not a contradiction, it is *per-repo variation*; the correct resolution is a **scope-split rewrite** ("style varies by repo: conventional commits in T2-*, emoji headers in Pattern"), not a winner. The old detector's "same domain, low similarity" heuristic manufactured exactly these false contests.
2. **Higher authority wins** regardless of recency: an Arjun decision from March beats an assistant suggestion from yesterday.
3. **Equal authority → durability, then recency**: a newer Arjun decision supersedes an older one (recorded as an `Evolution` entry — the schema already has the structure, `entries.py:196-204`); a dated *fact* yields to a newer observation.
4. **Behavioral-vs-stated conflict** (Arjun says X, Arjun repeatedly does Y) is the one genuinely interesting case: never auto-resolve. Keep both as `positions`, inject a one-line *contested summary* instead of both raw entries, and surface it in the weekly digest — this is knowledge about Arjun that only Arjun can adjudicate.

### 4.3 Detection, lifecycle, and hygiene

- **Detect at admission**: when retrieve-before-admit (§3.2) finds a high-similarity neighbor, a cheap judged check asks "does the candidate contradict the neighbor?" — contradiction found → create the entry, link `contradicts`, set both to contested *with a receipt*. This catches contradictions at the moment they enter, which is when the context to resolve them is richest.
- **Detect in Dream**: within the nightly semantic slice, opposing-claim detection on high-similarity pairs (same check, batched).
- **Contest receipts**: `state: contested` is only valid alongside `contest_receipt {detected_at, basis, counterpart_ids, resolution_deadline}`. No receipt → not a real contest.
- **Auto-expiry**: nightly, any contested entry whose counterparts are all archived/merged/resolved reverts to `active` with a note. This rule alone clears most of today's fossils.
- **Injection guard**: `search` never returns two entries linked by `contradicts` in one result set without collapsing them into the contested-summary form. Knowingly injecting inconsistent knowledge — the state the brief rightly called worst — becomes impossible at the last line of defense, independent of how far the resolution backlog has drained.
- **One-time backfill sweep** (§6 stage 5): dry-run first; clears dangling/self-referential contests (the `ke_4dbf732e757d` pathology), re-adjudicates surviving real contests through the lattice, routes the ≤ handful of genuine behavioral-vs-stated cases to the digest.

### 4.4 Cheapest validation

A labeled fixture set of ~30 pairs (true contradictions, scope-splits, supersessions, false positives from the April heuristic — real examples are abundant in the store) with expected lattice outcomes; a unit-tested comparator; the probe suite's existing supersession axis extended by ~10 probes asserting that resolved-contest losers stop surfacing and contested summaries surface instead.

---

## 5. The Evaluation Harness — Crown Jewel

**Contract:** `FABLE.md` §3's validated `PKS-RETRIEVAL-REGRESSION-GATE-001` (copied to `contracts/retrieval-regression-gate.spec.md`) is the enforcement keystone; the extensions below build on it.

The harness answers three questions, each with its own gold set, metrics, gate, and cadence. Design principle: **a proposed change ships iff the harness says it helps** — that is what lets Sonnet/Haiku iterate on this system for months without Fable. Everything below is buildable by a cheaper model; the judgment is frozen here and in the gates.

### 5.1 Layer R — Retrieval / injection quality

- **Gold set.** The existing 8-axis, 52-enabled-probe suite (`tests/probes/*.json`) is the seed. Grow to ~150–200 probes: a mining script proposes candidate probes from real session JSONL (queries whose answers demonstrably lived in PKS), stratified across the 8 axes plus two new ones — **redundancy** (queries whose candidate pool contains known near-duplicate clusters; pass = no two results with pairwise cosine > 0.92) and **budget** (pass = result set fits the declared token budget without dropping the top relevant entry). Every mined probe is human-approved in batch before it enters the suite; the suite is versioned in-repo.
- **Metrics.** Per axis as today (recall@k, stale-leak, supersession accuracy, negative precision, paraphrase consistency) plus: nDCG@5 on graded probes, mean pairwise cosine of returned sets, salience tie rate, tier-distribution entropy.
- **Gate.** Commit `tests/baselines/retrieval_baseline.json`; `--fail-on-regression` with per-axis tolerances (recall axes: −0.02 absolute; stale-leak: +0.02; others: no pass→fail probe flips). UNMEASURED (null) axes are skipped, never scored as 0. Runs: (a) CI on every Worker/ingestion PR against **staging**; (b) nightly against prod, read-only, appending to the ledger; (c) as the Phase 9 outcome gate's probe set, so **every Dream apply is bracketed by a before/after replay** — the machinery exists (`phase9OutcomeGate.ts:119-198`), it just needs probes configured and the flag verified (§1.3).

### 5.2 Layer C — Consolidation safety

- **Per-apply invariants** (deterministic, blocking): the H-gates of §3.4, plus corpus-level evidence-count conservation (total evidence across active+archived never decreases), plus the Phase 9 pre/post probe bracket.
- **Nightly corpus health report** (append-only ledger artifact): duplicate-candidate count and trend, semantic-cluster density on a sampled slice, contested count with receipt coverage, salience tie rate, tier entropy, archive/promotion counts. Thresholds turn the report into a gate: e.g. duplicate-candidate trend positive for 7 consecutive nights → nightly Dream flips to dry-run and pages the digest. **Numbers that "look wrong" stop the machine instead of decorating a dashboard.**
- **Sampled merge audits**: each night, N=10 randomly sampled applied merges get the J1/J2 judged checks re-run by a cheap model; any FAIL → automatic rollback of that merge (`rollbackDreamApply`), quarantine of the pair, and a digest item. This is the ongoing spot-check that the lossless gate stays honest against drift in the judge itself.
- **Weekly staging rollback drill**: the existing `staging_e2e` (19-step, rollback included) stays, scheduled rather than ad hoc.

### 5.3 Layer S — End-to-end session uplift

The hardest layer; three mechanisms, cheapest first:

1. **Injection-utility telemetry (continuous, free).** With the usage loop live (§2.2), log which injected entries each session actually *used* — the agent-sessions ingester already parses session JSONL; extend it to check whether injected entry content (entities, distinctive strings) appears in the session's subsequent work. Metric: utilization rate per tier / per context type. A tier-1 entry with 0 utilization over 90 days is mis-tiered by definition — this feedback closes the loop the current system lacks entirely, and it doubles as ground truth for future ranking work.
2. **Counterfactual QA (monthly, ~$5).** A set of ~50 questions answerable *only* from PKS content (mined from entries, spot-checked once). Run a cheap model with and without injected context; an LLM judge scores answer correctness. Uplift = Δaccuracy. This is the direct measurement of "did injected context make the session better," in miniature.
3. **A/B replay on historical prefixes (quarterly or on-demand).** For ~20 gold session prefixes, generate continuations with/without injection and judge groundedness pairwise. Most expensive, run only when a major ranking change ships.

### 5.4 Operating rules

Cadence: CI per-PR (Layer R staging), nightly (R prod read-only + C report + C sampled audits), weekly (staging drill + digest), monthly (counterfactual QA). All metrics obey the repo's no-fake-zero rule — unmeasured is `UNMEASURED`. Every gate is an exit code; nothing depends on a model's self-assessment. Baseline refreshes are deliberate, reviewed commits — never automatic — so "the bar" cannot drift without a human seeing the diff.

---

## 6. Migration / Rollout Plan — staged, reversible, gate-before-advance

Every stage names its rollback lever. No stage begins until the prior stage's gate is green. Live-corpus mutation happens only in stages 4–5, after three stages of pure-additive change have made it observable and safe.

| Stage | What ships | Contract | Gate to advance | Rollback lever |
|---|---|---|---|---|
| **0** | Regression gate: commit baseline, `--fail-on-regression`, wire to CI | `PKS-RETRIEVAL-REGRESSION-GATE-001` | Self-compare exits 0; degraded fixture exits nonzero; CI wired | Revert commit (measurement only) |
| **1** | Usage loop: search/get_context write `last_accessed`/`access_count` async fail-open. Provenance fields (`asserted_by`, `assertion_kind`) on new extractions | `PKS-USAGE-SIGNAL-001`, part of `PKS-CONTRADICTION-LIFECYCLE-001` | 7 nights: nonzero access-write rate, zero read-path errors, probe baseline holds | Env flag off; fields additive, ignored by old code |
| **2** | Salience v2 in **shadow** (`salience_v2` field, live ranking untouched); distribution report | `PKS-INJECTION-RANKING-002` (phase A) | Tie rate < 1%, sane decile spread, report reviewed by Arjun | Shadow field only — delete it |
| **3** | Admission dedup in **dry-run** (log-only), then live behind flag. Ranking v2 cutover (flag) + MMR/budget selection | `PKS-ADMISSION-DEDUP-001`, `PKS-INJECTION-RANKING-002` (phase B) | Dry-run routing distribution reviewed; staging probe replay green incl. new redundancy axis; 7 nights new-entry rate visibly bent | Both flag-off; v1 salience still computed |
| **4** | Consolidation: merge gate rings H+J; nightly semantic slice (cursor); protected-type merge check; THEN merge cap 10→50; backlog drain of the known ~459 clusters in ≤200-entry batches, `verify-memory-full` after each batch, stop-on-fail (the 06-08 abort is the precedent to respect) | `PKS-SEMANTIC-CONSOLIDATION-001` | Merge-fixture library green; each batch verifies; Layer C health report trending down | Per-merge `rollbackDreamApply`; cap back to 10; cursor pause |
| **5** | One-time hygiene sweeps, each dry-run → reviewed → applied: contested backfill (§4.3), project lifecycle transitions (§3.5), staleness-archive trigger fix | `PKS-CONTRADICTION-LIFECYCLE-001`, `PKS-PROJECT-LIFECYCLE-001` | Dry-run lists reviewed by Arjun (bounded: ~50 contested, 26 projects); receipts on every change | `restore_entry`/`restore_archived`; receipts make every change enumerable |
| **6** | Eval harness completion: probe growth to 150+, Phase 9 probe bracket on, Layer C thresholds enforcing, Layer S telemetry + counterfactual QA | extends stage-0 contract | Harness runs green for 14 days as the enforcing authority | Each gate individually disableable; report-only mode |

**Execution model:** each contract goes to a cheaper model in Build Mode (`build-ladder` escalates Sonnet → Opus only on gate failure). Fable's remaining role after tonight: review stage-2's distribution report, stage-3's routing distribution, and the stage-5 dry-run lists — three bounded reviews, everything else is gated code.

---

## 7. The Three Changes With the Highest Uplift-per-Unit-Risk

**1. Close the usage loop (`PKS-USAGE-SIGNAL-001`).** A ~day of work: persist `last_accessed`/`access_count` on retrieval hits, letting the already-written `retrievalBoost` term fire. Risk is as close to zero as changes get — additive metadata, async, fail-open, no ranking change until v2 reads it. Uplift is foundational twice over: it is the *only* continuous, honest signal available to break the salience tie plateau (everything else is discrete or gameable), and it is the substrate for Layer S evaluation and future mis-tier detection. The system currently cannot see whether anything it remembers is ever used; after this, it can. No other single-day change buys that.

**2. Retrieve-before-admit (`PKS-ADMISSION-DEDUP-001`).** The Phase 0 numbers make the case brutally: ~800 entries/day in, 10 merges/day out, and the flagship duplicate cluster was minted by re-distilling the *same two files*. Every downstream investment — semantic sweeps, merge gates, caps — is bailing until admission stops drilling. Risk is modest and controllable: it is flag-gated, dry-run-first, additive (below-threshold admissions unchanged), and its worst failure mode (a wrong append) is recoverable because evidence is append-only with provenance. It also single-handedly turns `mention_count` — today a merge artifact — into a real corroboration signal, which ranking then inherits for free. Repetition becomes reinforcement instead of pollution: one change, three systems improved.

**3. The regression gate (`PKS-RETRIEVAL-REGRESSION-GATE-001`, FABLE P0).** Half a day: commit a baseline, make `--compare` return a real exit code, wire CI. Zero live-store risk — it mutates nothing. Its uplift is of a different *kind* than the other two: it converts every future change to this system, by any model, from "hope" to "provable" — which is precisely the brief's deepest requirement, that cheaper models keep improving the system without Fable. Stages 2–6 of the rollout are only safe *because* this exists; it is the enabling move for the entire program, and it is 90% built already.

(The conspicuous absentee: the semantic consolidation sweep. It is the most *visible* fix but not the best risk-adjusted one — it mutates thousands of entries via LLM-judged merges, its own safety gate must ship first, and its problem shrinks dramatically once change #2 stops the inflow. It is stage 4 for a reason.)

---

## Appendix A — Live evidence snapshot (2026-07-07)

- Index: 10,521 topics, 26 projects, 7,597 archived; tiers 1/2/3 = 2,488 / 7,541 / 518. Last Dream: 2026-07-06T12:27Z.
- Corpus growth (dry-run proposal `total_entries`): 8,701 (07-05 06:20) → 9,646 (07-05 10:52) → 9,779 (07-06 06:20) → 10,199 (07-06 12:29) → 10,291 (07-07 06:20).
- Duplicate-merge candidates per run (all lexical; `semantic_only_merges: 0` every run): 97 → 133 → 136 → 158 → 161. Applied per governed run: 10, 10, 12, 10 (`scheduled_cap_reached:duplicate_merge:10` on the rest). `contradictions_detected: 0`, `archive_candidates: 0–2`, `promotion_candidates: 0` in every recent run.
- 2026-06-08 one-off semantic catch-up: 459 clusters / 1,338 entries found; 35 merges applied; aborted on `verify-memory-full` failure; never resumed (script no longer in repo).
- Quality audit (last run 2026-06-08, still shown green): `duplicate_entries: 849`, `multi_member_clusters: 228`, `tier_1_share: 0.319`, `recall_at_5: 0.95`.
- Entry reads: `ke_4dbf732e757d` (contested, tier 1, salience 1.0, 8 entries merged in, 9 near-duplicate insights, self-referential `contradicts` link, `access_count: 0`); `ke_871c4c235a94` + `ke_f4f7bbfb8411` (same-source duplicate pair, salience 0.4928 / 0.4844, mention_count 2, `access_count: 0` both).
- Probe suite: 8 axes, 57 probes, 52 enabled. `compare()` returns 0 unconditionally (`run_eval.py:274-296`); no committed baseline; `scripts/reports/` gitignored.

## Appendix B — Contract index

| Contract | File | Stage | Size |
|---|---|---|---|
| PKS-RETRIEVAL-REGRESSION-GATE-001 | `contracts/retrieval-regression-gate.spec.md` (from FABLE.md, validated 07-06) | 0 | ~0.5 d |
| PKS-USAGE-SIGNAL-001 | `contracts/usage-signal-loop.spec.md` | 1 | ~1 d |
| PKS-CONTRADICTION-LIFECYCLE-001 | `contracts/contradiction-lifecycle.spec.md` | 1, 5 | ~2 d |
| PKS-INJECTION-RANKING-002 | `contracts/injection-ranking-v2.spec.md` | 2–3 | ~3 d |
| PKS-ADMISSION-DEDUP-001 | `contracts/admission-dedup.spec.md` | 3 | ~2 d |
| PKS-SEMANTIC-CONSOLIDATION-001 | `contracts/semantic-consolidation.spec.md` | 4 | ~3 d |
| PKS-PROJECT-LIFECYCLE-001 | `contracts/project-lifecycle.spec.md` | 5 | ~1 d |
