# PKS Phase 10 Builder Plan: Phase 9 Staging Activation

- Status: Builder-ready implementation plan
- Date: 2026-06-07
- Scope: Make the Phase 9 outcome gate operational in the staging smoke path
- Prior phase: Phase 9 outcome-gated Dream apply is implemented and pushed

## Objective

Phase 9 added the mechanism. Phase 10 turns it on in the staging harness without
touching production data.

Build:

- a small staging-safe Phase 9 outcome probe set
- seeding support for `dream:outcome_probes`
- staging smoke apply arguments that enable `phase9_outcome_gate`
- validation that the staging apply reports a passed Phase 9 gate before the
  normal rollback drill runs

## Files To Add Or Update

- `tests/fixtures/phase9_staging_outcome_probes.json`
- `scripts/seed_staging_env.py`
- `scripts/run_e2e_staging.py`
- `tests/python/test_phase10_staging_phase9.py`
- `tests/fixtures/README.md`
- `docs/pks-memory-upgrade-checklist.md`
- `Makefile`

## In Scope

- Write a bounded probe fixture against `sample_memory_fixture.json` entries.
- Validate probe fixture shape locally.
- Seed probes into staging Redis under `dream:outcome_probes`.
- Include probe metadata in staging seed reports.
- Enable Phase 9 gate in the staging Dream apply call by default.
- Keep `phase9_auto_rollback` off in staging smoke because the existing R5 flow
  performs an explicit rollback drill after verifying post-apply state.
- Add a `--skip-phase9-gate` escape hatch for staging smoke debugging.

## Out Of Scope

- Deploying staging.
- Running live staging smoke.
- Enabling production cron Phase 9 flags.
- Writing production validation ledger entries.
- Adding LLM judges, embeddings, or browser automation.

## Acceptance Tests

Run:

```bash
python3 -m json.tool tests/fixtures/phase9_staging_outcome_probes.json >/dev/null
distillation/venv/bin/python -m unittest tests.python.test_phase10_staging_phase9
make test-python-checker
cd cloudflare-mcp/mcp-server && npm run type-check
git diff --check
```

Expected:

- The staging probe fixture validates.
- Unit tests prove probe loading, Redis payload shape, and staging apply
  arguments.
- Full Python checker remains green.
- Worker type-check remains green.
- No staging or production network calls were run.

