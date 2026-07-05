# Architecture overview

The repository implements a personal knowledge system that turns raw personal and project evidence into a searchable, governed memory store.

The architectural shape is intentionally asymmetric:

- **Python ingestion/distillation** is responsible for getting evidence in and normalizing it.
- **Cloudflare Worker TypeScript** is responsible for serving retrieval, applying policy, and running Dream governance.
- **Python orchestration** is responsible for nightly coordination, scheduling, reporting, and resume semantics.

## End-to-end flow

A simplified view of the system is:

1. Raw sources are collected from conversation exports, GitHub repo context, Gmail exports, and related agent-session artifacts.
2. The ingestion layer extracts structured entries with provenance and source-specific limits.
3. Storage writes canonical state to Upstash Redis and embeddings to Upstash Vector.
4. The Worker MCP server serves retrieval tools and writes.
5. Retrieval applies query-time policy shaping, salience weighting, and source-aware ranking.
6. Dream maintenance consolidates, archives, restores, and can generate content-bearing insight synthesis when enabled.
7. The nightly orchestrator coordinates scheduled runs and writes durable reports/ledger state.

The top-level README describes the memory philosophy behind this design: the system prefers selective retention and gradual consolidation rather than keeping every weak trace equally visible.

## Major runtime boundaries

### Ingestion and distillation

`ingestion/` and `distillation/` are Python-heavy. They own environment loading, API clients, source-specific extraction logic, checkpointing, and the older export-processing pipeline.

Relevant evidence:

- `ingestion/core/config.py` loads environment variables, defines source limits, and validates per-source requirements.
- `AGENTS.md` identifies the active ingestion sources and warns that the repo depends on external credentials and local paths.
- Recent git history shows repeated hardening of ingestion SDK guards and nightly source-specific jobs.

### Production MCP server and Dream control plane

`cloudflare-mcp/mcp-server/` is the current production runtime. It is a Cloudflare Worker implementation using `agents/mcp`, Upstash Redis, Upstash Vector, and OpenAI embeddings.

Its responsibilities include:

- serving MCP retrieval tools
- applying retrieval policy and salience ranking
- managing Dream proposals, grading, apply, rollback, and verification
- handling judge queue items
- exposing scheduled async Dream behavior
- exposing OpenAI-compatible read surfaces

The server distinguishes read-only tools from mutating tools, and the write surface is gated by auth/scopes and policy checks.

### Nightly orchestrator

`orchestrator/` is the Python control plane for nightly runs. It uses:

- a fencing lock to avoid duplicate runs
- a run ledger to persist stage state
- stage executors to model the run as a state machine
- report rendering to emit JSON and Markdown artifacts every run
- a launchd supervision mode for Phase 4 sidecar operation

The orchestrator is designed to be injectable and testable. The engine code makes the run identity, clock, sleep function, dream client, and backend pluggable so tests can drive the state machine without reaching live services.

## Current vs legacy MCP implementations

There are two TypeScript MCP implementations in the repo:

- `cloudflare-mcp/mcp-server/` — current production path
- `mcp-server/` — legacy Vercel-style implementation

`AGENTS.md` explicitly warns that the legacy server can mislead operators if treated as the live system. If a task concerns current retrieval or Dream behavior, inspect the Cloudflare Worker server first.

## Why the architecture is structured this way

The design separates three concerns that should not be mixed casually:

- **Evidence collection** should be source-specific and idempotent.
- **Ranking and retrieval** should be fast, policy-driven, and safe for operator-facing queries.
- **Lifecycle mutation** should be explicit, auditable, bounded, and reversible.

That separation shows up in the codebase as Python ingestion, Worker retrieval/mutation, and Python orchestration.

## Main source anchors

- `README.md` — philosophy and system model
- `AGENTS.md` — repository operating notes and subsystem map
- `ingestion/core/config.py` — ingestion configuration and source limits
- `cloudflare-mcp/mcp-server/src/index.ts` — main MCP server and tool wiring
- `cloudflare-mcp/mcp-server/src/dream.ts` — Dream and mutation logic
- `orchestrator/engine.py` — nightly run state machine
- `orchestrator/stages.py` — stage executors and shadow behavior
