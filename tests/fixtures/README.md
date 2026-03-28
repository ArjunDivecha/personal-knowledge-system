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

## Design Rules

- keep bundles small
- keep ids stable
- use realistic metadata
- prefer deterministic timestamps
- make archive candidates obvious
- include at least one durable Tier 1 item and one weak Tier 3 item
