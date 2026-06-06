# PKS Phase 8 Builder Plan: Retrieval Upgrade

- Status: Builder-ready implementation plan
- Date: 2026-06-05
- Scope: Phase 8 offline retrieval upgrade and eval harness
- Prior phase: Phase 7 acceptance harness is green

## Objective

Add a deterministic hybrid retrieval layer over Phase 7 artifacts so PKS can
prove the new read path before changing live MCP search.

Phase 8 ranks over:

- compiled current claims
- unexpired provisional claims
- observations for evidence/history queries
- read-only memory blocks

It combines lightweight text matching, entity matching, optional vector scores,
temporal query classification, lane-aware surface selection, and stale-current
exclusion.

## Files To Add Or Update

- `distillation/models/phase8.py`
- `tests/python/test_phase8_retrieval.py`
- `tests/fixtures/phase8_retrieval_fixture.json`
- `distillation/models/__init__.py`
- `tests/fixtures/README.md`
- `docs/pks-memory-upgrade-checklist.md`

## In Scope

- Query classification:
  - current answer
  - evidence/history
  - point-in-time
  - procedural/policy
- Temporal query signal extraction.
- Lane-aware candidate construction from Phase 7 current view and blocks.
- Hybrid deterministic ranking:
  - lexical/full-text overlap
  - entity overlap
  - optional vector score
  - source priority
  - temporal intent boost/penalty
- Read-only retrieval evals:
  - recall probes
  - stale-current probes
  - memory-block probes
  - procedural/policy pointer probes

## Out Of Scope

- Live Cloudflare Worker search changes.
- Redis or Vector writes.
- Embedding generation.
- LLM reranking.
- Dream apply or rollback changes.

## Acceptance Tests

Run:

```bash
distillation/venv/bin/python -m unittest tests.python.test_phase8_retrieval
make test-python-checker
python3 -m json.tool tests/fixtures/phase8_retrieval_fixture.json >/dev/null
git diff --check
```

Expected:

- Focused Phase 8 tests pass.
- Full Python checker remains green.
- Fixture JSON validates.
- Diff whitespace check is clean.
- No credentials appear in Phase 8 files.

## Completion Definition

Phase 8 is complete when the offline retrieval layer can show, through fixture
evals, that current-answer queries prefer compiled current claims, evidence
queries can reach observations, policy/procedural queries prefer memory-block
pointers, and stale claims stay out of current-answer results.

Live MCP search wiring is a later integration step after this offline read-path
contract is green.
