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

- source-first build, scanner, and publisher atomicity (`tests/python/test_source_first.py`) — chunking determinism, boilerplate stripping, authoritative-file filtering, required-project inclusion, and the "failed candidate does not replace the working pointer" invariant
- ingestion behavior
- Gmail/GitHub/Twitter clients and extractors
- memory migration and deserialization
- quality audits and validation ledgers
- Dream judge fallback behavior (`tests/python/test_dream_judge_fallback.py`) and the judge client parser (`ingestion/dream_judge/test_parser.py`)
- nightly health checks
- retrieval and phase-specific schema behavior
- salience v2 and precedence lattice lockstep with the Worker twins

This is the main place to look when changing the Python pipeline or repo-level behavior outside the Worker runtime.

### Worker runtime tests

`cloudflare-mcp/mcp-server/test/` contains the Cloudflare Worker Vitest suite.

These tests cover a lot of the production control plane:

- source-first scoring, suppression, and project index (`test/sourceFirst.test.ts`) — the production read path
- retrieval policy (legacy/staging)
- salience (legacy/staging)
- archive guards
- judge queue behavior
- scheduled async Dream behavior
- semantic deduplication
- durable semantic consolidation authority (stale/cross-type vector neighbour filtering, merge hard gates, semantic cursor)
- phase 8 retrieval behavior
- tripwires
- OAuth/MCP transport behavior
- worker HTTP behavior

If you change the live MCP server, Dream logic, or the source-first read path, this suite is the first regression layer to check.

### Worker CI

`.github/workflows/worker-runtime-tests.yml` runs on push and pull request when
`cloudflare-mcp/mcp-server/**`, `Makefile`, `README.md`, or
`docs/testing-matrix.md` change. It runs `npm ci`, generates Worker types
(`npm run cf-typegen`), type-checks (`npm run type-check`), and runs the runtime
suite (`npm run test:worker`, i.e. `vitest run --no-file-parallelism`). Locally
the Makefile target `make worker-test` calls the same script; the narrow
type-check is `make worker-typecheck`.

### Python twins of shared logic

Several Worker TypeScript modules have Python twins in `distillation/utils/`
that must stay semantically identical, enforced by shared fixture tables replayed
by both test suites:

- `salience_v2.ts` ↔ `distillation/utils/salience_v2.py`, locked by
  `shared/salience_v2_fixtures.json` (`test/salience_v2.test.ts` and
  `tests/python/test_salience_v2.py`)
- `precedence.ts` ↔ `distillation/utils/precedence.py`, locked by
  `shared/precedence_fixtures.json` (`test/precedence.test.ts` and
  `tests/python/test_precedence_lattice.py`)

If you change one twin, update the shared fixture and run both suites so the
other twin does not drift.

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

- **Source-first build/scanner/publisher changes:** `tests/python/test_source_first.py` (chunking, authoritative filtering, atomic publish/verify, failed-candidate isolation)
- **Source-first serving/scoring changes:** `cloudflare-mcp/mcp-server/test/sourceFirst.test.ts` (fixed-score components, suppression rules, explicit-project detection, project index ordering), then `make worker-test`
- **Ingestion env/config changes:** Python tests around the affected source and config validation
- **Retrieval/scoring changes (legacy/staging):** Worker tests for salience, retrieval policy, and phase 8 retrieval; for salience v2 / MMR / precedence also run the Python twin tests (`tests/python/test_salience_v2.py`, `tests/python/test_precedence_lattice.py`) so the twins stay in lockstep; then run `scripts/run_eval.py` before and after to produce an eval diff
- **Dream mutation changes (legacy/staging):** Worker Dream tests, judge queue tests, phase 9 outcome gate tests
- **Orchestrator changes (staging/legacy):** `orchestrator/tests/`
- **Launchd or schedule changes (staging/legacy):** orchestrator supervise-window tests and shell wrapper behavior

## Useful source anchors

- `docs/testing-matrix.md`
- `tests/python/README.md`
- `tests/python/`
- `tests/python/test_source_first.py`
- `tests/probes/README.md`
- `tests/probes/`
- `scripts/run_eval.py`
- `cloudflare-mcp/mcp-server/test/README.md`
- `cloudflare-mcp/mcp-server/test/`
- `cloudflare-mcp/mcp-server/test/sourceFirst.test.ts`
- `orchestrator/tests/`
