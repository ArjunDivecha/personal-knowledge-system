---
type: "Reference"
title: "Operations and local workflow"
description: "Environment, launchd, scripts, GitHub Actions automation (including the source-first rebuild schedule and retired semantic-maintenance crons), and the OpenWiki CI update workflow."
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

## Common workflow patterns

### Source-first build or serving changes

Typical workflow:

1. Update `ingestion/source_first/` scanner/publisher/models, `scripts/source_first_rebuild.py`, `shared/source_first_config.json` / `shared/source_first_suppressions.json`, or `cloudflare-mcp/mcp-server/src/sourceFirst.ts`.
2. Run `python -m unittest tests.python.test_source_first` (build/publish/verify) and `cd cloudflare-mcp/mcp-server && npx vitest run test/sourceFirst.test.ts --no-file-parallelism` (serving/scoring).
3. For a config/policy change, run `python scripts/source_first_rebuild.py` locally (no `--publish`) to validate the candidate artifacts.
4. Dispatch `source-first-rebuild.yml` with `publish: false` first, then `publish: true` to promote. See [Source-first rebuild workflow](workflows/source-first-rebuild.md).

### Ingestion changes (legacy sources)

Typical workflow:

1. Update source-specific ingestion code or config.
2. Run targeted Python tests.
3. Verify checkpointing and storage behavior.
4. These pipelines feed the legacy memory model, not the source-first index.

### Dream or retrieval changes (staging/legacy)

Typical workflow:

1. Update the Worker server modules (legacy path, exercised in staging where `SOURCE_FIRST_MODE: "off"`).
2. Run the Worker Vitest suite.
3. Run `scripts/run_eval.py` before the change, then again after, and diff the two reports (`--compare`) — the README requires this before any ranking, forgetting, or admission change ships.
4. Inspect the relevant `docs/` PRD if behavior is phase-gated.
5. Check whether the orchestrator or tests need matching updates.

### Orchestrator changes (staging/legacy)

Typical workflow:

1. Update the Python engine, ledger, or stage definitions.
2. Run orchestrator tests.
3. Verify run/report artifacts and the supervise window behavior.
4. Check the launchd wrapper for environment coupling.

## OpenWiki update workflow

A scheduled GitHub Actions workflow (`.github/workflows/openwiki-update.yml`) automatically refreshes this wiki. It runs daily (`cron: "0 8 * * *"`) and on manual `workflow_dispatch`. The job installs OpenWiki globally, runs `openwiki --update --print --modelId z-ai/glm-5.2` (authenticated with `OPENROUTER_API_KEY`), and commits any changes in `openwiki/` directly to the current branch as `openwiki-bot` (`docs: update OpenWiki [automated]`) before pushing. There is no pull-request step and no separate `AGENTS.md`/`CLAUDE.md` section management in this workflow.

## GitHub Actions automation

Beyond the OpenWiki update, the repo runs several workflows under `.github/workflows/`. After the source-first cutover, only two workflows have real `cron` schedules: `source-first-rebuild.yml` and `openwiki-update.yml`. The semantic-maintenance and sleep-report workflows are retired from automatic schedule (manual `workflow_dispatch` only); the ingestion workflows were never scheduled and run only on `repository_dispatch` / `workflow_dispatch`:

- `source-first-rebuild.yml` — the only scheduled maintenance job after cutover. Runs `scripts/source_first_rebuild.py --publish` on `cron: "30 6 * * *"` (06:30 UTC) on a self-hosted macOS runner (`knowledge-agent-sessions` label) that can read the Dropbox sources. `workflow_dispatch` accepts a `publish` boolean (defaults to `true`; set `false` for a dry build). After publish it runs `--verify-current` to confirm the promoted generation. Uploads `manifest.json`, `projects.json`, and `suppressions.json` as build evidence (30-day retention). See [Source-first rebuild workflow](workflows/source-first-rebuild.md).
- `agent-session-ingestion.yml` — remote run of `ingestion/agent_sessions/run.py` on a self-hosted macOS runner (`knowledge-agent-sessions` label). Triggered by `repository_dispatch` (`agent-session-ingestion-manual`) or `workflow_dispatch`; not scheduled. Supports `dry_run`, `backfill`, `sync_state_only`, a `source` filter (`claude_code` or `codex_cli`), and a `limit` cap.
- `github-ingestion.yml` — remote GitHub repo ingestion, including repo-attached agent context under `.pks/agent-context/`. Triggered by `repository_dispatch` (`github-ingestion-manual`) or `workflow_dispatch`; not scheduled. Supports `dry_run`, `no_resume`, a comma-separated `repos` filter, and `skip_code` / `skip_commits` toggles.
- `twitter-ingestion.yml` — remote Twitter/X timeline ingestion on a self-hosted macOS runner. Triggered by `workflow_dispatch` only; not scheduled. Supports `dry_run`, `reset_state` (ignore saved `since_id`), and an optional `max_tweets` cap. Do not install a local LaunchAgent for Twitter ingestion.
- `nightly-semantic-maintenance.yml` — **retired from automatic operation by the source-first cutover**. Manual `workflow_dispatch` only; the `cron: "20 7 * * *"` schedule was removed. Accepts `plan` or `live` mode and a `max_applied` cap. Guards that the retired Worker semantic slice (`SEMANTIC_SLICE_SIZE == 0`) remains disabled. Remains as a manual-only rollback path while the legacy index is archived.
- `nightly-sleep-report.yml` — **retired from automatic operation by the source-first cutover**. Manual `workflow_dispatch` only; the `cron: "45 8 * * *"` schedule was removed. Runs `scripts/nightly_semantic_maintenance.py --check-latest --max-age-hours 4` to inspect the archived legacy system.
- `worker-runtime-tests.yml` — push/PR-triggered CI for `cloudflare-mcp/mcp-server/**`, `Makefile`, `README.md`, and `docs/testing-matrix.md`: type-check + `npm run test:worker`. See [Testing guidance](testing.md).

The scheduled sequence after cutover is: source-first rebuild at `06:30 UTC`, OpenWiki update at `08:00 UTC`. The legacy Worker Dream cron at `07:10 UTC` is gone (`wrangler.json` top-level `triggers.crons: []`). The ingestion workflows have no fixed schedule and are expected to be dispatched externally (or manually) to fit the self-hosted macOS runner's availability.

## Useful source anchors

- `scripts/`
- `scripts/source_first_rebuild.py`
- `scripts/nightly_orchestrator.py`
- `scripts/nightly_semantic_maintenance.py`
- `scripts/semantic_candidate_planner.py`
- `scripts/run_orchestrator_launchd.sh`
- `scripts/run_nightly_ingestion.sh`
- `scripts/install_global_repo_agent_context_hook.sh`
- `scripts/install_repo_agent_context_hook.sh`
- `shared/source_first_config.json`
- `shared/source_first_suppressions.json`
- `.github/workflows/`
- `.github/workflows/source-first-rebuild.yml`
- `docs/testing-matrix.md`
