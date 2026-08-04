---
type: "Reference"
title: "Testing guidance"
description: "Testing strategy across Python ingestion, TypeScript Worker runtime, Dream governance, orchestration, and retrieval-quality evals."
---

# Testing guidance

Testing in this repository is broad because the system crosses Python ingestion, TypeScript Worker runtime, Dream governance, and orchestration.

The canonical high-level reference is `docs/testing-matrix.md`, which describes the layered test strategy and the environments the repo expects.

## Test surfaces

### Python tests

The `tests/python/` directory contains a substantial regression suite around:

- ingestion behavior
- Gmail/GitHub/Twitter clients and extractors
- memory migration and deserialization
- quality audits and validation ledgers
- Dream judge fallback behavior
- nightly health checks
- retrieval and phase-specific schema behavior

This is the main place to look when changing the Python pipeline or repo-level behavior outside the Worker runtime.

### Worker runtime tests

`cloudflare-mcp/mcp-server/test/` contains the Cloudflare Worker Vitest suite.

These tests cover a lot of the production control plane:

- retrieval policy
- salience
- archive guards
- judge queue behavior
- scheduled async Dream behavior
- semantic deduplication
- durable semantic consolidation authority (stale/cross-type vector neighbour filtering, merge hard gates, semantic cursor)
- phase 8 retrieval behavior
- tripwires
- OAuth/MCP transport behavior
- worker HTTP behavior

If you change the live MCP server or Dream logic, this suite is the first regression layer to check.

### Orchestrator tests

`orchestrator/tests/` validates:

- run date identity
- lock behavior
- ledger transitions
- Dream client handling
- preflight behavior
- report rendering
- supervise-window mapping
- engine state-machine behavior

These tests are especially important if you touch date handling, resume semantics, or the launchd sidecar path.

### Retrieval-quality eval suite

`tests/probes/` contains an 8-axis probe suite (recall, project, explicit-save, exact-lexical, stale-fact, supersession, negative, paraphrase) and `scripts/run_eval.py` is the read-only runner that issues each probe's query against the MCP `search` tool and scores the results.

Key behavior from the code and `tests/probes/README.md`:

- scoring is deterministic string/id matching — no LLM judge needed in retrieval mode
- axes with zero enabled probes are reported as UNMEASURED, never silently omitted
- `--compare OLD.json NEW.json` diffs two prior reports with no network — this is the shadow A/B safety rail for retrieval changes
- reports land in `scripts/reports/eval_baseline_<UTC>.json`

The README mandates: no ranking, forgetting, or admission change ships without a before/after eval diff. If you change retrieval policy, salience weighting, or Dream consolidation behavior, run the eval before and after.

## What the matrix cares about

`docs/testing-matrix.md` makes an important distinction:

- fixture/offline tests for deterministic logic
- local integration tests for source-specific runtime behavior
- staging end-to-end tests for the deployed path
- production canaries for bounded read-only or restore-safe checks

That distinction matters because many failures in this repo are environment or service boundary failures, not simple unit test failures.

## Practical guidance for future edits

When changing code, check the relevant test family first:

- **Ingestion env/config changes:** Python tests around the affected source and config validation
- **Retrieval/scoring changes:** Worker tests for salience, retrieval policy, and phase 8 retrieval; then run `scripts/run_eval.py` before and after to produce an eval diff
- **Dream mutation changes:** Worker Dream tests, judge queue tests, phase 9 outcome gate tests
- **Orchestrator changes:** `orchestrator/tests/`
- **Launchd or schedule changes:** orchestrator supervise-window tests and shell wrapper behavior

## Useful source anchors

- `docs/testing-matrix.md`
- `tests/python/README.md`
- `tests/python/`
- `tests/probes/README.md`
- `tests/probes/`
- `scripts/run_eval.py`
- `cloudflare-mcp/mcp-server/test/README.md`
- `cloudflare-mcp/mcp-server/test/`
- `orchestrator/tests/`
