# Retired LaunchAgents

## com.arjun.knowledge-ingestion (23:00) + com.arjun.knowledge-ingestion-2am — retired 2026-09-04

The nightly legacy ingestion (Twitter → GitHub → agent sessions → `ke_*` entries → Dream
judge). Ran 30/30 nights OK through 2026-09-04, but since the 2026-08-09 source-first cutover
**nothing in production reads what it writes**: the Worker's source-first branches never touch
`ke_*`, `read_tweet` fetches by URL, and the `github` tool queries the GitHub API live. It was
kept only as a rollback target for the legacy index; that target is now frozen at its
2026-09-04 state.

Unloaded with `launchctl bootout gui/<uid>/<label>`; the plists were moved here from
`~/Library/LaunchAgents/` (they were real files, not symlinks). Removed from the Overseer
manifest the same day (replaced by `pks-rebuild-kicker`).

**To roll back to legacy serving** (not expected): re-run the ingestion via the
`workflow_dispatch`-only GitHub workflows (`agent-session-ingestion.yml`,
`github-ingestion.yml`, `twitter-ingestion.yml`, `nightly-semantic-maintenance.yml`) or
`bash scripts/run_nightly_ingestion.sh` manually, then flip `SOURCE_FIRST_MODE` off in
`wrangler.json`. To reinstall the schedule, copy the plists back and `launchctl bootstrap`.

Evaluation that led to this: `runs/20260904T2110Z_nightly_update_eval/REPORT.md`.
