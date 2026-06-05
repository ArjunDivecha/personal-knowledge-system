# PKS Phase 7B Builder Plan - Temporal Normalization And Entity Linking

- Status: Builder-ready and implemented in this slice
- Date: 2026-06-05
- Scope: Offline enrichment helpers only
- Repo root: `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system`
- Prerequisite: Phase 7A offline observation/claim schema

## 0. Objective

Add a pure offline Phase 7B enrichment layer that normalizes simple temporal
phrases and attaches stable entity IDs to Phase 7A observations and compiled
claims. Do not change live Redis, Vector, Dream, Worker, MCP retrieval, or
ingestion write behavior.

## 1. Files To Add

- `distillation/models/phase7b.py`
- `tests/python/test_phase7b_enrichment.py`
- `tests/fixtures/phase7b_temporal_outcome_probes.json`
- `tests/fixtures/phase7b_entity_index_fixture.json`

## 2. Files To Edit

- `distillation/models/__init__.py`
  - export Phase 7B dataclasses and helpers
- `docs/pks-memory-upgrade-checklist.md`
  - mark Phase 7B checklist items complete after validation
- `tests/fixtures/README.md`
  - document the new synthetic fixtures

## 3. Hard Non-Goals

Do not implement:

- live Dream compile operations
- MCP retrieval changes
- Redis or Vector writes
- LLM-based entity extraction
- full natural-language temporal parsing
- graph backend storage
- Phase 7C compiled-current-view proposal operations

## 4. Temporal Scope

Phase 7B supports deterministic, narrow temporal normalization:

- ISO dates: `YYYY-MM-DD`
- relative days: `today`, `tomorrow`, `yesterday`
- month windows: `in July`, `July 2026`
- simple status language:
  - future: `going to`, `will`, `planned`, `upcoming`
  - historical: `completed`, `finished`, `shipped`, `was`

The helper must return `unknown` when a phrase is ambiguous rather than
guessing.

## 5. Entity Scope

Entity extraction is deterministic and lightweight:

- backticked artifact/tool/path spans
- all-caps acronyms of length 2-10
- title-case spans of up to four words

Entity IDs are stable:

```python
stable_entity_id(name) -> "ent_<16 hex chars>"
```

Normalize names with casefold plus whitespace collapse. Do not use embeddings or
LLMs in 7B.

## 6. Expected Helpers

- `stable_entity_id`
- `normalize_entity_name`
- `extract_entity_mentions`
- `build_entity_index`
- `normalize_temporal_text`
- `enrich_observation_temporal`
- `enrich_claim_temporal`
- `enrich_observations_phase7b`
- `enrich_claims_phase7b`
- `evaluate_phase7b_temporal_probe`

## 7. Validation

Run:

```bash
distillation/venv/bin/python -m unittest tests.python.test_phase7b_enrichment
make test-python-checker
python3 -m json.tool tests/fixtures/phase7b_temporal_outcome_probes.json >/dev/null
python3 -m json.tool tests/fixtures/phase7b_entity_index_fixture.json >/dev/null
git diff --check
```

Success means Phase 7B is ready as an offline enrichment layer. Phase 7C remains
separate and should consume these helpers when building compiled current views.
