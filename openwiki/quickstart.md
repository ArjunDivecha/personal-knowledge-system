---
type: "Reference"
title: "OpenWiki quickstart"
description: "Entry point for the personal-knowledge-system wiki. Covers ingestion, distillation, MCP retrieval, Dream governance, nightly orchestration, and testing."
---

# OpenWiki quickstart

This repository is a personal knowledge system for an individual operator. It ingests conversations, code-repo context, email, and other source material; distills durable memories; stores them in Upstash Redis and Upstash Vector; and serves retrieval and maintenance actions through a Cloudflare-hosted MCP server.

The repo now has three big moving parts:

- **Ingestion and distillation** in Python under `ingestion/` and `distillation/`
- **Production retrieval and Dream governance** in Cloudflare Worker TypeScript under `cloudflare-mcp/mcp-server/`
- **Nightly orchestration** in Python under `orchestrator/` with shell wrappers in `scripts/`

This OpenWiki is a map of the current codebase, not a mirror of every file. It is meant to help both humans and future agents find the right subsystem quickly, understand the system boundaries, and avoid editing the wrong runtime.

## Start here

- [Architecture overview](architecture/overview.md)
- [Ingestion and distillation workflow](workflows/ingestion-and-distillation.md)
- [Nightly orchestrator workflow](workflows/nightly-orchestration.md)
- [Cloudflare MCP and Dream control plane](architecture/mcp-and-dream.md)
- [Operations and local workflow](operations.md)
- [Testing guidance](testing.md)

## What this repo does

At a high level, the system follows this loop:

1. Collect raw evidence from source systems.
2. Distill that evidence into structured memory entries.
3. Store the canonical state in Redis and embeddings in Vector.
4. Retrieve relevant memories through an MCP server with query-time ranking and policy shaping.
5. Periodically run Dream-style maintenance to consolidate, archive, reconcile, and occasionally surface synthesized insights.
6. Drive the whole process with a nightly orchestrator that records every stage and produces durable reports.

The main README describes the memory philosophy in more detail, especially the emphasis on selective forgetting rather than permanent retention of every weak signal.

## Major areas

### Ingestion

Python ingestion code lives under `ingestion/`. The shared config is in `ingestion/core/config.py`; source-specific pipelines include GitHub, Gmail, Twitter/X, and legacy agent-session backfills. This is where source limits, environment loading, and checkpointing behavior are defined.

### Distillation

`distillation/` contains the older export-processing pipeline and storage/model helpers. Some docs and tests still rely on this area, so treat it as active historical infrastructure rather than dead code.

### Production MCP server

`cloudflare-mcp/mcp-server/` is the current Worker-based MCP implementation and the production path. It contains retrieval policy, salience scoring, Dream lifecycle code, judge queue logic, scheduled async Dream support, and the read/write tool surface.

### Legacy MCP server

`mcp-server/` is the older Vercel-style TypeScript implementation. It exists for historical context and migration comparison, but the Cloudflare Worker server is the one to inspect first for live behavior.

### Nightly orchestrator

`orchestrator/` contains the Python state machine that coordinates nightly runs, handles run identity, locking, ledgering, resume/report flows, and the launchd supervision window. `scripts/nightly_orchestrator.py` is the thin CLI entrypoint.

### Operations and scripts

`scripts/` contains operational helpers for launchd, staging, audits, backfills, validation checks, and report generation.

## Repository map for future edits

When changing code, start in the subsystem that owns the behavior:

- **Source ingestion or env loading:** `ingestion/core/config.py`, `ingestion/core/storage.py`, and the relevant source runner (including `ingestion/dream_judge/run.py` for the Mac-side Opus judge client)
- **Retrieval or memory policy:** `cloudflare-mcp/mcp-server/src/index.ts`, `salience.ts`, `salience_v2.ts`, `retrievalPolicy.ts`, `mmr.ts`, `precedence.ts`
- **Dream mutation logic:** `cloudflare-mcp/mcp-server/src/dream.ts`, `judgeQueue.ts`, `phase9OutcomeGate.ts`
- **Semantic candidate guard or lossless merge gates:** `cloudflare-mcp/mcp-server/src/dream.ts` (`processSemanticCandidateTask`), `semanticMaintenance.ts`, `mergeGates.ts`, `semanticCursor.ts`, `maintenanceJournal.ts`, `maintenanceQueue.ts` — see [Cloudflare MCP and Dream control plane](architecture/mcp-and-dream.md)
- **Nightly scheduling and run control:** `orchestrator/engine.py`, `orchestrator/stages.py`, `orchestrator/dream.py`
- **Launchd behavior:** `scripts/run_orchestrator_launchd.sh`, `scripts/install_orchestrator_launchd_shadow.sh`
- **CI and remote scheduling:** `.github/workflows/` (worker-runtime-tests, agent-session/github/twitter ingestion, nightly-semantic-maintenance, nightly-sleep-report) — see [Operations and local workflow](operations.md)
- **Validation and regression checks:** `tests/python/`, `tests/probes/`, `cloudflare-mcp/mcp-server/test/`, `orchestrator/tests/`, `scripts/run_eval.py`, `Makefile` targets `worker-typecheck` / `worker-test`

## Important cautions

- There are **two MCP implementations**. Do not assume the root `mcp-server/` directory is the production path.
- Many paths and environment defaults are personal and machine-specific. Avoid normalizing them unless the task explicitly asks for portability work.
- Some docs in `docs/` describe earlier or aspirational designs. Use source code and recent git history to confirm current behavior.
- Several operations are gated by explicit policy flags and safety thresholds. Before changing live behavior, inspect the relevant tests and recent commits.

## If you are editing something

Use the page that matches the subsystem:

- Ingestion behavior: [Ingestion and distillation workflow](workflows/ingestion-and-distillation.md)
- Nightly run control: [Nightly orchestrator workflow](workflows/nightly-orchestration.md)
- Retrieval and Dream: [Cloudflare MCP and Dream control plane](architecture/mcp-and-dream.md)
- Tests and checks: [Testing guidance](testing.md)

If you are unsure, read the architecture overview first, then jump to the subsystem page that owns the code you plan to touch.

## Backlog

No outstanding documentation backlog. The semantic consolidation modules
(`mergeGates.ts`, `semanticCursor.ts`, `semanticMaintenance.ts`,
`maintenanceJournal.ts`, `maintenanceQueue.ts`) and the stale-vector-row
candidate guard are documented in
[MCP and Dream control plane](architecture/mcp-and-dream.md), along with the
salience v2 / MMR / `RANKING_V2` ranking path, the precedence lattice, and the
tripwire kill switches. The Mac-side Opus judge client
(`ingestion/dream_judge/run.py`) is documented in
[Ingestion and distillation workflow](workflows/ingestion-and-distillation.md),
and the GitHub Actions automation surface is documented in
[Operations and local workflow](operations.md).
