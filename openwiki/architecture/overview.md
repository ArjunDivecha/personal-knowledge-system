---
type: "Reference"
title: "Architecture overview"
description: "High-level architecture of the personal knowledge system: ingestion, distillation, Redis/Vector storage, Cloudflare MCP retrieval, and Dream governance."
---

# Architecture overview

The repository implements a personal knowledge system that turns raw personal and project evidence into a searchable memory store.

The production serving model is **source-first memory**: immutable, source-derived evidence generations ranked by a fixed transparent score, with no LLM-inferred identity, salience, or overnight corpus mutation. The earlier self-modifying memory model (Dream governance, salience scoring, retrieval policy) remains in the codebase as the staging/legacy path.

The architectural shape is intentionally asymmetric:

- **Python source-first build** is responsible for scanning authoritative source documents, chunking and embedding them, and atomically publishing a verified generation.
- **Cloudflare Worker TypeScript** is responsible for serving the source-first read path in production, and still owns the legacy Dream/retrieval-policy/salience machinery used in staging.
- **Python orchestration** coordinates the legacy nightly Dream workflow (shadow/staging) and writes durable reports/ledger state.

## End-to-end flow (production, source-first)

A simplified view of the current production path is:

1. The `Source-First Memory Rebuild` GitHub Actions workflow scans authoritative source files on the self-hosted macOS runner.
2. The Python scanner chunks, normalizes, and checksums each file into immutable evidence records.
3. The publisher embeds new/changed chunks and writes a complete candidate generation into its own Upstash Vector namespace and generation-scoped Redis keys.
4. After strict count verification, the publisher atomically swaps `sf:current_generation`.
5. The Worker MCP server serves `get_index`, `get_context`, `get_deep`, and `search` from the active generation, ranking by a fixed `0.70 semantic + 0.15 lexical + 0.10 authority + 0.05 recency` score.

See [Source-first memory model](../domain/source-first-memory.md) and [Source-first rebuild workflow](../workflows/source-first-rebuild.md) for the canonical detail.

## Legacy path (staging)

The self-modifying model is still present and exercisable in staging:

1. Raw sources are collected from conversation exports, GitHub repo context, Gmail exports, and related agent-session artifacts.
2. The ingestion layer extracts structured entries with provenance and source-specific limits.
3. Storage writes canonical state to Upstash Redis and embeddings to Upstash Vector.
4. The Worker MCP server serves retrieval tools and writes.
5. Retrieval applies query-time policy shaping, salience weighting, and source-aware ranking.
6. Dream maintenance consolidates, archives, restores, and can generate content-bearing insight synthesis when enabled.
7. The nightly orchestrator coordinates scheduled runs and writes durable reports/ledger state.

The staging Worker environment keeps `SOURCE_FIRST_MODE: "off"` and
`DREAM_QUEUE_MODE: "live"` so this path can still be validated. Production
sets `SOURCE_FIRST_MODE: "on"` and has no Dream cron. See
[Cloudflare MCP and Dream control plane](mcp-and-dream.md) for the legacy
control plane.

The top-level README describes the memory philosophy behind this design: the system prefers selective retention and gradual consolidation rather than keeping every weak trace equally visible.

## Major runtime boundaries

### Source-first build and serving (production)

`ingestion/source_first/` is the Python build side: scanner, publisher, and
models. It turns authoritative source documents into immutable evidence
records and atomically publishes verified generations. The rebuild is
driven by `scripts/source_first_rebuild.py` and the
`Source-First Memory Rebuild` GitHub Actions workflow. See
[Source-first rebuild workflow](../workflows/source-first-rebuild.md).

`cloudflare-mcp/mcp-server/src/sourceFirst.ts` is the Worker read side:
when `SOURCE_FIRST_MODE === "on"` (the production default in `wrangler.json`),
the `get_index`, `get_context`, `get_deep`, and `search` tools dispatch to
it. See [Source-first memory model](../domain/source-first-memory.md).

### Ingestion and distillation (legacy evidence collection)

`ingestion/` and `distillation/` are Python-heavy. They own environment loading, API clients, source-specific extraction logic, checkpointing, and the older export-processing pipeline. These pipelines fed the legacy self-modifying memory; they are not part of the source-first build path, which scans source documents directly.

Relevant evidence:

- `ingestion/core/config.py` loads environment variables, defines source limits, and validates per-source requirements.
- `AGENTS.md` identifies the active ingestion sources and warns that the repo depends on external credentials and local paths.
- Recent git history shows repeated hardening of ingestion SDK guards and nightly source-specific jobs.

### Production MCP server and Dream control plane (staging/legacy)

`cloudflare-mcp/mcp-server/` is the Worker implementation. In production it
serves the source-first read path. In staging it still exercises the legacy
Dream/retrieval-policy/salience control plane. It uses `agents/mcp`,
Upstash Redis, Upstash Vector, and OpenAI embeddings.

Its legacy responsibilities (active in staging) include:

- applying retrieval policy, salience ranking (v1 and shadow v2), and optional MMR diversity selection behind the `RANKING_V2` flag
- managing Dream proposals, grading, apply, rollback, and verification
- handling judge queue items
- exposing scheduled async Dream behavior
- atomic-commit maintenance journal and queue-backed semantic consolidation
- tripwire-based kill switches for Dream auto-apply, retrieval policy, and ranking v2
- exposing OpenAI-compatible read surfaces

See [Cloudflare MCP and Dream control plane](mcp-and-dream.md) for the full
legacy control plane. The server distinguishes read-only tools from
mutating tools, and the write surface is gated by auth/scopes and policy
checks.

### Nightly orchestrator (staging/legacy)

`orchestrator/` is the Python control plane for the legacy nightly Dream
run. It uses:

- a fencing lock to avoid duplicate runs
- a run ledger to persist stage state
- stage executors to model the run as a state machine
- report rendering to emit JSON and Markdown artifacts every run
- a launchd supervision mode for Phase 4 sidecar operation

The orchestrator is designed to be injectable and testable. The engine code makes the run identity, clock, sleep function, dream client, and backend pluggable so tests can drive the state machine without reaching live services.

After the source-first cutover, production serving no longer depends on the
orchestrator or Dream mutation. The orchestrator remains for staging
validation of the legacy path. See
[Nightly orchestration workflow](../workflows/nightly-orchestration.md).

## Current vs legacy MCP implementations

There are two TypeScript MCP implementations in the repo:

- `cloudflare-mcp/mcp-server/` — the Worker implementation. In production it serves the source-first read path; in staging it exercises the legacy Dream/retrieval-policy control plane.
- `mcp-server/` — the older Vercel-style implementation, retained for historical context.

`AGENTS.md` explicitly warns that the legacy server can mislead operators if treated as the live system. If a task concerns current retrieval behavior, inspect the Cloudflare Worker server first and check whether `SOURCE_FIRST_MODE` is on.

## Why the architecture is structured this way

The design separates three concerns that should not be mixed casually:

- **Evidence collection** should be source-specific and idempotent. Source-first makes it immutable and checksum-stable.
- **Serving** should be fast, transparent, and free of hidden state. The fixed source-first score has no access reinforcement or LLM-inferred identity.
- **Lifecycle mutation** (the legacy path) should be explicit, auditable, bounded, and reversible. It is now confined to staging.

That separation shows up in the codebase as Python source-first build, Worker source-first serving, and the legacy Python orchestration + Dream control plane.

## Main source anchors

- `README.md` — philosophy and system model
- `AGENTS.md` — repository operating notes and subsystem map
- `docs/source-first-memory.md` — source-first design intent and operator contract
- `ingestion/source_first/scanner.py` — source scanning and evidence chunking
- `ingestion/source_first/publisher.py` — atomic generation publish and verify
- `cloudflare-mcp/mcp-server/src/sourceFirst.ts` — Worker source-first read path
- `cloudflare-mcp/mcp-server/wrangler.json` — `SOURCE_FIRST_MODE` and cron config
- `cloudflare-mcp/mcp-server/src/index.ts` — main MCP server and tool wiring
- `cloudflare-mcp/mcp-server/src/dream.ts` — legacy Dream and mutation logic
- `orchestrator/engine.py` — legacy nightly run state machine
- `orchestrator/stages.py` — stage executors and shadow behavior
