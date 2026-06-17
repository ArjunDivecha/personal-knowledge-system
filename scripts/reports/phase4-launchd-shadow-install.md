# Phase 4 - launchd shadow sidecar: INSTALL evidence

Installed 2026-06-16 22:12 PDT. Sidecar live; old schedulers untouched; shadow-only.

## Implementation + install acceptance criteria - MET
- plist passes plutil -lint; installer has install/status/uninstall
- supervise window logic + 6 unit tests + live outside-window no-op
- wrapper preflight exits 0 (auth_route=sdk, no browser)
- LaunchAgent installed under new label ONLY; old ingestion jobs still LOADED
- Cloudflare cron 10 7 * * * and GitHub nightly-sleep-report.yml 45 8 * * * unchanged
- user-facing reports still legacy (PKS_NIGHTLY_SOURCE_OF_TRUTH=legacy)
- RunAtLoad fired and no-op'd (outside window), exit 0, no ledger created

## Bug surfaced + fixed
- resume() catch-up cutoff used TODAY's 08:45 -> an evening 23:20 start mislabeled 'missed'. supervise() now uses the morning-after-target 08:45 cutoff (unit-tested).

## Pending: first scheduled night (overnight)
- First fire 23:20 PT tonight targets 2026-06-16. After it runs, capture phase4-launchd-shadow-2026-06-16.{json,md} and compare with the legacy nightly.

## Uninstall
- bash scripts/install_orchestrator_launchd_shadow.sh uninstall  (removes only the new label)
