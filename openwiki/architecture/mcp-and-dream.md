---
type: "Reference"
title: "Cloudflare MCP and Dream control plane"
description: "Production Cloudflare Worker MCP server for retrieval, salience scoring, Dream lifecycle, judge queue, and scheduled async Dream support."
---

# Cloudflare MCP and Dream control plane

`cloudflare-mcp/mcp-server/` is the current production runtime for retrieval and memory governance.

It is a Cloudflare Worker implementation that uses the MCP SDK, Upstash Redis, Upstash Vector, OpenAI embeddings, and a small set of policy modules to shape both search and mutation behavior.

## What lives here

This server is the live control plane for:

- MCP tool serving
- retrieval ranking and policy shaping
- salience computation
- Dream proposal generation, grading, apply, rollback, and verification
- judge queue management
- scheduled async Dream support
- OpenAI-compatible read surfaces
- Twitter and GitHub helper flows

The top-level `AGENTS.md` says this is the current production path and should be inspected first for live MCP behavior.

## Main entrypoints

### `src/index.ts`

The main server file wires together:

- authentication and OAuth handling
- read-only and mutating MCP tools
- retrieval helpers
- Dream operations
- judge queue utilities
- tripwires and rate limiting
- source-specific helpers such as tweets and GitHub lookup

The file is large because it owns the full tool surface, but its major responsibilities are still fairly coherent: route, authorize, rank, and delegate to the right memory operation.

### `src/dream.ts`

Contains the core Dream logic and memory mutation helpers.

Recent history and code evidence show this module handles:

- proposal generation
- grading
- bounded apply
- rollback
- duplicate merge and contradiction handling
- scheduled governance operations
- insight synthesis support for content-bearing verdicts

### `src/judgeQueue.ts`

Manages the Worker-side judge queue.

The queue exists so borderline Dream operations can be judged offline and settled later. It now includes an `insight_synthesis` op type, which is the content-bearing verdict path introduced in the recent commit history.

### `src/scheduledDreamAsync.ts`

Implements the async scheduled Dream contract used by the newer orchestrator work.

It enforces:

- idempotent starts
- a per-date Redis lock
- mode gating for shadow vs live
- terminal status validation
- separation between accepted status and executed behavior

### Policy modules

Relevant policy/helper files include:

- `src/salience.ts`
- `src/retrievalPolicy.ts`
- `src/phase8Retrieval.ts`
- `src/phase9OutcomeGate.ts`
- `src/tripwires.ts`
- `src/consolidation.ts`

These modules are where the repo encodes its memory-specific heuristics, thresholds, and safety rules.

## Retrieval behavior

Retrieval is not a plain vector lookup.

The code uses additional policy shaping such as:

- topic bucket classification
- cross-context penalties
- quarantine suppression
- salience scoring and source weighting
- phase-specific retrieval gates

The goal is to keep unrelated memories from surfacing uninvited while still allowing them to be found on direct query.

`retrievalPolicy.ts` documents the rationale for this in code comments: the primary fix for unwanted cross-topic surfacing is to score down unrelated items rather than hard-filtering them.

## Dream governance

Dream is the repository's maintenance and consolidation system.

The current code and recent docs show it is no longer just a simple archive job. It now includes:

- candidate discovery
- proposal creation
- deterministic grading
- bounded live apply
- rollback mechanics
- duplicate merge handling
- contradiction handling
- judge queue interplay
- outcome-quality gating
- content-bearing insight synthesis in the judge path

This is the place to look when the repo changes what counts as durable memory, how memories are consolidated, or when a nightly maintenance action becomes safe to apply.

## Read/write tool surface

`AGENTS.md` notes the production server exposes read-only tools such as index, context, deep retrieval, search, and health, and write tools that require `mcp:write` scope.

If you are changing one of these tools, verify whether the change affects:

- auth scopes
- OpenAI-compatible routes
- retrieval ranking
- judge queue settlement
- Dream apply/revert flows
- tests in `cloudflare-mcp/mcp-server/test/`

## Important caution

There are two MCP implementations in the repo.

- `cloudflare-mcp/mcp-server/` is the current production Worker path.
- `mcp-server/` is the older Vercel-style implementation.

Do not treat them as interchangeable. They share intent, but their runtime environment and current responsibilities differ.

## Main source anchors

- `cloudflare-mcp/mcp-server/src/index.ts`
- `cloudflare-mcp/mcp-server/src/dream.ts`
- `cloudflare-mcp/mcp-server/src/judgeQueue.ts`
- `cloudflare-mcp/mcp-server/src/scheduledDreamAsync.ts`
- `cloudflare-mcp/mcp-server/src/salience.ts`
- `cloudflare-mcp/mcp-server/src/retrievalPolicy.ts`
- `cloudflare-mcp/mcp-server/src/phase8Retrieval.ts`
- `cloudflare-mcp/mcp-server/src/phase9OutcomeGate.ts`
- `cloudflare-mcp/mcp-server/test/`
- `cloudflare-mcp/mcp-server/package.json`
