# PKS Phase 7C Builder Plan: Compiled Current View

- Status: Builder-ready implementation plan
- Date: 2026-06-05
- Scope: Phase 7C offline current projection and compile proposal grading
- Prior phases:
  - Phase 7A: observation and compiled-claim schema
  - Phase 7B: temporal normalization and entity linking

## Objective

Add a pure offline Phase 7C layer that turns Phase 7 observations and compiled
claims into a deterministic current-view projection, then models the next Dream
compile operations as proposal records with deterministic hard-gate grading.

This phase does not change live Redis, Vector, MCP retrieval, Cloudflare Worker
Dream apply, or rollback behavior.

## Files To Add Or Update

- `distillation/models/phase7c.py`
- `tests/python/test_phase7c_current_view.py`
- `tests/fixtures/phase7c_current_projection_fixture.json`
- `distillation/models/__init__.py`
- `tests/fixtures/README.md`
- `docs/pks-memory-upgrade-checklist.md`

## In Scope

- Offline compiled-current-view generator.
- Separate lists for:
  - durable current claims
  - unexpired provisional `pending_compile` claims
  - excluded claims with exclusion reasons
- Synthetic current projection fixture.
- Compile proposal operation records for:
  - `compile_claim`
  - `supersede_claim`
  - `mark_current`
- Deterministic grade checks for compile operations:
  - operation IDs
  - allowed operation type
  - source observation evidence
  - taxonomy decision labels
  - expected revisions for mutated claims
  - rollback metadata
  - scoped exception scope
  - supersession subject/entity relation
  - refinement support preservation
  - temporal expiry validity window
  - mark-current TTL/source-authority/conflict checks
  - procedural memory excluded from semantic compiler mutation

## Out Of Scope

- Live Dream operation support in `cloudflare-mcp/mcp-server/src/dream.ts`.
- Redis or Vector writes.
- Default MCP retrieval changes.
- Automatic LLM contradiction classification.
- Memory block schema.
- Pre/post Dream outcome rollback.

## Acceptance Tests

Run:

```bash
distillation/venv/bin/python -m unittest tests.python.test_phase7c_current_view
make test-python-checker
python3 -m json.tool tests/fixtures/phase7c_current_projection_fixture.json >/dev/null
git diff --check
```

Expected:

- Focused 7C tests pass.
- Full Python checker remains green.
- Fixture JSON validates.
- Diff whitespace check is clean.
- No credentials appear in Phase 7C files.

## Completion Definition

Phase 7C is complete when the offline projection, proposal operation records,
deterministic grade checks, fixture coverage, exports, and checklist updates are
implemented and tested. The live Dream apply path remains intentionally
unchanged until a later phase.
