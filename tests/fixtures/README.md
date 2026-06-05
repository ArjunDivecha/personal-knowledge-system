# Fixture Bundles

Fixture bundles define small, frozen memory states for testing.

The immediate purpose is:

- seed staging
- drive deterministic retrieval tests
- create stable Dream archive candidates

## Bundle Format

A bundle is a JSON object with:

- `metadata`
- `knowledge_entries`
- `project_entries`

Each entry item should follow the existing `to_dict()` shape used by:

- `KnowledgeEntry`
- `ProjectEntry`

The sample file is:

- [sample_memory_fixture.json](/Users/arjundivecha/Dropbox/AAA%20Backup/A%20Working/Memory/knowledge-system/tests/fixtures/sample_memory_fixture.json)

Outcome-quality probe files:

- `recall_probes.json` contains carry-forward recall probes for M6.
- `temporal_staleness_probes.json` contains temporal freshness probes for M8. Disabled examples document the schema only; enable temporal probes only when the stale/fresh expectation is source-verified.

Phase 7A schema fixture:

- `phase7_migration_fixture.json` contains synthetic legacy entries, Phase 7 observations, compiled claims, and supersession edges for offline observation/claim schema tests. It must stay synthetic and must not include live user facts.

Phase 7B enrichment fixtures:

- `phase7b_temporal_outcome_probes.json` contains synthetic temporal normalization probes for current/future/expired/unknown outcomes.
- `phase7b_entity_index_fixture.json` contains synthetic observations and expected source-aware entity index rows.

Phase 7C current-view fixtures:

- `phase7c_current_projection_fixture.json` contains synthetic current projection and compile proposal-operation cases.

Phase 7D memory-block fixtures:

- `phase7d_memory_blocks_fixture.json` contains synthetic read-only operator, project-status, and policy-pointer block cases.

## Design Rules

- keep bundles small
- keep ids stable
- use realistic metadata
- prefer deterministic timestamps
- make archive candidates obvious
- include at least one durable Tier 1 item and one weak Tier 3 item
