---
type: "Reference"
title: "OpenWiki quickstart"
description: "Entry point for the personal-knowledge-system wiki. Covers the source-first memory serving model, its rebuild workflow, the legacy Dream governance and nightly orchestration paths, ingestion, and testing."
---

# OpenWiki quickstart

This repository is a personal knowledge system for an individual operator. It ingests conversations, code-repo context, email, and other source material; distills durable memories; stores them in Upstash Redis and Upstash Vector; and serves retrieval and maintenance actions through a Cloudflare-hosted MCP server.

> **Production cutover (2026-08):** serving is now **source-first memory**.
> The self-modifying memory model (salience, retrieval policy, Dream
> consolidation) is retired from production and remains only in staging and
> the codebase. The nightly semantic-maintenance and sleep-report crons are
> retired (manual `workflow_dispatch` only); the only scheduled maintenance
> job is the daily source-first rebuild. See
> [Source-first memory model](domain/source-first-memory.md) and
> [Source-first rebuild workflow](workflows/source-first-rebuild.md).

The repo now has three big moving parts:

- **Source-first build and serving** in Python under `ingestion/source_first/` and TypeScript under `cloudflare-mcp/mcp-server/src/sourceFirst.ts` — the production path
- **Legacy Dream control plane** in Cloudflare Worker TypeScript under `cloudflare-mcp/mcp-server/` (staging only)
- **Nightly orchestration** in Python under `orchestrator/` with shell wrappers in `scripts/` (staging/legacy)

This OpenWiki is a map of the current codebase, not a mirror of every file. It is meant to help both humans and future agents find the right subsystem quickly, understand the system boundaries, and avoid editing the wrong runtime.

## Start here

- [Architecture overview](architecture/overview.md)
- [Source-first memory model](domain/source-first-memory.md)
- [Source-first rebuild workflow](workflows/source-first-rebuild.md)
- [Ingestion and distillation workflow](workflows/ingestion-and-distillation.md)
- [Nightly orchestrator workflow](workflows/nightly-orchestration.md)
- [Cloudflare MCP and Dream control plane](architecture/mcp-and-dream.md)
- [Operations and local workflow](operations.md)
- [Testing guidance](testing.md)

## What this repo does

At a high level, the production system follows this loop:

1. Scan authoritative source files (README, PRD, FABLE, ARJUN, etc.) from Dropbox project folders and pinned global files.
2. Chunk, checksum, and embed them into an immutable evidence generation.
3. Atomically publish the generation to Upstash Vector (namespaced) and Redis (generation-scoped keys) after strict completeness verification.
4. Serve retrieval through the Worker MCP server with a fixed, transparent score (semantic + lexical + authority + recency) and exact provenance.
5. Rebuild daily from source so the index tracks real project activity; a failed build cannot replace the last working generation.

The legacy/staging loop (Dream consolidation, salience, forgetting) is documented in [Cloudflare MCP and Dream control plane](architecture/mcp-and-dream.md) and [Memory model and business logic](domain/memory-model.md).

The main README describes the memory philosophy in more detail, especially the emphasis on selective forgetting rather than permanent retention of every weak signal.

## Major areas

### Source-first build (production)

`ingestion/source_first/` is the Python build pipeline that scans authoritative source files, chunks and checksums them, embeds them with OpenAI `text-embedding-3-large` (3072-dim), and atomically publishes a generation. `scripts/source_first_rebuild.py` is the CLI entrypoint. See [Source-first rebuild workflow](workflows/source-first-rebuild.md).

### Source-first serving (production)

`cloudflare-mcp/mcp-server/src/sourceFirst.ts` is the Worker read path. When `SOURCE_FIRST_MODE === "on"` (production default in `wrangler.json`), `get_index`, `get_context`, `get_deep`, and `search` dispatch here and never write access signals or touch the legacy ranking path. See [Source-first memory model](domain/source-first-memory.md).

### Ingestion (legacy/sources)

Python ingestion code lives under `ingestion/`. The shared config is in `ingestion/core/config.py`; source-specific pipelines include GitHub, Gmail, Twitter/X, and legacy agent-session backfills. These feed the legacy memory model, not the source-first index.

### Distillation (legacy)

`distillation/` contains the older export-processing pipeline and storage/model helpers. It also houses the Python twins (`salience_v2.py`, `precedence.py`) of the legacy Worker ranking modules.

### Legacy Dream control plane (staging)

`cloudflare-mcp/mcp-server/` contains the legacy retrieval policy, salience scoring, Dream lifecycle code, judge queue logic, scheduled async Dream support, and the read/write tool surface. In staging (`SOURCE_FIRST_MODE: "off"`, `DREAM_QUEUE_MODE: "live"`) this path is still exercised; in production it is bypassed by source-first. See [Cloudflare MCP and Dream control plane](architecture/mcp-and-dream.md).

### Legacy MCP server

`mcp-server/` is the older Vercel-style TypeScript implementation. It exists for historical context and migration comparison.

### Nightly orchestrator (staging/legacy)

`orchestrator/` contains the Python state machine that coordinates the legacy nightly Dream run, handles run identity, locking, ledgering, resume/report flows, and the launchd supervision window. `scripts/nightly_orchestrator.py` is the thin CLI entrypoint. See [Nightly orchestrator workflow](workflows/nightly-orchestration.md).

### Operations and scripts

`scripts/` contains operational helpers for launchd, staging, audits, backfills, validation checks, report generation, and the source-first rebuild.

## Repository map for future edits

When changing code, start in the subsystem that owns the behavior:

- **Source-first build/scanner/publisher:** `ingestion/source_first/scanner.py`, `ingestion/source_first/publisher.py`, `ingestion/source_first/models.py`, `scripts/source_first_rebuild.py`
- **Source-first serving/scoring:** `cloudflare-mcp/mcp-server/src/sourceFirst.ts`, `cloudflare-mcp/mcp-server/src/index.ts` (the `SOURCE_FIRST_MODE === "on"` branches on `get_index`, `get_context`, `get_deep`, `search`)
- **Source-first config/policy:** `shared/source_first_config.json`, `shared/source_first_suppressions.json`, `shared/source_first_curated_memory.json`
- **Source-first rebuild CI:** `.github/workflows/source-first-rebuild.yml`
- **Legacy retrieval or memory policy (staging):** `cloudflare-mcp/mcp-server/src/index.ts`, `salience.ts`, `salience_v2.ts`, `retrievalPolicy.ts`, `mmr.ts`, `precedence.ts`
- **Legacy Dream mutation logic (staging):** `cloudflare-mcp/mcp-server/src/dream.ts`, `judgeQueue.ts`, `phase9OutcomeGate.ts`
- **Legacy semantic candidate guard or lossless merge gates (staging):** `cloudflare-mcp/mcp-server/src/dream.ts` (`processSemanticCandidateTask`), `semanticMaintenance.ts`, `mergeGates.ts`, `semanticCursor.ts`, `maintenanceJournal.ts`, `maintenanceQueue.ts` — see [Cloudflare MCP and Dream control plane](architecture/mcp-and-dream.md)
- **Legacy nightly scheduling and run control (staging):** `orchestrator/engine.py`, `orchestrator/stages.py`, `orchestrator/dream.py`
- **Launchd behavior (staging):** `scripts/run_orchestrator_launchd.sh`, `scripts/install_orchestrator_launchd_shadow.sh`
- **CI and remote scheduling:** `.github/workflows/` (source-first-rebuild, worker-runtime-tests, agent-session/github/twitter ingestion, retired nightly-semantic-maintenance, retired nightly-sleep-report) — see [Operations and local workflow](operations.md)
- **Validation and regression checks:** `tests/python/` (including `tests/python/test_source_first.py`), `tests/probes/`, `cloudflare-mcp/mcp-server/test/` (including `test/sourceFirst.test.ts`), `orchestrator/tests/`, `scripts/run_eval.py`, `Makefile` targets `worker-typecheck` / `worker-test`

## Important cautions

- **Production serves source-first memory.** Check `wrangler.json` (`SOURCE_FIRST_MODE: "on"` in top-level `vars`) before assuming legacy ranking or Dream behavior is live. Staging keeps the legacy path (`SOURCE_FIRST_MODE: "off"`, `DREAM_QUEUE_MODE: "live"`).
- **The nightly semantic-maintenance and sleep-report crons are retired.** They are manual `workflow_dispatch` only. The only scheduled maintenance job is `source-first-rebuild.yml` (`cron: "30 6 * * *"`).
- **The Cloudflare Worker has no Dream cron.** `wrangler.json` top-level `triggers.crons: []`. The legacy `07:10 UTC` Dream trigger is gone.
- There are **two MCP implementations**. Do not assume the root `mcp-server/` directory is the production path.
- Many paths and environment defaults are personal and machine-specific (e.g. Dropbox source roots in `shared/source_first_config.json`). Avoid normalizing them unless the task explicitly asks for portability work.
- Some docs in `docs/` and the README describe earlier or aspirational designs (e.g. the multi-scheduler model). Use source code and `wrangler.json` to confirm current behavior.
- Several legacy operations are gated by explicit policy flags and safety thresholds. Before changing live staging behavior, inspect the relevant tests.

## If you are editing something

Use the page that matches the subsystem:

- Source-first build or scoring: [Source-first memory model](domain/source-first-memory.md) and [Source-first rebuild workflow](workflows/source-first-rebuild.md)
- Ingestion behavior (legacy sources): [Ingestion and distillation workflow](workflows/ingestion-and-distillation.md)
- Nightly run control (staging/legacy): [Nightly orchestrator workflow](workflows/nightly-orchestration.md)
- Legacy retrieval and Dream (staging): [Cloudflare MCP and Dream control plane](architecture/mcp-and-dream.md)
- Tests and checks: [Testing guidance](testing.md)

If you are unsure, read the architecture overview first, then jump to the subsystem page that owns the code you plan to touch.

## Backlog

No outstanding documentation backlog. The source-first memory model and
rebuild workflow are documented in
[Source-first memory model](domain/source-first-memory.md) and
[Source-first rebuild workflow](workflows/source-first-rebuild.md). The
legacy semantic consolidation modules (`mergeGates.ts`, `semanticCursor.ts`,
`semanticMaintenance.ts`, `maintenanceJournal.ts`, `maintenanceQueue.ts`)
and the stale-vector-row candidate guard are documented in
[MCP and Dream control plane](architecture/mcp-and-dream.md), along with the
salience v2 / MMR / `RANKING_V2` ranking path, the precedence lattice, and the
tripwire kill switches. The Mac-side Opus judge client
(`ingestion/dream_judge/run.py`) is documented in
[Ingestion and distillation workflow](workflows/ingestion-and-distillation.md),
and the GitHub Actions automation surface is documented in
[Operations and local workflow](operations.md).
