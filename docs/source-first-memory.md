# Source-First Memory

Source-First Memory is the production replacement for the self-modifying PKS
memory model. It retrieves source-backed evidence without trying to
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

Every record also has a stable `source_id`, which groups all chunks from the
same file or logical session for `get_deep`.

The index contains no context classifier, injection tier, salience score,
access count, or LLM-generated canonical view.

Project evidence comes from source documents such as README, PRD, spec,
report, handoff, FABLE, and ARJUN files. Per-project `CLAUDE.md`, `AGENTS.md`,
and generated `.pks/agent-context` views are excluded so global instruction
updates cannot masquerade as recent project facts. The global
`AGENT_MEMORY.md` and `AGENTS.md` remain explicit pinned sources.

Recent Claude Code and Codex conversational turns are integrated directly as
`working_context` evidence. System/developer messages, tool calls, and tool
outputs are excluded. Deterministically identified retrieval-validation prompts
and their PASS/FAIL reports are also excluded, preventing a negative-control
query copied into a session from retrieving its own test transcript.
Credentials are redacted before checksums, embeddings, artifacts, or remote writes. Session records use logical
`session://<surface>/<session_id>` locators rather than local raw-log paths.

## Atomic generations

Every rebuild writes a complete candidate generation into:

- its own Upstash Vector namespace;
- generation-scoped Redis evidence keys;
- a generation manifest, project catalog, exact-project evidence maps, and
  suppression policy.

The builder stages without touching `sf:current_generation`, verifies storage,
then executes the exact production search implementation against the staged
generation. Only a candidate that passes every retrieval probe is promoted.
A failed build or evaluation cannot replace the last working generation.

## Retrieval

When a query explicitly names a known project, its deterministic source records
are selected before the general semantic candidates. A bounded inverted index
also recovers opaque identifiers and strong exact lexical phrases that vector
top-K misses. Ranking within that set is fixed and inspectable:

- 70% semantic similarity;
- 15% lexical overlap;
- 10% source authority;
- 5% source recency.

For `working_context` only, retrieval adds:

```text
0.08 * semantic_similarity * exp(-ln(2) * age_days / 3)
```

The lift describes attention, not authority. It may reorder evidence that
already qualifies, but it can never be what admits evidence: the relevance
floor is applied to the base score, before the lift is added. Multiplying the
lift by semantic relevance bounds its size; applying the floor to the base
score is what actually prevents an unrelated recent session being rescued.
(Before 2026-08-21 the floor was applied after the lift, and an unrelated
session with base 0.6292 was admitted at 0.6759.)

Byte-identical results collapse by `content_checksum` while preserving
alternate provenance. General results below `0.65` are omitted; if none remain,
the response explicitly abstains rather than returning confident-looking noise.

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

Stage without promoting:

```bash
ingestion/.venv/bin/python scripts/source_first_rebuild.py --stage
```

Verify and promote the staged generation:

```bash
ingestion/.venv/bin/python scripts/source_first_rebuild.py --verify-generation sf_YYYYMMDDTHHMMSSZ
ingestion/.venv/bin/python scripts/source_first_rebuild.py --promote-generation sf_YYYYMMDDTHHMMSSZ
```

Verify the currently serving generation:

```bash
ingestion/.venv/bin/python scripts/source_first_rebuild.py --verify-current
```

The GitHub Actions workflow `Source-First Memory Rebuild` is the only scheduled
maintenance job after cutover. It runs every two hours, meeting the recent
session freshness SLA. GitHub owns the schedule; its self-hosted Mac runner
provides read access to Dropbox and raw agent sessions. There is no local
scheduled job.
