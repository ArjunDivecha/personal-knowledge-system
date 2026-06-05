# PKS Phase 7D Builder Plan: Memory Blocks

- Status: Builder-ready implementation plan
- Date: 2026-06-05
- Scope: Phase 7D offline memory block schema and read-only block builders
- Prior phases:
  - Phase 7A: observation and compiled-claim schema
  - Phase 7B: temporal normalization and entity linking
  - Phase 7C: compiled current-view projection and compile operation grading

## Objective

Add a pure offline memory-block layer for compact always-visible context.
Blocks are a presentation layer over Phase 7C current claims and versioned
policy pointers. They are not a new live mutation path.

## Files To Add Or Update

- `distillation/models/phase7d.py`
- `tests/python/test_phase7d_memory_blocks.py`
- `tests/fixtures/phase7d_memory_blocks_fixture.json`
- `distillation/models/__init__.py`
- `tests/fixtures/README.md`
- `docs/pks-memory-upgrade-checklist.md`

## In Scope

- Memory block schema with:
  - stable block ID
  - label
  - description
  - value
  - scope and optional scope ref
  - read-only flag
  - character limit
  - compiled claim provenance
  - source observation and source path provenance
- Read-only operator profile block.
- Current project status block.
- Policy/procedural pointer block.
- Pure helper to build the Phase 7D block set from a Phase 7C current view.
- Tests for:
  - round-trip serialization
  - size limits
  - source traceability
  - read-only enforcement
  - generated block content
  - policy pointers staying pointer-only

## Out Of Scope

- Live MCP retrieval changes.
- Redis or Vector writes.
- Agent-writable memory blocks.
- Automatic procedural rule compilation.
- Operator-profile inference from raw conversation text.
- UI changes.

## Acceptance Tests

Run:

```bash
distillation/venv/bin/python -m unittest tests.python.test_phase7d_memory_blocks
make test-python-checker
python3 -m json.tool tests/fixtures/phase7d_memory_blocks_fixture.json >/dev/null
git diff --check
```

Expected:

- Focused 7D tests pass.
- Full Python checker remains green.
- Fixture JSON validates.
- Diff whitespace check is clean.
- No credentials appear in Phase 7D files.

## Completion Definition

Phase 7D is complete when read-only memory blocks can be represented,
generated from compiled current claims or policy file pointers, validated for
size/source traceability, exported, documented, and tested. Production retrieval
continues to use the existing path until Phase 8.
