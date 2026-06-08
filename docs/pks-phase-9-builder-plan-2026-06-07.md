# PKS Phase 9 Builder Plan: Quality-Gated Dream

- Status: Builder-ready implementation plan
- Date: 2026-06-07
- Scope: Phase 9 outcome gate for Dream apply/rollback decisions
- Prior phase: Phase 8 retrieval contract is implemented offline and wired into live Worker search scoring

## Objective

Add a deterministic outcome-quality gate around Dream mutations so the system
can distinguish "safe to write" from "improves or preserves recall."

The existing Dream grade remains the pre-write operational safety gate. Phase 9
adds a separate pre/post outcome gate over Phase 8 probes:

- run retrieval probes before a proposed Dream apply
- run the same probes after the apply
- detect any probe that passed before and fails after
- recommend rollback through the existing `rollback_dream_apply` path when a
  regression is attributable to the apply
- emit a compact validation-ledger details payload without writing the ledger
  unless an explicit production command opts in

## Files To Add Or Update

- `distillation/models/phase9.py`
- `cloudflare-mcp/mcp-server/src/phase9OutcomeGate.ts`
- `cloudflare-mcp/mcp-server/src/dream.ts`
- `cloudflare-mcp/mcp-server/src/index.ts`
- `cloudflare-mcp/mcp-server/src/env.d.ts`
- `cloudflare-mcp/mcp-server/test/dream-replay.test.ts`
- `tests/python/test_phase9_outcome_gate.py`
- `tests/fixtures/phase9_outcome_gate_fixture.json`
- `distillation/models/__init__.py`
- `tests/fixtures/README.md`
- `docs/pks-memory-upgrade-checklist.md`

## In Scope

- Pure, offline Phase 9 report types:
  - outcome regression
  - outcome gate report
  - rollback recommendation
- Gate comparison logic over serialized `Phase8EvalReport` objects.
- Regression detection:
  - a check that passed before apply fails after apply
  - a check that existed before apply is missing after apply
  - a new post-apply failure appears when the pre baseline was clean
- Rollback recommendation payload that can feed the existing Worker
  `rollbackDreamApply` / `rollback_dream_apply` interface.
- Opt-in Worker apply wiring controlled by:
  - `phase9_outcome_gate`
  - `phase9_auto_rollback`
  - `phase9_probe_set_key`
  - `phase9_write_validation_ledger`
- Validation-ledger detail builder for later opt-in ledger writes.
- Fixture and tests proving no live Redis, Vector, workflow, SDK, or browser
  dependency is required.

## Out Of Scope

- Running live production Dream apply.
- Running live rollback.
- Writing the validation ledger.
- Triggering GitHub Actions, SDK auth preflights, or browser automation.
- Changing the existing Dream operational grade semantics.
- Adding LLM judges or embedding generation.

## Outcome Contract

Phase 9 consumes Phase 8 eval reports:

```python
pre_report = run_phase8_retrieval_fixture(pre_fixture)
post_report = run_phase8_retrieval_fixture(post_fixture)
gate = evaluate_phase9_outcome_gate(pre_report, post_report)
```

The gate passes only when:

- the pre baseline passed
- every pre-check is present after apply
- no previously passing check regresses
- no new post-apply failure appears

If the pre baseline is already failing, Phase 9 marks the gate failed but does
not recommend rollback, because the failure cannot be attributed to the apply.

If the post state regresses, Phase 9 marks:

```json
{
  "passed": false,
  "rollback_required": true,
  "rollback_reason": "phase9_outcome_probe_regression"
}
```

## Rollback Integration

Phase 9 does not create a new rollback mechanism. It prepares a deterministic
recommendation for the existing rollback path:

```json
{
  "required": true,
  "ready": true,
  "proposal_id": "dream_proposal_id",
  "apply_mutation_id": "apply_mutation_id",
  "rollback_mutation_id": "phase9_rollback_mutation_id",
  "reason": "phase9_outcome_probe_regression: check_a, check_b"
}
```

The Worker already has snapshot-backed rollback support for Dream apply records.
Phase 9 should call that path only when production wiring is explicitly enabled
and the gate report has enough proposal/apply identity to build a ready
rollback request.

## Validation-Ledger Integration

Phase 9 exposes details suitable for the existing validation ledger:

```python
details = build_phase9_validation_gate_details(gate)
```

The gate name should be `dream_outcome_quality` when a production runner writes
it. This implementation must not write the ledger during unit tests or fixture
evals.

## Acceptance Tests

Run:

```bash
python3 -m json.tool tests/fixtures/phase9_outcome_gate_fixture.json >/dev/null
distillation/venv/bin/python -m unittest tests.python.test_phase9_outcome_gate
distillation/venv/bin/python -m unittest tests.python.test_phase8_retrieval tests.python.test_phase9_outcome_gate
make test-python-checker
cd cloudflare-mcp/mcp-server
npm run type-check
npx vitest run --no-file-parallelism test/phase8-retrieval.test.ts test/dream-replay.test.ts test/scheduled.test.ts
git diff --check
```

Expected:

- Phase 9 fixture JSON validates.
- Focused Phase 9 tests pass.
- Phase 8 and Phase 9 pass together.
- Full Python checker remains green.
- Worker type-check passes.
- Local Worker replay/scheduled tests prove gated apply, ledger write, and
  auto-rollback behavior.
- Diff whitespace check is clean.
- No production memory mutation commands were run.

## Completion Definition

Phase 9 is complete when the repo has a deterministic pre/post outcome gate that
can prove a Dream apply preserved Phase 8 recall probes, detect a post-apply
retrieval regression, and prepare a rollback recommendation through the
existing Dream rollback contract without mutating live storage in tests.
