---
type: "Reference"
title: "Operations and local workflow"
description: "Environment, launchd, scripts, and the automated OpenWiki CI update workflow for the personal-knowledge-system repository."
---

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

## Source-first freshness and write lockdown

When `SOURCE_FIRST_MODE=on`, the Cloudflare Worker serves only the promoted
immutable source-first generation. All legacy MCP write tools, `/ops/dream/*`
mutation routes, the Dream scheduler, and the maintenance queue are held. The
source-first rebuild workflow is the only promotion path:
`.github/workflows/source-first-rebuild.yml` writes a manifest and `sf:heartbeat`,
runs the retrieval regression gate against the committed baseline, and then
checks that the promoted generation is no more than 36 hours old. `/health`
reports the generation age and freshness status; `degraded` means the serving
generation or heartbeat is missing, stale, or mismatched.

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

A scheduled GitHub Actions workflow (`.github/workflows/openwiki-update.yml`) automatically refreshes this wiki. It runs daily (`cron: "0 8 * * *"`) and on manual `workflow_dispatch`. The job installs OpenWiki globally, runs `openwiki --update --print --modelId z-ai/glm-5.2` (authenticated with `OPENROUTER_API_KEY`), and commits any changes in `openwiki/` directly to the current branch as `openwiki-bot` (`docs: update OpenWiki [automated]`) before pushing. There is no pull-request step and no separate `AGENTS.md`/`CLAUDE.md` section management in this workflow.

## GitHub Actions automation

Beyond the OpenWiki update, the repo runs several scheduled and push-triggered workflows under `.github/workflows/`. The semantic-maintenance and sleep-report workflows are on real `cron` schedules; the ingestion workflows are not scheduled and run only on `repository_dispatch` / `workflow_dispatch`:

- `agent-session-ingestion.yml` — remote run of `ingestion/agent_sessions/run.py` on a self-hosted macOS runner (`knowledge-agent-sessions` label). Triggered by `repository_dispatch` (`agent-session-ingestion-manual`) or `workflow_dispatch`; not scheduled. Supports `dry_run`, `backfill`, `sync_state_only`, a `source` filter (`claude_code` or `codex_cli`), and a `limit` cap.
- `github-ingestion.yml` — remote GitHub repo ingestion, including repo-attached agent context under `.pks/agent-context/`. Triggered by `repository_dispatch` (`github-ingestion-manual`) or `workflow_dispatch`; not scheduled. Supports `dry_run`, `no_resume`, a comma-separated `repos` filter, and `skip_code` / `skip_commits` toggles.
- `twitter-ingestion.yml` — remote Twitter/X timeline ingestion on a self-hosted macOS runner. Triggered by `workflow_dispatch` only; not scheduled. Supports `dry_run`, `reset_state` (ignore saved `since_id`), and an optional `max_tweets` cap. Do not install a local LaunchAgent for Twitter ingestion.
- `nightly-semantic-maintenance.yml` — runs `scripts/nightly_semantic_maintenance.py` on `cron: "20 7 * * *"` (07:20 UTC, after the Worker's `07:10 UTC` Dream trigger). Feeds bounded candidate clusters to the Worker queue. Defaults to `live` mode on schedule; `workflow_dispatch` accepts `plan` or `live` and a `max_applied` cap (defaults to `100` on schedule, `100` on manual). Guards that the retired Worker semantic slice (`SEMANTIC_SLICE_SIZE == 0`) remains disabled.
- In source-first mode, the legacy Dream and semantic-maintenance paths above are intentionally short-circuited by the Worker; retain the workflow files as historical/rollback artifacts, but do not treat their old mutation behavior as live.
- `nightly-sleep-report.yml` — verifies durable completion of the semantic maintenance cohort on `cron: "45 8 * * *"` (08:45 UTC) by running `scripts/nightly_semantic_maintenance.py --check-latest --max-age-hours 4`.
- `worker-runtime-tests.yml` — push/PR-triggered CI for `cloudflare-mcp/mcp-server/**`, `Makefile`, `README.md`, and `docs/testing-matrix.md`: type-check + `npm run test:worker`. See [Testing guidance](testing.md).

The active source-first sequence is: ingestion workflows as dispatched, source-first rebuild/promotion at `06:30 UTC`, and a read-only retrieval gate in that same workflow. The old Dream/semantic/sleep sequence remains documented only for rollback archaeology and is short-circuited when source-first mode is on.

## Useful source anchors

- `scripts/`
- `scripts/nightly_orchestrator.py`
- `scripts/nightly_semantic_maintenance.py`
- `scripts/semantic_candidate_planner.py`
- `scripts/run_orchestrator_launchd.sh`
- `scripts/run_nightly_ingestion.sh`
- `scripts/install_global_repo_agent_context_hook.sh`
- `scripts/install_repo_agent_context_hook.sh`
- `.github/workflows/`
- `docs/testing-matrix.md`
