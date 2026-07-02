# PKS Dream Insight Synthesis — PRD (2026-07-02)

**Status:** implemented on branch `claude/pks-dream-vs-cma-ah46w7`, defaults off
**Motivating analysis:** side-by-side comparison of PKS Dream vs Anthropic CMA "Dreams"
(platform.claude.com/docs/en/managed-agents/dreams)
**Related docs:** `docs/dream-and-forgetting-design-2026-05-17.md`,
`docs/dream-and-forgetting-impl-status.md`

## Background

CMA's dreaming pipeline does two things: it *reorganizes* an existing memory store
(dedup, staleness, contradictions) and it *mines inputs for new insights* to fold into
the output. PKS Dream does the first extensively (duplicate merge, contradiction
contest, promote/demote, quarantine, retier, archive) but has no mechanism that
*creates* knowledge — every Dream operation reshuffles or removes what ingestion
already wrote. Insight synthesis closes that gap.

Design constraint carried over from the 2026-05-17 design: **mostly deterministic,
Opus only for judgment, no human review layer, everything defaults off.**

## What it does

Nightly, when enabled, Dream:

1. **Detects insight opportunities (Worker, deterministic).** Finds clusters of 3–6
   related-but-not-duplicate active knowledge entries — semantic cosine in the
   **insight band `[0.80, 0.90)`**, strictly below the contest-band floor (0.90) so
   this never overlaps dedup/contradiction territory — spanning **≥2 distinct
   domains** (cross-cutting, not same-topic trivia). No LLM involved.
2. **Enqueues them to the existing judge queue** as a new op type
   `insight_synthesis`, capped per run, fingerprinted so the same cluster is never
   re-enqueued within the seen-TTL window.
3. **Opus synthesizes (Mac-side judge, subscription billing).** The judge reads the
   member entries and decides: is there ONE durable, cross-cutting insight here that
   no single entry already states? Bias-to-skip preserved. If apply, the verdict
   **carries the synthesized content** — the first content-bearing verdict type —
   plus a placement decision:
   - `append` — the insight refines one entry; names an `anchor_entry_id` from the
     cluster. Applied via the existing `addInsight` primitive (revision-checked,
     duplicate-insight no-op, idempotent).
   - `create` — the insight is genuinely cross-entry; names a `domain`. Applied via
     the existing `createEntry` primitive as a `recurring_pattern` entry
     (tier-2 default, 90-day half-life) with provenance to the source entries.
4. **Applies next Worker cycle.** Both verdict-consumption sites
   (`runScheduledRetierCycle` — the production nightly path — and the legacy
   `runDreamCycle` loop) handle the new op type. Settled outcomes land in judge
   history and surface in the weekly digest like every other judged op.

## Why this shape (CMA comparison)

| Property | CMA dreaming | PKS insight synthesis |
|---|---|---|
| Insight source | Session transcripts + store | The store itself (cross-entry patterns) |
| Provenance | None per-insight (new store wholesale) | Every insight cites its cluster member IDs |
| Quality gate | Human reviews output store | Conservative Opus rubric, bias-to-skip |
| Blast radius | New store (zero, until adopted) | **Additive-only ops** — no archive/delete/demote |
| Cost | API-billed dream job | Claude CLI subscription credits (existing judge path) |

Mining *session transcripts* (CMA's other input) is deliberately out of scope for v1;
the ingestion pipeline already distills conversations into entries. If entry-level
mining proves valuable, v2 can feed recent conversation snippets into the judge
payload without changing the loop shape.

## Detection algorithm

- Eligible entries: `type == "knowledge"`, not archived, `state == "active"`
  (contested excluded), not quarantined.
- Query budget: top `MAX_QUERIES` (150) eligible entries by salience, each queried
  against the vector index via the same injected-NeighborFn pattern as semantic
  dedup (fail-open health probe; a dead vector store disables the phase for the run).
- Edges kept where `INSIGHT_BAND_MIN ≤ cosine < INSIGHT_BAND_MAX` (0.80–0.90).
- Clusters via the existing `connectedComponents` union-find.
- Cluster is a candidate iff `MIN_CLUSTER_SIZE ≤ size ≤ MAX_CLUSTER_SIZE` (3–6)
  and members span ≥2 distinct domains.
- Fingerprint = sorted member IDs joined; `dream:insight:seen:{fingerprint}` with
  `SEEN_FINGERPRINT_TTL_DAYS` (90) TTL suppresses re-enqueue. Marked at enqueue
  time, so a judged-and-skipped cluster stays quiet for the TTL window.
- Deterministic ordering (by fingerprint) → reproducible runs, stable caps.

## Verdict schema extension

`JudgeVerdict` gains an optional `synthesis` block — only meaningful for
`insight_synthesis` ops with `verdict: "apply"`:

```json
{
  "verdict": "apply",
  "reason": "…",
  "synthesis": {
    "insight_text": "≤500 chars",
    "placement": "append" | "create",
    "anchor_entry_id": "ke_…",   // required for append; must be a cluster member
    "domain": "…"                 // required for create
  }
}
```

Validation is layered: the Mac script refuses to post an apply verdict with an
invalid synthesis block (item stays pending, retried next night); the Worker
`/ops/dream/judge_verdict` endpoint validates shape; the cycle revalidates
semantics (anchor ∈ `target_entry_ids`, text length) before applying and settles
`stale` on any mismatch.

## Safety

- **Additive-only.** The op can only append a `key_insight` or create one new
  tier-2 entry. It cannot archive, merge, demote, or delete anything. Reversal is
  the ordinary paths: entry revision history / `update_entry` for appends,
  `archive_entry` for created entries.
- **Default off.** New env var `DREAM_INSIGHT_MODE` (`off` | `on`), default off —
  fourth kill switch alongside the existing three.
- **Caps.** `PER_RUN_ENQUEUE_CAP` (5) new judge items per night; the judge's
  existing ~25-call nightly budget bounds total Opus work; `MAX_QUERIES` (150)
  bounds Worker subrequests independently of the dedup budget.
- **Bias-to-skip rubric.** "A wrong new memory is worse than a missed insight."
- **Idempotent.** Application is keyed `judge_insight_{op_id}` through the standard
  mutation-result dedup (72h TTL); `addInsight` additionally no-ops on duplicate
  insight text.
- **No mega-clusters.** `MAX_CLUSTER_SIZE` (6) discards oversized components
  outright (the 2026-05-29 sweep's 1980-member lesson).
- **Observability.** Enqueues + verdicts appear in the run record
  (`phases.insight_synthesis`, `judge_queue` blocks) and judge history → weekly
  digest, same as all judged ops.

## Config

New `insight_synthesis` block in `shared/memory_policy.json` (code defaults match):
`INSIGHT_BAND_MIN` 0.80, `INSIGHT_BAND_MAX` 0.90, `MIN_CLUSTER_SIZE` 3,
`MAX_CLUSTER_SIZE` 6, `MAX_QUERIES` 150, `PER_RUN_ENQUEUE_CAP` 5,
`SEEN_FINGERPRINT_TTL_DAYS` 90, `MAX_INSIGHT_CHARS` 500.

## Rollout

1. Deploy Worker (inert — `DREAM_INSIGHT_MODE` unset).
2. One manual shadow pass: `python ingestion/dream_judge/run.py --dry-run` after
   setting `DREAM_INSIGHT_MODE=on` for one night — inspect what clusters get
   detected and what Opus would synthesize, without posting verdicts.
3. Flip live: `echo "on" | npx wrangler secret put DREAM_INSIGHT_MODE`. Watch the
   weekly digest; created entries are visible as `recurring_pattern` entries with
   `dream:insight_synthesis` actor provenance.
4. Retune band/caps from observed cluster quality (same posture as the
   `COSINE_DUP_THRESHOLD` 0.86→0.95 retune).

## Out of scope (v1)

- Session-transcript mining (CMA's other input) — see above.
- Insight synthesis over `project` entries.
- Judge-initiated placement outside the cluster (anchor must be a member).
- Any destructive companion op.
