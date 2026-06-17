# PKS Nightly Orchestrator Phase 4 PRD

Date: 2026-06-17
Status: implemented + installed 2026-06-16 22:12 PT; first shadow night passed, legacy comparison found repaired Twitter-state failure
Owner: PKS nightly orchestrator
Phase: 4 - launchd shadow sidecar on anchored M4

## Implementation Status (2026-06-16 PT)

Rollout steps 1-8 done; the sidecar is installed and live, shadow-only, old
schedulers untouched. Evidence:
`scripts/reports/phase4-launchd-shadow-install.{json,md}`.

- `orchestrator/engine.py`: pure `decide_target_date(now)` (23:20-23:59 -> today,
  00:00-08:50 -> yesterday, else skip) + `Orchestrator.supervise(now)`; CLI
  `supervise` subcommand. 6 window unit tests; full suite **47 pass**.
- `scripts/run_orchestrator_launchd.sh` v2: `supervise` action + shadow env
  (`PKS_ORCH_DREAM_CLIENT=http`, `PKS_ORCH_ALLOW_MUTATION=0`,
  `PKS_NIGHTLY_SOURCE_OF_TRUTH=legacy`, no-browser guards) re-asserted after
  sourcing `.env`. Idempotent/supervisory.
- `scripts/com.arjun.pks-nightly-orchestrator.shadow.plist`: RunAtLoad +
  StartCalendarInterval every ~30 min 23:20-08:50; `caffeinate -i ... supervise`.
  Passes `plutil -lint`.
- `scripts/install_orchestrator_launchd_shadow.sh`: install/status/uninstall;
  touches only the new label; prints old-job status.
- Installed under `gui/$UID/com.arjun.pks-nightly-orchestrator.shadow`; RunAtLoad
  fired at 22:12 PT and no-op'd (outside window), exit 0, no ledger created.
  `com.arjun.knowledge-ingestion` + `-2am` still LOADED; Cloudflare cron
  `10 7 * * *` and GitHub `nightly-sleep-report.yml` (`45 8 * * *`) unchanged.

Bug surfaced + fixed: `resume()`'s catch-up cutoff compared `now` to *today's*
08:45, so an evening 23:20 start (numerically after 08:45) was wrongly marked
`missed`. `supervise()` uses a target-aware cutoff (08:45 the morning after the
target night); unit-tested.

First-night evidence captured in
`scripts/reports/phase4-launchd-shadow-2026-06-16.{json,md}`. The Phase 4
sidecar passed: terminal `completed_with_holds`, Worker Dream
`completed_shadow`, `executed_mode=shadow`, and `applied_count=0`. The legacy
comparison found an independent Twitter ingestion failure caused by missing
Redis-backed Twitter state; the state was synced and verified after the run.

## Summary

Phase 4 installs the new M4-owned orchestrator as a launchd sidecar in
shadow-validation mode.

This phase must not cut over production. The existing production schedules stay
active and remain the user-facing source of truth:

- `com.arjun.knowledge-ingestion` at 23:00 Pacific
- `com.arjun.knowledge-ingestion-2am` at 02:00 Pacific
- Cloudflare Worker cron `10 7 * * *` UTC for governed live Dream
- GitHub nightly Dream sleep report at `45 8 * * *` UTC

The new sidecar runs the orchestrator against the real production async Dream
Worker endpoint, but with mutations disabled and Dream executed in shadow.

## Current State

Phase 3 passed on the real production Worker:

- synthetic run date `2099-12-29`
- terminal orchestrator status `completed_with_holds`
- Worker Dream terminal `completed_shadow`
- `executed_mode=shadow`
- `applied_count=0`
- zero active-memory mutation
- same-host resume exercised after a real `DREAM_START` failure
- 41 Python orchestrator tests passed

Evidence is committed in:

- `scripts/reports/phase3-shadow-run-2099-12-29.json`
- `scripts/reports/phase3-shadow-run-2099-12-29.md`
- `scripts/reports/pks-nightly-2099-12-29.json`
- `scripts/reports/pks-nightly-2099-12-29.md`

Phase 4 starts only because the Phase 3 gate is satisfied.

## Product Goal

Install one new launchd job on the anchored M4 that:

- runs the orchestrator nightly in shadow-validation mode
- uses the production async Dream Worker HTTP client every night
- never enables local ingestion mutation
- never enables Worker live apply
- writes the normal orchestrator ledger and report
- attempts same-date resume/catch-up during the overnight window
- leaves old production schedules untouched
- makes the first five shadow nights easy to audit for Phase 5 cutover

## Non-Goals

Phase 4 does not:

- disable `com.arjun.knowledge-ingestion`
- disable `com.arjun.knowledge-ingestion-2am`
- disable Cloudflare cron
- disable GitHub nightly sleep report
- enable `PKS_ORCH_ALLOW_MUTATION`
- enable `PKS_ORCH_DREAM_LIVE_ENABLED`
- call live Dream apply from the async endpoint
- change the existing user-facing nightly report source of truth
- retire old repair scripts

## Launchd Shape

Add a separate LaunchAgent label:

```text
com.arjun.pks-nightly-orchestrator.shadow
```

Do not reuse or replace the old ingestion labels.

Recommended repo files:

```text
scripts/com.arjun.pks-nightly-orchestrator.shadow.plist
scripts/install_orchestrator_launchd_shadow.sh
scripts/run_orchestrator_launchd.sh
```

The plist should call:

```text
/usr/bin/caffeinate -i /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/scripts/run_orchestrator_launchd.sh supervise
```

Use launchd wall-clock triggers that stay within the overnight validation
window. The primary scheduled start should be 23:20 Pacific: late enough to
avoid the first few minutes of the existing 23:00 ingestion run, but before
midnight so the orchestrator's `auto` date naturally matches the nightly local
date.

Recommended `StartCalendarInterval` entries:

```text
23:20, 23:50,
00:20, 00:50,
01:20, 01:50,
02:20, 02:50,
03:20, 03:50,
04:20, 04:50,
05:20, 05:50,
06:20, 06:50,
07:20, 07:50,
08:20, 08:50
```

The repeated invocations are safe only if the wrapper is supervisory and
idempotent. It must no-op outside the allowed window and must resume the same
run date instead of starting a second run.

Use `RunAtLoad=true` only with the same supervisory guard, so reboot/login
catch-up is safe.

## Wrapper Contract

`scripts/run_orchestrator_launchd.sh` should keep its direct subcommands, but
Phase 4 should add `supervise`.

Required environment for every launchd orchestrator invocation:

```bash
PKS_ORCH_DREAM_CLIENT=http
DREAM_MCP_BASE_URL=https://mcp.dancing-ganesh.com
PKS_ORCH_ALLOW_MUTATION=0
PKS_NIGHTLY_SOURCE_OF_TRUTH=legacy
CI=1
BROWSER=/usr/bin/false
GIT_TERMINAL_PROMPT=0
PYTHONDONTWRITEBYTECODE=1
```

`supervise` must compute the target run date in Pacific time:

- from 23:20 through 23:59, target date is today
- from 00:00 through 08:50, target date is yesterday
- outside that window, exit 0 with a log line and no orchestrator call

`supervise` behavior:

- if no ledger exists before the catch-up cutoff, run/resume the target date
- if a ledger exists and is incomplete, resume the target date
- if a ledger exists and is terminal, exit 0
- after the 08:45 catch-up cutoff, do not start a new late run; let the
  orchestrator mark missed/reportable state if needed
- never run with `--mode live`
- never delete or overwrite an existing terminal ledger

The wrapper should log every decision to:

```text
ingestion/logs/orchestrator/launchd_YYYY-MM-DD.log
```

## Installer Contract

`scripts/install_orchestrator_launchd_shadow.sh` should support:

```bash
bash scripts/install_orchestrator_launchd_shadow.sh install
bash scripts/install_orchestrator_launchd_shadow.sh status
bash scripts/install_orchestrator_launchd_shadow.sh uninstall
```

Installer requirements:

- lint the plist with `plutil -lint`
- make `scripts/run_orchestrator_launchd.sh` executable
- copy only the new plist to `~/Library/LaunchAgents`
- use `launchctl bootstrap gui/$UID ...` or the modern equivalent
- use `launchctl enable gui/$UID/com.arjun.pks-nightly-orchestrator.shadow`
- do not unload, bootout, disable, edit, or replace old ingestion jobs
- print status for old jobs and the new job after install
- support uninstall for only the new shadow sidecar

## First-Night Evidence

For the first scheduled real-date shadow night, capture:

- new LaunchAgent plist path
- `launchctl print gui/$UID/com.arjun.pks-nightly-orchestrator.shadow`
- old launchd labels still loaded
- Cloudflare cron still configured
- GitHub nightly sleep report workflow still scheduled
- orchestrator ledger path
- `scripts/reports/pks-nightly-{run_date}.json`
- `scripts/reports/pks-nightly-{run_date}.md`
- Dream terminal status
- `executed_mode`
- `applied_count`
- held count
- before/after thin-index counts
- old nightly report path for the same night
- comparison summary

Recommended evidence output:

```text
scripts/reports/phase4-launchd-shadow-{run_date}.json
scripts/reports/phase4-launchd-shadow-{run_date}.md
```

## Acceptance Criteria

Phase 4 implementation is complete only when all of these are true:

- Phase 3 commit is on `origin/main`
- new launchd plist exists and passes `plutil -lint`
- installer has install/status/uninstall actions
- wrapper `supervise` is implemented and dry-run tested across time windows
- wrapper direct `preflight` exits 0 with the launchd environment
- new LaunchAgent is installed under the new label only
- old ingestion LaunchAgents remain loaded and unchanged
- Cloudflare cron remains active
- GitHub nightly sleep report remains scheduled
- first real-date sidecar run reaches terminal-OK
- first real-date sidecar run uses `HttpDreamClient`
- Worker Dream terminal is `completed_shadow`
- Dream `executed_mode` is `shadow`
- Dream `applied_count` is `0`
- report JSON and Markdown exist
- no active-memory counts change unexpectedly
- user-facing reports still come from the legacy path

## Validation Commands

Static validation:

```bash
cd "/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system"

plutil -lint scripts/com.arjun.pks-nightly-orchestrator.shadow.plist
bash -n scripts/run_orchestrator_launchd.sh
bash -n scripts/install_orchestrator_launchd_shadow.sh
PYTHONDONTWRITEBYTECODE=1 ingestion/.venv/bin/python -m pytest orchestrator/tests
git diff --check
```

Launchd state validation:

```bash
launchctl list | grep -E 'com.arjun.knowledge-ingestion|com.arjun.pks-nightly-orchestrator'
launchctl print "gui/$UID/com.arjun.pks-nightly-orchestrator.shadow"
```

Wrapper preflight validation:

```bash
PKS_ORCH_DREAM_CLIENT=http \
DREAM_MCP_BASE_URL=https://mcp.dancing-ganesh.com \
PKS_ORCH_ALLOW_MUTATION=0 \
PKS_NIGHTLY_SOURCE_OF_TRUTH=legacy \
CI=1 \
BROWSER=/usr/bin/false \
GIT_TERMINAL_PROMPT=0 \
PYTHONDONTWRITEBYTECODE=1 \
scripts/run_orchestrator_launchd.sh preflight
```

## Rollout Plan

1. Implement wrapper `supervise` with explicit shadow/HTTP environment.
2. Add tests for target-date/window selection if the logic lives outside pure
   shell; otherwise add a documented dry-run override and test it.
3. Add the new shadow LaunchAgent plist.
4. Add the install/status/uninstall script for only the new label.
5. Run static validation.
6. Run wrapper preflight.
7. Install the new LaunchAgent.
8. Verify old jobs are still loaded.
9. Wait for the first scheduled shadow sidecar run.
10. Capture Phase 4 evidence and compare against the legacy nightly artifacts.
11. Commit Phase 4 implementation and first-night evidence.

## Gate To Phase 5

Do not cut over after one good sidecar night.

Phase 5 may start only after:

- 5 consecutive terminal orchestrator shadow runs
- one deliberately interrupted sidecar run resumes successfully
- one missed-completion/dead-man drill is exercised in shadow validation
- no unexpected active-memory mutation across the validation window
- legacy and orchestrator reports are understandable side by side
- the orchestrator reaches terminal state before 08:30 Pacific during validation

## Builder Notes

The risky mistakes are:

- replacing an old LaunchAgent instead of adding a new sidecar
- letting launchd run the Phase 1 in-process Dream simulator instead of the
  real HTTP Worker client
- using `auto` after midnight and accidentally labeling the wrong night
- making `RunAtLoad=true` start a surprise daytime run
- treating a loaded plist as proof that the scheduled run succeeded
- interpreting proposal/status artifacts as active-memory mutation

Keep the sidecar boring: one new label, shadow only, HTTP Dream, no old scheduler
changes, clear evidence after the first real scheduled night.
