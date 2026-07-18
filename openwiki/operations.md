# Operations and local workflow

This repository is operationally heavy. It depends on environment files, local exports, launchd wrappers, staging checks, and scripted validation runs.

## Environment and secrets

Several subsystems expect personal credentials and local paths. The repo deliberately does not hide that.

Common environment families include:

- Upstash Redis and Vector credentials
- OpenAI and Anthropic API keys
- GitHub API credentials
- Gmail mailbox path
- local export paths for Claude/GPT data

Do not read or document secret values. The docs should point to the relevant config files and scripts, not expose secrets.

## Launchd and nightly operations

The nightly orchestrator is wrapped by shell scripts for local and launchd usage.

Important files:

- `scripts/run_orchestrator_launchd.sh`
- `scripts/install_orchestrator_launchd_shadow.sh`
- `scripts/install_launchd.sh`
- `scripts/run_nightly_ingestion.sh`
- `scripts/run_second_chance.sh`

The launchd wrapper is especially important because it forces shadow-only behavior and maps launchd firings to the correct run date.

## Operational scripts

The `scripts/` directory contains many one-off or recurring operational tools, including:

- deployment helpers
- backfill and repair scripts
- nightly health/validation checks
- Dream-related audits and benchmarks
- repo-agent-context export/install helpers
- staging provisioning and verification tools
- `scripts/run_eval.py` — retrieval-quality eval runner (see [Testing guidance](testing.md))

When changing a script, check whether it writes to `scripts/reports/`, `ingestion/checkpoints/`, or Redis keys. Many of these scripts are designed around durable artifacts rather than stdout alone.

## Common workflow patterns

### Ingestion changes

Typical workflow:

1. Update source-specific ingestion code or config.
2. Run targeted Python tests.
3. Verify checkpointing and storage behavior.
4. Check whether retrieval metadata or Dream behavior needs a follow-up change.

### Dream or retrieval changes

Typical workflow:

1. Update the Worker server modules.
2. Run the Worker Vitest suite.
3. Run `scripts/run_eval.py` before the change, then again after, and diff the two reports (`--compare`) — the README requires this before any ranking, forgetting, or admission change ships.
4. Inspect the relevant `docs/` PRD if behavior is phase-gated.
5. Check whether the orchestrator or tests need matching updates.

### Orchestrator changes

Typical workflow:

1. Update the Python engine, ledger, or stage definitions.
2. Run orchestrator tests.
3. Verify run/report artifacts and the supervise window behavior.
4. Check the launchd wrapper for environment coupling.

## OpenWiki update workflow

A scheduled GitHub Actions workflow (`.github/workflows/openwiki-update.yml`) automatically refreshes this wiki. The workflow runs `openwiki code --update --print` with the model configured via `OPENWIKI_MODEL_ID` and LangSmith tracing enabled. Instead of committing directly to the default branch, it creates a pull request (branch `openwiki/update`) using `peter-evans/create-pull-request`, so updates are reviewable before merge. The workflow also updates `AGENTS.md` and `CLAUDE.md` (managed OpenWiki sections delimited by `<!-- OPENWIKI:START -->` / `<!-- OPENWIKI:END -->`).

## Useful source anchors

- `scripts/`
- `scripts/nightly_orchestrator.py`
- `scripts/run_orchestrator_launchd.sh`
- `scripts/run_nightly_ingestion.sh`
- `scripts/install_global_repo_agent_context_hook.sh`
- `scripts/install_repo_agent_context_hook.sh`
- `docs/testing-matrix.md`
