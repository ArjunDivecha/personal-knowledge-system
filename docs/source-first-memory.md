# Source-First Memory

Source-First Memory is the serving replacement for the self-modifying PKS
memory model. It retrieves recent, authoritative evidence without trying to
infer Arjun's identity, assign salience, reinforce accessed results, or rewrite
the corpus overnight.

## Data contract

Each evidence record is immutable and contains:

- exact source path;
- source timestamp;
- exact evidence text;
- content checksum;
- project and source kind;
- deterministic record ID.

The index contains no context classifier, injection tier, salience score,
access count, or LLM-generated canonical view.

Project evidence comes from source documents such as README, PRD, spec,
report, handoff, FABLE, and ARJUN files. Per-project `CLAUDE.md`, `AGENTS.md`,
and generated `.pks/agent-context` views are excluded so global instruction
updates cannot masquerade as recent project facts. The global
`AGENT_MEMORY.md` and `AGENTS.md` remain explicit pinned sources.

## Atomic generations

Every rebuild writes a complete candidate generation into:

- its own Upstash Vector namespace;
- generation-scoped Redis evidence keys;
- a generation manifest, project catalog, exact-project evidence maps, and
  suppression policy.

The builder verifies that every expected Redis record, vector, and project map
exists. Only then does it update `sf:current_generation`. A failed build cannot
replace the last working generation.

## Retrieval

When a query explicitly names a known project, its deterministic source records
are selected before the general semantic candidates. Ranking within that set is
fixed and inspectable:

- 70% semantic similarity;
- 15% lexical overlap;
- 10% source authority;
- 5% source recency.

Explicit suppression rules are applied before results are returned. A rule can
permit direct historical lookup while preventing the topic from appearing in
unrelated searches.

## Project status

Projects are derived from real folders and authoritative files. `active` means
the project has authoritative source activity within 90 days; otherwise it is
shown separately as `dormant`. Status is regenerated from the source rather
than mutated by Dream.

## Operations

Local build without publishing:

```bash
ingestion/.venv/bin/python scripts/source_first_rebuild.py
```

Build, verify, and atomically promote:

```bash
ingestion/.venv/bin/python scripts/source_first_rebuild.py --publish
```

Verify the currently serving generation:

```bash
ingestion/.venv/bin/python scripts/source_first_rebuild.py --verify-current
```

The GitHub Actions workflow `Source-First Memory Rebuild` is the only scheduled
maintenance job after cutover. GitHub owns the schedule; its self-hosted Mac
runner provides read access to the Dropbox sources. There is no local scheduled
job.
