# Nightly orchestration workflow

The nightly orchestrator is the repository's run-control layer. It coordinates a date-scoped nightly job, persists stage state, writes reports, and manages resume logic.

## What it does

The orchestrator is responsible for:

- preflight checks
- acquiring a fencing lock for a run date
- executing a fixed stage sequence
- heartbeating between stages
- persisting a run ledger
- emitting JSON and Markdown reports
- supporting resume and report-only modes
- supervising the overnight window from launchd

The implementation is in Python under `orchestrator/`, with a thin CLI wrapper in `scripts/nightly_orchestrator.py`.

## Main files

### `orchestrator/cli.py`

Defines the command surface:

- `preflight`
- `run --mode shadow|live --date YYYY-MM-DD|auto`
- `resume --date YYYY-MM-DD|auto`
- `report --date YYYY-MM-DD|auto`
- `supervise`

### `orchestrator/engine.py`

This is the central state machine.

Important behavior from the code and recent history:

- it resolves run dates in Pacific time
- it creates a run identity that encodes the run date
- it uses a fencing lock to prevent duplicate runs
- it downgrades `live` requests to `shadow` when mutations are disabled
- it always renders a report, even on failure or supersession
- resume is same-host only
- a supervisory window determines whether launchd firings map to today, yesterday, or skip

### `orchestrator/stages.py`

Contains the stage executors.

Phase 1 is explicitly shadow/non-mutating:

- ingestion stages are shadow no-ops
- Dream stages drive an injectable Dream client, usually the shadow client
- verify/report stages are read-only

This file is where the run-state machine turns into concrete stage records.

### Supporting modules

Other orchestrator modules each own a narrow concern:

- `orchestrator/ledger.py` — persistent run ledger
- `orchestrator/lock.py` — fencing lock and heartbeat
- `orchestrator/dream.py` — Dream client abstraction and HTTP/shadow behavior
- `orchestrator/preflight.py` — env/auth prechecks
- `orchestrator/report.py` — report rendering
- `orchestrator/states.py` — stage record/status vocabulary
- `orchestrator/ids.py` — run identity construction
- `orchestrator/backends.py` — backend abstraction for Redis or test doubles

## Launchd supervision

`scripts/run_orchestrator_launchd.sh` is the launchd-facing wrapper.

It is intentionally opinionated:

- forces the Dream client to the real async Worker-backed path for Phase 4 supervision
- forces mutations off locally
- keeps the legacy nightly schedule as the source of truth
- maps launchd `supervise` firings into the correct run date

This wrapper is the operational entrypoint for the shadow-validation sidecar described in the recent PRDs.

## What changed recently

Recent git history shows the orchestrator evolved through multiple phases:

- initial nightly orchestrator state machine
- async Dream Worker integration
- manual shadow-run proofing
- launchd sidecar supervision
- same-host resume and report hardening

That history matters because the current code is not just a simple cron wrapper; it is a hardened control plane for a date-scoped nightly workflow.

## Where to be careful

- Do not assume `run` mutates anything in Phase 1 or the launchd sidecar path.
- Watch the difference between `requested_mode` and `effective_mode`.
- Keep the report artifacts in sync with ledger terminal state.
- Resume semantics are guarded by host identity and run-date identity.
- If you change stage order or stage names, you will affect reports and tests.

## Main source anchors

- `orchestrator/cli.py`
- `orchestrator/engine.py`
- `orchestrator/stages.py`
- `orchestrator/lock.py`
- `orchestrator/ledger.py`
- `orchestrator/dream.py`
- `scripts/nightly_orchestrator.py`
- `scripts/run_orchestrator_launchd.sh`
- `orchestrator/tests/`
