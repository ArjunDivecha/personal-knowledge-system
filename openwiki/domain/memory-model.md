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
- `cloudflare-mcp/mcp-server/src/retrievalPolicy.ts`
- `cloudflare-mcp/mcp-server/src/dream.ts`
- `cloudflare-mcp/mcp-server/src/judgeQueue.ts`
- `docs/dream-and-forgetting-impl-status.md`
- `docs/pks-dream-insight-synthesis-prd-2026-07-02.md`
- `docs/pks-memory-os-v0.4-prd.md`
