# Dream + Forgetting design — SIMPLIFIED VERSION (concepts only)

## Background

Personal knowledge store (Upstash Redis + Vector, MCP server on Cloudflare). Entries have `injection_tier` (1/2/3), `salience_score`, `access_count`, `mention_count`, `context_type`, `updated_at`.

User goals:
- Get rid of duplicates.
- Gradually forget low-salience entries so irrelevant things don't surface in unrelated conversations (canonical case: "Panchakarma" question from 3 months ago surfacing in a quant chat today).

Current state: Dream produces nightly proposals but applies nothing. The user wants action to happen automatically.

## Explicit user direction shaping this design

- "Mostly deterministic with Opus only being called in for border cases — no second opinion."
- No human review at any layer.
- Previous detailed design (with confidence calibration, canary sets, taxonomy versioning, auditor model, multi-phase rollouts) was rejected as too complicated.

## Design

**Core principle**: rules do the bulk of the work. Opus is a narrow tiebreaker on the small set of genuinely ambiguous ops (~5-15 calls/night). No auditor, no canary, no taxonomy migration ceremony, no calibration contracts.

### Layer 0 — Retrieval Policy (query-time, primary fix for Panchakarma)
- Classify query intent at retrieval time (one classifier call, single confidence number).
- If confidence ≥ 0.75 → apply cross-context penalty (coefficient 0.3) per context_type pair from a static penalty matrix.
- If confidence < 0.75 → no penalty (current behavior preserved on uncertain queries).
- That's the entire layer. No bands, no calibration contract, no per-bucket freezes.

### Layer 1 — Sleep ops (auto-apply nightly, deterministic)
- Exact-label duplicate merges (canonical = highest-salience entry, archive the rest).
- Decay archive: Tier 3 + `access_count == 0` + `mention_count == 1` + age >60 days + salience <0.05.
- Cap 50 ops/night. 7-day rollback.

### Layer 2 — Quarantine then demote (auto-apply nightly, deterministic, reversible)
- Salience below tier threshold 3 consecutive nights → quarantine flag (suppresses auto-injection but doesn't change tier; reversible by any access).
- Quarantine sustained 7 more nights → tier demote (2→3, or 1→2 under stricter rule: salience below tier-1 threshold for 14 nights AND zero access in 30 days).
- Tier-3 entry demoted 60+ days → eligible for L1 archive.

### Layer 3 — Border-case judgment (rules first, Opus for ambiguity)

| Op type | Rule: apply if | Rule: skip if | Opus border |
|---|---|---|---|
| Fuzzy duplicate merge | cosine ≥ 0.99 (treat as exact) | cosine < 0.92 | 0.92 ≤ cosine < 0.99 |
| Tier-1 demote (1→2) | salience low 14n AND zero access 30d | otherwise | (none — deterministic) |
| Promote tier | access ≥10 in 30d AND salience > tier threshold | otherwise | mixed signals: rising access but salience middling |
| High-access touch (access_count >5 in 90d) | never auto-apply | | always Opus for this set |

### Layer 4 — Pruning (rules first, Opus for ambiguity)

| Op | Rule: apply if | Rule: skip if | Opus border |
|---|---|---|---|
| Soft-delete | archived >60d AND zero retrieval queries matched | otherwise | (none — bright-line) |
| Hard-delete | soft-deleted >30d AND zero access in past 12 months | otherwise | soft-deleted >30d AND any access in past 12 months |

### Opus call shape (when invoked)

- Given: the proposal (op type, target entry IDs, evidence), both entries' full content, recent access history.
- Returns: binary verdict (apply / skip) + 2-3 sentence reason.
- No confidence score, no defer state, no "operator should see" flag, no auditor pass.
- Model pinned to `claude-opus-4-6`.
- Hard cap: 25 Opus calls/night across L3+L4.

## Kill switches and rollback

Three env vars:
- `DREAM_AUTO_APPLY_MODE` = `off | governed | full` (`governed` is the preferred autonomous path: proposal -> grade -> bounded apply; `full` is the legacy direct cycle)
- `DREAM_OPUS_MODE` = `off | on` (controls L3+L4 border-case routing to Opus)
- `RETRIEVAL_POLICY_MODE` = `off | on` (controls Layer 0)

Each can be flipped independently. All ops (deterministic and Opus-decided) get a 7-day easy-rollback window.

## Rollout

Three flips, one week apart. No phased modes, no exit-criteria gating.

1. **Week 1**: turn on `RETRIEVAL_POLICY_MODE=on`. Read weekly digest. Flip back if anything looks wrong.
2. **Week 2**: turn on `DREAM_AUTO_APPLY_MODE=governed`. L1 proposal operations auto-apply only after deterministic grade, risk, cap, and tripwire checks; L2 stays out of scheduled autonomy until it has proposal/apply operation types.
3. **Week 3**: turn on `DREAM_OPUS_MODE=on`. L3+L4 border cases route to Opus.

## Observability

One artifact: **weekly digest** `scripts/reports/dream-weekly-YYYY-WW.md`. Sections:
- L1+L2 auto-applied this week (count, sample of 5)
- L3+L4 Opus decisions this week (every one, with Opus's reasoning)
- Rollback-able window: ops eligible for rollback now
- Anomaly tripwires fired this week (if any)
- Anything that errored

If the operator doesn't read the digest, the system still works — they've opted into not knowing.

## Anomaly tripwires (the one safety scaffold)

The risk that justifies this: **silent over-pruning compounding**. Layer 0 suppresses an entry → access signals stop → salience decay accelerates → entry demotes → archives → hard-deletes. By the time the operator notices, valuable knowledge is gone. To bound this without adding human review, two machine tripwires watch the destructive side of the pipeline and auto-flip kill switches when anomalies appear.

### Tripwire 1 — destructive action volume spike
- **Counter**: count of (L1 archives + L4 soft-deletes + L4 hard-deletes) applied per day.
- **Baseline**: trailing 14-day median.
- **Trigger**: daily count exceeds 3× the 14-day median for 2 consecutive days.
- **Action**: auto-set `DREAM_AUTO_APPLY_MODE=off` and log alert in next digest.
- **Reset**: operator manually flips the env var back when they've investigated.

### Tripwire 2 — retrieval-hit collapse
- **Counter**: fraction of MCP retrieval queries returning ≥1 entry above the Layer 0 injection threshold, per day.
- **Baseline**: trailing 14-day median.
- **Trigger**: daily hit rate falls >30% below 14-day baseline for 2 consecutive days.
- **Action**: auto-set `RETRIEVAL_POLICY_MODE=off` and log alert in next digest. (Hit collapse implies Layer 0 is over-suppressing — disable it first; if rate doesn't recover within 3 days post-disable, escalate by also disabling `DREAM_AUTO_APPLY_MODE`.)
- **Reset**: operator manually flips back after investigating.

### Hard-delete extra rule (the irreversible step)
- Hard-delete count specifically capped at **5 per day** regardless of tripwires. Anything beyond that defers to the next day, even if the rules say apply.
- If hard-delete cap is hit 3 days in a row, treat that as a tripwire event (auto-set `DREAM_AUTO_APPLY_MODE=off`).

These are the only automated safety rails. Two threshold-based flips, both reversible, no human in the loop, no per-night ceremony. They sit dormant unless something is genuinely going wrong, in which case they protect against the compounding silent-failure mode without requiring the operator to be vigilant.

## Mapping to user goals

| Goal | Mechanism |
|---|---|
| Get rid of duplicates | L1 exact-match auto + L3 fuzzy via Opus when borderline |
| Forget low-salience gradually | L2 quarantine → tier demote → L1 archive → L4 soft-delete → L4 hard-delete |
| Don't surface irrelevant things | PRIMARY: Layer 0 (immediate, query-time). SECONDARY: L2 demotion (gradual). |

---

## Questions for your review

CONCEPTS only — do NOT critique implementation, types, or code structure.

The user previously had a more elaborate version of this design that you (Codex) approved after 5 rounds — that one included confidence calibration contracts, canary query sets with smoothing, taxonomy versioning with migration maps, suppression-plane precedence, auditor models for the LLM judge, and multi-phase rollouts with exit-criteria gates. The user pushed back: "way too complicated, mostly deterministic with Opus only for border cases, no second opinion." This is the stripped-down result.

1. Does this simplified design still meet the user's two stated goals (duplicates gone, irrelevant things stop surfacing)?

2. The previous design had safety scaffolding for: classifier drift (calibration contract), canary metric flapping (smoothing rules), taxonomy schema changes (migration maps), LLM judge failure modes (auditor). All gone. What's the worst-case failure mode that's now exposed, and is it actually catastrophic or just annoying?

3. Is the Layer 3 / Layer 4 border-case carve-out (rules handle clear cases, Opus handles narrow ambiguous middle) the right division of labor, or does it produce blind spots?

4. Is the "if the operator doesn't read the weekly digest, they've opted into not knowing" stance defensible, or is there a minimum forcing function still required?

5. Anything in the simplification that crosses from "acceptably risky" into "actually dangerous"?

Be skeptical and direct. The user explicitly rejected the more elaborate design — don't try to push them back to it. But if there's a single critical safety scaffold that you think can't be dropped, name it explicitly. End with VERDICT: APPROVED or VERDICT: REVISE.
