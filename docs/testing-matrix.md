# Testing Matrix

This document defines the testing system for the personal knowledge system.

The goal is not just to verify that code runs. The goal is to validate the full memory loop:

- ingestion
- distillation
- storage
- retrieval
- reconsolidation
- Dream
- operator controls
- deployment health

## Current State

The repo already has useful probes, but they are not yet a unified testing stack:

- live MCP probes:
  - [test_mcp_simple.py](/Users/arjundivecha/Dropbox/AAA%20Backup/A%20Working/Memory/knowledge-system/test_mcp_simple.py)
  - [test_mcp.py](/Users/arjundivecha/Dropbox/AAA%20Backup/A%20Working/Memory/knowledge-system/test_mcp.py)
  - [test_mcp_tools.py](/Users/arjundivecha/Dropbox/AAA%20Backup/A%20Working/Memory/knowledge-system/test_mcp_tools.py)
  - [test_sse_connection.py](/Users/arjundivecha/Dropbox/AAA%20Backup/A%20Working/Memory/knowledge-system/test_sse_connection.py)
- storage and migration checks:
  - [distillation/test_connection.py](/Users/arjundivecha/Dropbox/AAA%20Backup/A%20Working/Memory/knowledge-system/distillation/test_connection.py)
  - [scripts/backfill_context_type.py](/Users/arjundivecha/Dropbox/AAA%20Backup/A%20Working/Memory/knowledge-system/scripts/backfill_context_type.py)
  - [scripts/backfill_counts.py](/Users/arjundivecha/Dropbox/AAA%20Backup/A%20Working/Memory/knowledge-system/scripts/backfill_counts.py)
  - [scripts/verify_memory_consistency.py](/Users/arjundivecha/Dropbox/AAA%20Backup/A%20Working/Memory/knowledge-system/scripts/verify_memory_consistency.py)
- production-shaped Dream canary:
  - [test-dream-live.ts](/Users/arjundivecha/Dropbox/AAA%20Backup/A%20Working/Memory/knowledge-system/cloudflare-mcp/mcp-server/scripts/test-dream-live.ts)

That is a good operator toolbox. It is not yet a full layered test system.

## Environments

### 1. Fixture / Offline

Purpose:

- deterministic tests
- no production secrets
- no external mutation

Data source:

- frozen fixtures in [tests/fixtures/](/Users/arjundivecha/Dropbox/AAA%20Backup/A%20Working/Memory/knowledge-system/tests/fixtures)

### 2. Local Integration

Purpose:

- run Python and Worker tests against local code
- validate schema, transforms, and local runtime behavior

Characteristics:

- may use real APIs for embeddings if needed
- must never target production storage by default

### 3. Staging End-To-End

Purpose:

- validate the full deployed path
- seed a known memory state
- test retrieval, Dream, and operator flows against isolated infrastructure

Requirements:

- separate staging Worker
- separate staging Upstash Redis
- separate staging Upstash Vector

### 4. Production Canary

Purpose:

- validate live deployment safety
- bounded checks only

Rules:

- read-only by default
- explicit bounded write canaries only
- all write canaries must restore and verify afterward

## Test Layers

### Layer A: Schema And Pure Logic

Validates:

- metadata normalization
- salience computation
- tier resolution
- archive candidacy rules
- promotion rules

Target:

- fixture/offline

### Layer B: Python Pipeline Integration

Validates:

- parser output
- distillation output
- backfill logic
- thin index generation
- storage serialization

Target:

- fixture/offline and local integration

### Layer C: Worker Runtime Integration

Validates:

- `/health`
- OAuth flow edges
- MCP initialize/list/call
- search filtering
- reconsolidation writes
- operator endpoint auth

Target:

- local integration and staging

Tooling note:

- Worker runtime tests should converge on Cloudflare's Vitest integration rather than raw curl scripts.
- When local behavior diverges from real bindings, use remote-aware development/testing instead of trusting a purely local simulation.

### Layer D: Storage Consistency

Validates:

- Redis entries
- vector metadata
- thin index totals
- migration flags

Target:

- local integration, staging, and production canary

### Layer E: Dream Lifecycle

Validates:

- dry-run candidate generation
- bounded live archive
- snapshot creation
- restore behavior
- post-restore non-prunability
- `dream:last_run` stability when `setAsLatest=false`

Target:

- staging first
- production canary second

## Minimum Acceptance Gates

Before calling the system healthy, the following should be green:

- fixture seeding succeeds into staging
- `/health` is green in staging
- deterministic fixture queries return expected tiers/order
- MCP `initialize`, `tools/list`, `get_index`, `search`, and `get_context` all pass in staging
- unauthorized operator calls return `401`
- Dream dry run returns expected archive candidates for the seeded fixture set
- bounded live Dream archive/restore canary passes
- final `verify_memory_consistency.py --full --strict` returns `0` issues in staging

## Repo Layout

The target layout is:

```text
tests/
  fixtures/
    README.md
    sample_memory_fixture.json
  golden/
    README.md
  python/
    README.md

cloudflare-mcp/mcp-server/
  test/
    README.md

scripts/
  seed_staging_env.py
  run_e2e_staging.py
```

## Command Surface

The root [Makefile](/Users/arjundivecha/Dropbox/AAA%20Backup/A%20Working/Memory/knowledge-system/Makefile) is the entry point for the testing system.

Key commands:

- `make worker-typecheck`
- `make verify-memory-full`
- `make dream-live-canary`
- `make seed-staging-dry-run`
- `make staging-smoke-dry-run`

## Near-Term Build Order

1. Build a small gold fixture corpus.
2. Seed an isolated staging Redis/Vector pair from that corpus.
3. Add Worker runtime tests for `/health`, operator auth, and MCP basics.
4. Add staging smoke tests that hit the deployed staging Worker.
5. Add CI to run offline and local layers automatically.
6. Keep production canaries separate and explicit.

## Important Rule

Production is not the default test bed.

The right long-term shape is:

- fixture tests for correctness
- staging tests for end-to-end confidence
- production canaries for bounded real-world safety
