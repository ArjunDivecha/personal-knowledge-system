# PKS Integrated Recent-Session Context PRD

**Date:** 2026-08-09
**Status:** Implemented 2026-08-10; production verification recorded in the source-first completion report
**Owner:** Arjun / PKS Core
**Runtime:** Source-first Cloudflare Worker
**Decision:** Recent Claude Code and Codex sessions become first-class contextual evidence inside the existing source-first generation, index, and retrieval path.

## 1. Product decision

PKS will have one corpus, one evidence contract, one atomic generation, and one search path.

Recent Claude Code and Codex sessions will be represented as `working_context` evidence in the same Redis and Vectorize generation as authoritative project files. Retrieval will apply a small, relevance-gated temporal attention prior to recent working context. It will not create a second “recent sessions” lane, merge a second result list after retrieval, or turn session text into canonical memory.

The system must distinguish two independent concepts:

- **Authority:** how strongly a record can establish what is true.
- **Attention:** how likely a record is to reflect what Arjun currently has in mind.

Authoritative source files remain the primary evidence for facts, decisions, specifications, and project state. Recent sessions help PKS understand the active frame, vocabulary, unresolved questions, and work in progress.

## 2. Problem

Arjun usually asks PKS about work that is already active in recent Claude Code or Codex sessions. The current source-first runtime does not represent that attention state:

- The source-first scanner deliberately excludes dot directories, so generated `.pks/agent-context` files do not enter the live generation.
- The older agent-session ingester writes to the legacy knowledge path rather than the current source-first generation.
- Repository commits are an unreliable boundary for “what is top of mind.” Important work may exist in a session before it is committed or written into a durable project document.
- The current general source-recency term is intentionally slow-moving and cannot express the short half-life of active working context.
- A separate recent-session search followed by query-time result merging would duplicate retrieval policy and introduce another failure boundary.

The result is a mismatch between PKS retrieval and Arjun’s mental context. The fix must preserve the simplicity and evidence discipline of the source-first system.

## 3. Goals

1. Make relevant recent Claude Code and Codex sessions materially more likely to appear for questions about current work.
2. Keep recent sessions in the same ingestion, publication, retrieval, and provenance model as all other evidence.
3. Preserve authoritative project files as the source of truth.
4. Make every ranking contribution inspectable and deterministic.
5. Capture new sessions within two hours without adding a local LaunchAgent or a second serving system.
6. Preserve atomic promotion: a complete generation is published or the previous generation remains live.
7. Prevent credentials, tool noise, and system instructions from entering the evidence corpus.
8. Add no LLM dependency to ingestion, ranking, or freshness.

## 4. Non-goals

- No separate vector namespace or result lane for recent sessions.
- No query-time merge of “memory results” and “session results.”
- No LLM-generated canonical summaries, inferred facts, or autonomous memory writes.
- No access-frequency reinforcement, salience counters, dream/consolidation loop, or user-behavior feedback loop.
- No mutation of source session logs.
- No use of session evidence to update authoritative project status, `last_touched`, or lifecycle fields.
- No full transcript archive inside PKS. Only bounded, recent, redacted evidence is included in the current generation.
- No Cursor session support in v1. The initial surfaces are Claude Code and Codex only.
- No replacement for durable documentation. Important conclusions still belong in project files.

## 5. Design principles

### 5.1 One integrated evidence system

All evidence uses the same `EvidenceRecord`, deterministic ID rules, checksum rules, embedding path, lexical map, publisher, generation manifest, and search endpoint.

### 5.2 Truth and attention are separate dimensions

`authority` continues to describe evidentiary reliability. The new temporal attention score only describes current relevance. A very recent session cannot acquire the authority of a specification or source file merely because it is new.

### 5.3 Recency must be relevance-gated

Fresh but unrelated session text receives no meaningful advantage. The attention prior is multiplied by semantic relevance rather than added as a free-standing freshness bonus.

### 5.4 Provenance is part of the product

Callers must be able to see whether a result came from an authoritative source or working context, which surface produced it, when it was observed, and how much the attention prior affected its rank.

### 5.5 Failure preserves the last good generation

If required session roots cannot be read, parsing or redaction fails closed, evaluation fails, or publication is incomplete, the candidate generation is not promoted.

## 6. Unified evidence contract

Extend the current source-first `EvidenceRecord` with the following fields:

| Field | Type | Required | Meaning |
|---|---|---:|---|
| `evidence_role` | `authoritative \| working_context` | yes | Whether the record establishes durable truth or provides active context. Existing records default to `authoritative`. |
| `session_surface` | `claude_code \| codex \| null` | yes | Originating agent surface for session evidence. `codex` covers Codex desktop and CLI sessions stored in the configured Codex session root. `null` for normal files. |
| `session_id` | `string \| null` | yes | Stable source session identifier. |
| `session_started_at` | ISO-8601 or `null` | yes | Earliest included turn timestamp. |
| `session_ended_at` | ISO-8601 or `null` | yes | Latest included turn timestamp. |
| `attention_observed_at` | ISO-8601 or `null` | yes | Timestamp used for temporal attention decay. |

Existing fields remain authoritative:

- `id`
- `title`
- `text`
- `source_path`
- `source_kind`
- `project`
- `source_modified_at`
- `checksum`
- chunk metadata
- `authority`
- `pinned`

New `source_kind` values:

- `claude_code_session`
- `codex_session`

Session records use a logical source locator rather than exposing a home-directory path:

```text
session://claude_code/<session_id>
session://codex/<session_id>
```

The raw local file path may appear in local build diagnostics, but it must not be published in returned evidence, workflow artifacts, or API responses.

### 6.1 Default authority

Initial authority values:

| Source | Authority |
|---|---:|
| Durable authoritative file | Existing source-kind policy |
| Claude Code session | `0.70` |
| Codex session | `0.70` |

These values recognize that session text is direct evidence of recent work but may contain hypotheses, errors, abandoned directions, and uncommitted intentions.

## 7. Session collection

### 7.1 New source-first component

Add `ingestion/source_first/session_scanner.py` and call it from the existing source-first rebuild.

The scanner reads:

- `~/.claude/projects/**/*.jsonl`
- `~/.codex/sessions/**/*.jsonl` (Codex desktop and CLI sessions present in that store)

It should reuse or extract deterministic parsing and redaction primitives from the existing session tooling where correct, but it must not invoke the legacy knowledge ingester, legacy storage model, distillation pipeline, or `.pks/agent-context` export as an intermediate system.

### 7.2 Included content

Include only human-readable conversational content from:

- user turns
- assistant turns

Exclude:

- system instructions
- developer instructions
- tool calls
- tool outputs
- hidden reasoning
- environment dumps
- attachment binary content
- generated status chatter that has no conversational content

The existing exporter behavior that can map a developer message into a user role must not be used in this ingestion path. Developer content is excluded, not relabeled.

### 7.3 Repository and project association

Associate each session with a project using the session’s recorded working directory.

Resolution order:

1. Exact match to a configured source-first project root.
2. Longest containing configured root.
3. Existing deterministic repository-name mapping.
4. If none resolves, exclude the session and report `unmapped_session_count`.

No model-based project classification is allowed.

### 7.4 Chunking

Session evidence is chunked as contiguous conversation windows, preserving:

- turn order
- role labels
- per-turn timestamps where available
- session ID
- project
- surface

Initial limits:

- `max_turn_chars`: 1,200
- `max_session_chars`: 24,000
- use the existing source-first maximum chunk size for the final evidence chunks
- prefer the most recent turns when a session exceeds its cap

Chunk titles use a deterministic format:

```text
<project> — <surface> session — <YYYY-MM-DD HH:MM>
```

### 7.5 Deterministic identity

Session evidence IDs must be stable across rebuilds when the included content has not changed.

The ID input is:

```text
surface + session_id + chunk_index + normalized_redacted_text_checksum
```

The checksum and embedding are calculated only after redaction and normalization.

### 7.6 Retention and bounds

V1 includes sessions whose `attention_observed_at` is within 30 days of the build time.

Initial safety bounds:

- maximum 100 sessions per surface
- maximum 250 session evidence chunks across both surfaces
- select newest eligible sessions first after project mapping

Older sessions do not remain in the next immutable generation. Source logs are not deleted.

### 7.7 Empty and unavailable sources

- A configured source root that exists and contains zero eligible recent sessions is valid.
- If session integration is enabled and a required source root is missing or unreadable, candidate promotion fails.
- Malformed individual session files are skipped, counted, and named only in local diagnostics.
- If malformed files exceed 5% of discovered eligible files for either surface, candidate promotion fails.
- Any redaction engine failure fails the build immediately.

## 8. Redaction and security

Redaction happens before normalized text, checksums, embeddings, evidence JSON, or reports are created.

At minimum, redact:

- API keys and bearer tokens
- private keys
- credential-bearing URLs
- common secret assignment forms
- 1Password secret references and resolved values
- session authentication metadata
- environment variable values matching secret-name patterns

Security requirements:

1. Redaction is deterministic and covered by fixtures.
2. A redaction exception fails closed.
3. Workflow artifacts may contain manifests, counts, checksums, and diagnostics, but never session text.
4. Logs must not print raw turns or secret matches.
5. The published `source_path` is the logical `session://` locator.
6. A build-time secret canary test must show the canary absent from evidence artifacts, Redis payloads, Vectorize metadata, and API responses.

## 9. Integrated ranking policy

The current ranking path remains a single calculation over a single candidate set.

### 9.1 Existing base score

Preserve the current base score:

`semantic_score` below is the existing Worker `similarity_score`; this is a formula label, not a second semantic signal.

```text
base_score =
    0.70 * semantic_score
  + 0.15 * lexical_score
  + 0.10 * authority_score
  + 0.05 * source_recency_score
```

### 9.2 Working-context attention

For `working_context` evidence only:

```text
attention_decay = exp(-ln(2) * age_days / 3)
attention_score = semantic_score * attention_decay
working_context_bonus = 0.08 * attention_score
```

For authoritative evidence:

```text
attention_score = 0
working_context_bonus = 0
```

Final score:

```text
final_score = min(1, base_score + working_context_bonus)
```

Initial policy constants:

- attention half-life: 3 days
- attention weight: 0.08
- session retention: 30 days

This produces a modest lift for recent, semantically relevant context. It cannot rescue an unrelated session because freshness is multiplied by semantic relevance.

### 9.3 Ordering invariants

Existing exact-identifier and explicit-project ordering rules remain ahead of the general score comparison.

The implementation must preserve these invariants:

1. An exact identifier match is not displaced by an unrelated recent session.
2. An explicit project constraint is not overridden by recency from another project.
3. A session result never changes another record’s authority.
4. Session evidence never updates project status or `last_touched` output.
5. Ties remain deterministic using the existing tie-break contract.

### 9.4 Search response fields

Every result returns:

- `evidence_role`
- `session_surface`
- `session_id`
- `attention_observed_at`
- `base_score`
- `attention_score`
- `working_context_bonus`
- `final_score`

This makes the policy visible and debuggable rather than hiding recency inside an opaque rank.

## 10. Atomic generation and publication

Session evidence is built and published inside the same source-first candidate generation as file evidence.

There is no session-only live update.

The generation manifest adds:

```json
{
  "recent_sessions": {
    "enabled": true,
    "claude_code_sessions": 0,
    "claude_code_chunks": 0,
    "claude_code_newest_observed_at": null,
    "codex_sessions": 0,
    "codex_chunks": 0,
    "codex_newest_observed_at": null,
    "unmapped_session_count": 0,
    "malformed_session_count": 0,
    "redacted_match_count": 0,
    "retention_days": 30,
    "attention_half_life_days": 3,
    "attention_weight": 0.08
  }
}
```

The current publisher combines candidate creation and live-pointer mutation. This implementation must split those operations:

1. `stage_candidate`: publish the complete immutable candidate without changing `sf:current_generation` or `sf:heartbeat`.
2. `verify_candidate`: verify counts, checksums, project maps, lexical maps, Vectorize records, session manifest fields, and retrieval behavior against the staged generation.
3. `promote_candidate`: update the heartbeat and live generation pointer only after all candidate gates pass.

The candidate retrieval gate must execute the same Worker scoring implementation used in production. Refactor the current search function so an internal function accepts an explicit generation, while the public search path continues to resolve only `sf:current_generation`. A local authenticated test harness may call the generation-specific function during CI; the public API must not accept an arbitrary generation override.

Publication requirements:

1. File and session evidence share the same generation ID.
2. Redis evidence, lexical maps, project maps, and Vectorize records all contain the same candidate generation.
3. The candidate is retrieval-tested and verified before the live generation pointer changes.
4. The pre-promotion regression gate runs against the staged candidate, not merely the previously live generation.
5. A failed session scan, evaluation, publish, or verification leaves the previous live generation untouched.
6. After successful promotion, retain the live generation and two prior verified generations. Garbage collection must never remove the current or immediate rollback generation.

## 11. Configuration

Add a `recent_sessions` block to `shared/source_first_config.json`:

```json
{
  "recent_sessions": {
    "enabled": true,
    "retention_days": 30,
    "attention_half_life_days": 3,
    "attention_weight": 0.08,
    "max_sessions_per_surface": 100,
    "max_total_chunks": 250,
    "max_turn_chars": 1200,
    "max_session_chars": 24000,
    "require_source_roots": true,
    "surfaces": [
      {
        "name": "claude_code",
        "path": "~/.claude/projects"
      },
      {
        "name": "codex",
        "path": "~/.codex/sessions"
      }
    ]
  }
}
```

The loader expands `~` locally. Published configuration and diagnostics must not expose the expanded home path.

## 12. Freshness and scheduling

### 12.1 Product SLA

A completed or updated eligible session must become searchable within two hours under normal operation.

### 12.2 Scheduler

Use the existing GitHub Actions source-first rebuild on its self-hosted macOS runner. Change its schedule to every two hours and retain manual dispatch.

Do not add a local LaunchAgent, cron job, queue consumer, or session-end hook.

The two-hour rebuild remains a complete atomic source-first rebuild. Existing embedding reuse prevents unchanged file evidence from being re-embedded. The generation-retention rule prevents unbounded remote storage growth.

### 12.3 Freshness health

`/health` adds:

- `recent_sessions.enabled`
- per-surface included session and chunk counts
- per-surface newest `attention_observed_at`
- age of newest included session
- last successful session scan time
- session freshness status: `fresh`, `empty`, `stale`, or `error`

`stale` means the last successful source-first generation is older than four hours while recent-session ingestion is enabled.

## 13. Evaluation contract

Add a `recent_session_priority` axis to the source-first retrieval regression suite. All existing axes and probes remain mandatory.

### 13.1 Required fixtures

1. A recent relevant session outranks an equally relevant older contextual record.
2. An unrelated recent session does not outrank a relevant authoritative file.
3. The attention contribution halves at 3 days and quarters at 6 days.
4. Session evidence older than 30 days is absent from the candidate generation.
5. Exact identifier ordering still wins.
6. Explicit project scope still wins across projects.
7. Claude Code and Codex provenance fields are correct.
8. System, developer, tool-call, and tool-output text is excluded.
9. A secret canary is redacted before checksum and embedding.
10. Rebuilding unchanged session input produces identical IDs and checksums.
11. A missing required session root blocks promotion.
12. A valid empty source root produces a successful zero-session manifest.
13. A malformed-file rate above 5% blocks promotion.
14. Session evidence does not change project status or `last_touched`.
15. The live API exposes the score breakdown and logical source locator.

### 13.2 Acceptance thresholds

- All existing retrieval regression probes pass with no axis regression.
- `recent_session_priority` pass rate is 100% on deterministic fixtures.
- In a labeled live set of at least 20 current-work queries, the relevant recent session appears in the top 3 for at least 90% of queries where a relevant session exists.
- Irrelevant recent-session intrusion is 0 across negative and cross-project probes.
- Secret-canary leakage is 0 across local artifacts, Redis, Vectorize metadata, and API responses.
- Search error rate remains 0 in the live evaluation run.
- Search p95 latency increases by no more than 15% from the pre-change baseline.
- Response token p95 increases by no more than 15% from the pre-change baseline.
- The live generation, Redis evidence, lexical maps, and Vectorize namespace report the same generation ID.

## 14. Observability

The build report must show:

- discovered session files per surface
- eligible sessions per surface
- included chunks per surface
- newest and oldest included timestamps
- excluded old sessions
- unmapped sessions
- malformed sessions and malformed rate
- redaction match count, never matched text
- reused versus newly generated embeddings
- generation ID and verification result

Search diagnostics must permit reconstruction of ranking from returned component scores.

No normal log may contain raw session text.

## 15. Rollout plan

### Phase A — Schema and parser dry run

- Add the unified evidence fields and deterministic session scanner.
- Run against real local Claude Code and Codex logs.
- Produce only counts, checksums, timestamps, and redaction diagnostics.
- Do not publish a candidate.
- Verify stable IDs across two unchanged runs.

Exit criteria: parsing, mapping, redaction, bounds, and determinism tests all pass.

### Phase B — Integrated shadow generation

- Publish session evidence in the same candidate generation with `attention_weight = 0`.
- Verify Redis, Vectorize, lexical maps, manifests, and API provenance.
- Confirm all existing retrieval probes remain unchanged.

Exit criteria: the unified generation is correct before ranking behavior changes.

### Phase C — Staging attention policy

- Set `attention_weight = 0.08` in staging.
- Run deterministic fixtures and the labeled current-work query set.
- Inspect false-positive recent-session intrusions and score breakdowns.

Exit criteria: all thresholds in Section 13.2 pass.

### Phase D — Production promotion

- Promote one verified production generation atomically.
- Change the existing workflow schedule to every two hours.
- Run live source-first evaluation against the production Worker.
- Verify a controlled new session created after the previous generation is retrievable within the two-hour SLA.
- Verify a durable-source fact query still ranks authoritative evidence correctly.

Exit criteria: the exact user-visible behavior has been exercised against the live Worker.

## 16. Rollback and failure behavior

Rollback is generation-based, not a partial data edit.

If live behavior regresses:

1. Repoint the live generation to the most recent verified pre-change generation.
2. If session evidence is structurally sound but ranking is wrong, build and promote a verified generation with `attention_weight = 0`.
3. Preserve candidate artifacts and score diagnostics for analysis.

The system must never attempt to delete session records selectively from the live generation or patch production ranks in place.

## 17. Implementation map

Expected implementation areas:

| Area | Change |
|---|---|
| `ingestion/source_first/models.py` | Add unified evidence-role and session provenance fields. |
| `ingestion/source_first/session_scanner.py` | New deterministic Claude Code and Codex scanner. |
| `ingestion/source_first/scanner.py` | Compose file and session evidence without treating sessions as project-state files. |
| Existing session parser/redactor modules | Extract reusable deterministic parsing and redaction primitives where safe. |
| `scripts/source_first_rebuild.py` | Load session config, scan sessions, report metrics, and include them in the candidate. |
| `ingestion/source_first/publisher.py` | Publish the expanded evidence schema and enforce generation retention. |
| Worker source-first search module | Apply the integrated attention formula, expose an internal generation-specific search function for candidate evaluation, and return score components. |
| Worker environment/types | Add fields required by the expanded evidence and health contract. |
| `shared/source_first_config.json` | Add versioned recent-session policy. |
| `.github/workflows/source-first-rebuild.yml` | Run every two hours and enforce stage, candidate evaluation, verification, then promotion on the existing self-hosted runner. |
| Retrieval regression fixtures | Add the `recent_session_priority` axis and negative probes. |
| Source-first documentation | Document authority versus attention and operational rollback. |

## 18. Definition of done

This work is done only when all of the following are true:

- Claude Code and Codex session chunks are present in the same live generation as file evidence.
- The system uses one candidate set and one scoring path; no second search or post-retrieval merge exists.
- Returned session evidence is visibly marked `working_context` with complete provenance.
- Relevant recent sessions receive the specified relevance-gated attention lift.
- Unrelated recent sessions receive no material advantage.
- Authoritative source behavior, project scoping, and exact identifier ordering do not regress.
- Session text cannot change project status or `last_touched`.
- The secret-canary test passes end to end.
- The source-first regression suite, Worker tests, Python tests, and live evaluation all pass.
- Redis, Vectorize, lexical maps, and the Worker report one generation ID.
- A newly created controlled session is returned by the live Worker within two hours.
- A controlled authoritative fact query still returns authoritative evidence correctly.
- The previous verified generation remains available for rollback.
- The implementation documentation and runbook match the deployed behavior.

## 19. Frozen v1 decisions

To keep the implementation coherent and small, v1 fixes the following decisions:

- Surfaces: Claude Code and Codex only.
- Evidence role: `working_context`, never canonical truth.
- Ingestion: deterministic raw conversational windows, no LLM summary.
- Architecture: same evidence model, generation, indexes, and search.
- Retention: 30 days.
- Attention half-life: 3 days.
- Maximum attention weight: 0.08.
- Freshness target: two hours.
- Scheduling: existing remote self-hosted source-first workflow, not a new local job.
- Promotion: atomic generation only.
- Rollback: previous verified generation or verified zero-attention generation.

These constants may be changed later only through configuration plus the full regression and live acceptance contract. The architecture must not be forked to accommodate tuning.
