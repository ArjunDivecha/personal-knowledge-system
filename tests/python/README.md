# Python Test Suite

This directory is reserved for `pytest`-based tests covering:

- parsing
- distillation
- backfill
- thin index generation
- staging seed validation

It also covers ingestion reliability and billing safety:

- `test_sdk_client.py` — Agent SDK wrapper: key scrubbing, fallback budgets/call caps.
- `test_check_claude_sdk_auth.py` — the raw SDK auth probe (retry/fail-closed).
- `test_check_claude_sdk_auth_noninteractive.py` — the **no-browser** preflight:
  env hardening, key scrubbing, own-session subprocess, kill-on-timeout.
- `test_ingestion_billing_routes.py` — workflows + launchd wrapper route to API
  fallback (never skip) and reference the no-browser preflight + `BROWSER` guard.
- `test_github_run.py` — GitHub ingestion incl. per-repo fault isolation: one
  repo's extraction failure does not abort the run; good repos still save and the
  failed repo is left unmarked (retried next run).
- `test_nightly_health_monitor.py` — health-monitor verdict logic (PASS/WARN/FAIL,
  storm-hit → FAIL, marker `ok=false` → FAIL, redis-mirror-failure → WARN
  (disk-backed/self-healing), stale-daily-log scoping).

Run the ingestion/auth/monitor suite with:

```bash
ingestion/.venv/bin/python -m unittest \
  tests.python.test_sdk_client \
  tests.python.test_check_claude_sdk_auth \
  tests.python.test_check_claude_sdk_auth_noninteractive \
  tests.python.test_ingestion_billing_routes \
  tests.python.test_nightly_health_monitor
```
