# Personal Knowledge System

This repository is building a personal memory layer for AI assistants.

The goal is not just to store past conversations. The goal is to turn a long stream of chats, coding sessions, repositories, and email into a living memory system that can:

- remember stable identity and project context
- surface the right memories for the current question
- avoid flooding the model with one-off facts
- gradually consolidate what matters
- eventually "dream" over old material and keep only the useful residue

This README is meant to explain the system, not to present it as a turnkey template. The codebase is private, opinionated, and tied to one operator's local machine, Cloudflare account, and Upstash databases.

## What We Are Building

At a high level, the system has five layers:

1. Ingestion
   It pulls raw material from AI conversation exports, coding-agent sessions, GitHub, and Gmail.
2. Distillation
   It turns raw material into structured memory entries with provenance.
3. Storage
   It stores those entries in Redis and their embeddings in Upstash Vector.
4. Retrieval
   It serves the memory through a Cloudflare-hosted MCP server with tier-aware search and thin-index summaries.
5. Maintenance
   It will promote, demote, consolidate, and archive memories over time through reconsolidation and Dream jobs.

The end state is a memory system that behaves less like a document archive and more like a selective autobiographical memory.

## The Full System Vision

The target system has these behaviors:

- durable identity and long-lived project context should be available by default
- recurring but lower-priority context should appear when it is relevant
- one-off facts should stay retrievable without constantly taking up context window space
- repeated retrieval should strengthen memories
- stale or low-value memories should be archived, not deleted blindly
- nightly or scheduled "Dream" runs should reconcile the current self-model from accumulated experience

That full design is larger than what is currently live. Some parts are already running in production; some are the next phases.

## System Model

```text
Raw Sources
  Claude exports
  ChatGPT exports
  Claude Code sessions
  Codex CLI sessions
  GitHub repos
  Gmail

        |
        v

Ingestion + Distillation
  Python pipelines extract structured entries
  Models assign provenance, summaries, and embeddings
  Migration/backfill scripts normalize old data

        |
        v

Memory Store
  Upstash Redis
    knowledge:{id}
    project:{id}
    index:current
    migration flags
    future Dream/reconsolidation state

  Upstash Vector
    one embedding per active entry
    metadata for retrieval filters and scoring

        |
        v

Retrieval Layer
  Cloudflare Worker MCP server
  OAuth-enabled public interface
  thin index
  semantic search
  context retrieval
  health/status endpoint

        |
        v

Future Maintenance Layer
  reconsolidation on repeated access
  Dream coordinator and archive pipeline
  operator tools for restoration and overrides
```

## Memory Model

The system stores two primary entry types:

- `KnowledgeEntry` (`ke_*`)
  A durable belief, skill, preference, technique, or topic model.
- `ProjectEntry` (`pe_*`)
  An ongoing effort with goals, status, phase, blockers, and decisions.

Each entry has a `schema_version` and migration-safe metadata. The important fields in the current design are:

- `context_type`
  What kind of memory this is: identity, project, pattern, task-query, and so on.
- `injection_tier`
  How aggressively this memory should be surfaced.
- `salience_score`
  A score derived from confidence, recency decay, mention frequency, context type, and recent retrieval.
- `classification_status`
  Whether the entry has been backfilled/classified yet.
- `archived`
  Whether the entry should be excluded from normal retrieval.

### Retrieval Tiers

The system is moving toward a three-tier memory model:

- Tier 1
  Durable identity, long-running projects, and context that should often be available.
- Tier 2
  Recurring, topic-adjacent, or medium-priority context.
- Tier 3
  One-off or direct-query-only context that should stay searchable but not dominate context injection.

This tiering is the main mechanism for preventing the memory system from becoming a pile of equally weighted notes.

## Retrieval Model

The production MCP server exposes:

- `get_index`
  Returns the thin-index subset plus true totals and tier counts.
- `get_context`
  Returns the current view of the best matching active topic or project.
- `get_deep`
  Returns the full stored entry with provenance.
- `search`
  Performs tier-aware semantic retrieval.
- `github`
  Queries linked GitHub repositories live.

Search no longer uses a simple "70% relevance + 30% recency" rule. The current design reranks results using:

- semantic similarity
- recency
- salience
- source weights
- retrieval tier

Archived entries remain in storage but are excluded from normal retrieval by default.

## Thin Index

The thin index is the compressed map of the memory system.

It is intentionally not a full dump of every entry. It stores:

- a token-budgeted subset of topics and projects
- true total topic/project counts
- tier counts
- archive counts
- recent evolution summaries

This lets a client get a fast overview of the memory landscape without paying the cost of loading the entire store.

## Dream And Reconsolidation

These are the main pieces still being built.

### Reconsolidation

Reconsolidation is the short-horizon maintenance loop. The idea is:

- retrieval increments access counters
- frequently re-accessed memories get promoted or refreshed
- repeated retrieval can strengthen salience
- the system records consolidation notes and errors

This is Phase 4 work. It is not live yet.

### Dream

Dream is the long-horizon maintenance loop. The idea is:

- run on a schedule
- revisit the memory graph in batches
- keep durable context
- archive low-value memories with reversible pointers
- rebuild the current self-model without re-injecting everything forever

Dream is Phase 5 work. It is not live yet.

## What Is Live Today

As of March 27, 2026, the live system has:

- `573` knowledge topics
- `36` projects
- schema version `2`
- completed Phase 1, Phase 2, and Phase 3 of the current memory upgrade
- `0` pending classifications in `classification:pending`
- tier counts of `500` Tier 1, `24` Tier 2, `85` Tier 3
- `0` archived entries

Operationally, the following are live:

- Python ingestion/distillation pipelines
- shared salience policy between Python and TypeScript
- vector metadata normalization
- rebuilt thin index with tier/salience metadata
- OAuth-enabled Cloudflare MCP server
- `/health` and `/status` rollout endpoints

Not live yet:

- access-counter reconsolidation
- Dream scheduler and archive pipeline
- write-capable operator MCP tools such as restore/archive overrides

## How The Repo Is Organized

```text
knowledge-system/
  ingestion/
    github/, gmail/, agent_sessions/
    Python ingestion pipelines for ongoing raw-source intake

  distillation/
    Original export-processing pipeline for Claude/ChatGPT data
    Also contains storage clients, models, and thin-index generation

  scripts/
    Migration and verification scripts
    backfill_context_type.py
    backfill_counts.py
    verify_memory_consistency.py

  shared/
    Cross-language policy files
    memory_policy.json
    salience_fixtures.json

  cloudflare-mcp/mcp-server/
    Production MCP server
    Cloudflare Worker, OAuth wrapper, retrieval tools

  mcp-server/
    Legacy server implementation
    Not the production target

  docs/
    PRDs, audit notes, and upgrade checklists

  skill/
    Claude skill instructions for using the memory system
```

## Operational Surfaces

### Worker Endpoints

The public Worker exposes:

- `/sse`
- `/mcp`
- `/authorize`
- `/token`
- `/register`
- `/.well-known/oauth-authorization-server`
- `/health`
- `/status`

### Health Endpoint

`/health` and `/status` are the main operator-facing rollout checks. They report:

- schema version
- migration completion state
- pending classification count
- last Dream run timestamp
- thin-index totals
- tier counts
- archived count

### Migration Scripts

The upgrade work introduced three important operator scripts:

- `scripts/backfill_context_type.py`
  LLM classification pass for old entries.
- `scripts/backfill_counts.py`
  Deterministic metadata/vector normalization and thin-index rebuild.
- `scripts/verify_memory_consistency.py`
  Redis vs Vector vs thin-index verification.

These scripts are how the repo moved from legacy mixed-schema data to the current retrieval model.

## Current Upgrade Status

The repository is in the middle of a larger PKS memory upgrade.

Completed:

- Phase 0 audit and gap analysis
- Phase 1 schema and migration hooks
- Phase 2 live backfill and normalization
- Phase 3 tier-aware retrieval and rollout status endpoint

Next:

- Phase 4 reconsolidation
- Phase 5 Dream orchestration and reversible archiving
- Phase 6 ingestion hardening and operator tools

The upgrade checklist lives in `docs/pks-memory-upgrade-checklist.md`.

## Important Reading Order

If you are trying to understand the system, read in this order:

1. this README
2. `docs/pks-memory-upgrade-checklist.md`
3. `docs/pks-memory-upgrade-phase0-audit-2026-03-26.md`
4. `cloudflare-mcp/mcp-server/src/index.ts`
5. `distillation/models/entries.py`
6. `distillation/pipeline/index.py`
7. `shared/memory_policy.json`

That path gives the clearest picture of the actual architecture and the upgrade trajectory.

## Design Principles

The system is trying to enforce a few simple rules:

- memory should be selective, not exhaustive
- provenance matters
- retrieval quality matters more than raw storage volume
- the system should prefer reversible archival over destructive cleanup
- scoring rules should be shared across languages and runtimes
- health and migration state should be observable, not implicit

## Version History

- **1.2.0** (March 2026)
  Schema v2 migration, context-type backfill, tier-aware retrieval, shared salience policy, `/health` endpoint, OAuth-enabled Worker deployment.
- **1.1.0** (March 2026)
  Agent session ingestion, GitHub repo linking, launchd daemon, model upgrade to Claude Sonnet 4.6.
- **1.0.1** (March 2026)
  GitHub and Gmail ingestion pipelines, recency weighting, source-based scoring, thin-index compaction.
- **1.0.0** (December 2024)
  Initial implementation with distillation pipeline, Cloudflare MCP server, and Claude integration.

## License

Private repository. Not for redistribution.
