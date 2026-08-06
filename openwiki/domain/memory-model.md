---
type: "Reference"
title: "Memory model and business logic"
description: "Core memory model: selective autobiographical memory philosophy, salience scoring, forgetting policy, and Dream consolidation lifecycle."
---

# Memory model and business logic

This repository is built around a specific product idea: the system should behave more like selective autobiographical memory than like a write-only archive.

The README explains the philosophy clearly: the goal is to keep durable identity and project context available, allow lower-priority material to stay retrievable when relevant, and let weak signals fade or consolidate instead of competing forever for attention.

## Core memory concepts

### Context types and tiers

The Worker and policy code use a context taxonomy that distinguishes memory by durability and purpose.

The current code paths and docs reference categories such as:

- professional identity
- stated preference
- explicit save
- active project
- recurring pattern
- task query
- passing reference

The Worker uses these classifications to derive salience, injection tier, and search behavior.

### Salience

Salience is not a binary yes/no property. It is a scored combination of:

- recency decay
- mention frequency
- type-specific weighting
- retrieval reinforcement

This is the main mechanism that keeps durable memories visible while allowing weak, one-off facts to become effectively invisible unless reinforced.

A second-generation score, `salience_v2`, is computed as a five-component additive
score (usage, evidence, recency, authority, corroboration) and written into
`metadata.salience_v2` during the nightly Dream pass. It is in shadow phase only:
not consulted by live ranking or tiering until the `RANKING_V2` env flag is on
(Phase B). When enabled, it pairs with
[MMR diversity selection](../architecture/mcp-and-dream.md) to replace the
plain sort-and-slice top-K. The TypeScript `salience_v2.ts` and
`precedence.ts` modules have Python twins in `distillation/utils/` that must stay
semantically identical, enforced by shared fixture tables.

### Retrieval policy shaping

`cloudflare-mcp/mcp-server/src/retrievalPolicy.ts` documents the system's query-time biasing strategy:

- cross-context penalties reduce the score of unrelated memories
- quarantine penalties reduce the score of flagged items
- the penalties are multipliers, not hard filters

That design choice matters product-wise: the system can still return a memory on direct search, but it should not spontaneously surface irrelevant material in a different topic stream.

## Dream as memory governance

Dream is the maintenance loop that changes the memory store over time.

Across the README, PRDs, and Worker code, Dream now serves several business functions:

- consolidate duplicates
- resolve contradictions
- archive stale material
- restore or unarchive entries when needed
- promote durable context
- generate content-bearing insights when a cluster supports one
- verify that changes preserve the memory system's intended behavior

This makes Dream the main governance layer for what becomes durable, what gets demoted, and what gets removed from active visibility.

## Entry identity and revision semantics

Two production behaviors in `cloudflare-mcp/mcp-server/src/dream.ts` are important for anyone touching entry creation or Dream apply/rollback logic.

### Atomic entry ID allocation

`generateEntryId` claims IDs atomically using `redis.setnx` with a sentinel placeholder, then the caller overwrites with the real entry. This closes a TOCTOU window that existed across the four ingestion generators. Entry IDs use 16 hex characters (64 bits), widened from an earlier 12-char (48-bit) scheme to avoid birthday collisions. Knowledge entries are prefixed `ke_` and pattern entries `pe_`.

### Revision is a monotonic concurrency counter

Every Dream apply — including `markEntryContested` — bumps the entry's `revision` field before persisting. Rollback is itself a forward write: it restores content and state from the before-snapshot but never rewinds revision. This means the post-rollback revision is always higher than the pre-apply revision (typically +2 for a single apply+rollback pair). This design ensures conflict detection and rollback validation always see the intervening write.

Tests that assert specific revision values after an apply-then-rollback sequence must account for this monotonic behavior.

## Insight synthesis

Recent history added `insight_synthesis` as a new judge operation type.

The important product idea is that the system can sometimes infer a durable cross-cutting insight from a cluster of related memories, not just decide whether two entries are duplicates.

The code comments say this is a content-bearing verdict path: the judge can return synthesized text in the verdict itself, either appended to an existing entry or created as a new recurring-pattern entry.

This is a meaningful expansion of the system's memory behavior because it moves from preserving observations to actively producing higher-level memory.

## Why this matters to future changes

If you change anything in the memory model, you are probably changing product behavior, not just implementation detail.

Examples:

- changing context-type weights affects what surfaces in conversations
- changing salience logic affects forgetting behavior
- changing Dream caps affects how much maintenance the system can perform nightly
- changing judge queue rules affects which borderline cases get human review
- changing retrieval policy changes the user's lived experience of the assistant

## Main source anchors

- `README.md`
- `cloudflare-mcp/mcp-server/src/salience.ts`
- `cloudflare-mcp/mcp-server/src/salience_v2.ts`
- `cloudflare-mcp/mcp-server/src/retrievalPolicy.ts`
- `cloudflare-mcp/mcp-server/src/mmr.ts`
- `cloudflare-mcp/mcp-server/src/precedence.ts`
- `cloudflare-mcp/mcp-server/src/dream.ts`
- `cloudflare-mcp/mcp-server/src/judgeQueue.ts`
- `distillation/utils/salience_v2.py`
- `distillation/utils/precedence.py`
- `shared/memory_policy.json`
- `docs/dream-and-forgetting-impl-status.md`
- `docs/pks-dream-insight-synthesis-prd-2026-07-02.md`
- `docs/pks-memory-os-v0.4-prd.md`
