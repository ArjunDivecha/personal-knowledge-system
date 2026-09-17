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
Credentials are redacted before checksums, embeddings, artifacts, or remote writes.

claude.ai conversations (since 2026-09-04) come from the newest data export under
`Identity and Important Papers/Arjun Digital Identity/Anthropic/<YYYY-MM-DD>/`
(the `conversations-*.zip` parts, merged and de-duplicated by uuid; regenerated
branches resolved to the latest child). They are `claude_ai_chat` evidence with
authority 0.6 and the ordinary floor — not `working_context`, so no attention lift
and no 30-day cutoff; the whole archive is searchable and `get_deep` returns a
full conversation via `chat://claude_ai/<uuid>`. The export's memories (Claude's
own profile of Arjun, project memories, memory files) are pinned curated memory at
authority 1.0. ChatGPT conversations (OpenAI data export, since 2026-09-05) are handled
the same way from `.../Arjun Digital Identity/ChatGPT/<YYYY-MM-DD>/conversations-*.json`
as `chatgpt_chat` evidence (primary path from `current_node`; model reasoning and
conversations flagged *do not remember* are excluded). Dropping a newer dated export
folder under either root is all that is needed to refresh; the next rebuild picks it up. Session records use logical
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
0.04 * semantic_similarity * exp(-ln(2) * age_days / 3)
```

The lift describes attention, not authority. (Weight 0.08 → 0.04 and session
authority 0.7 → 0.6 on 2026-09-05: at 0.08 a burst of same-day sessions about a
topic outranked the document that answered the question.) It may reorder evidence that
already qualifies, but it can never be what admits evidence: the relevance
floor is applied to the base score, before the lift is added. Multiplying the
lift by semantic relevance bounds its size; applying the floor to the base
score is what actually prevents an unrelated recent session being rescued.
(Before 2026-08-21 the floor was applied after the lift, and an unrelated
session with base 0.6292 was admitted at 0.6759.)

Candidates recovered through the inverted index or an explicit project map are
not part of the vector top-K, so their similarity is unknown at that point.
Since 2026-09-04 the ranker fetches the stored vectors for up to 40 of the
best-placed recovered candidates and scores them with their real cosine
similarity on Upstash's `(1 + cos) / 2` scale (`similarity_source: "vector_fetch"`); before that they carried
similarity 0 and a fictitious `final_score` of roughly 0.2–0.3 while still
sorting ahead of real semantic hits. A candidate whose vector cannot be fetched
stays `"unscored"` rather than failing the search.
The vector query itself asks for at least the top 100 (was 50) since 2026-09-04: on a
6.7k-record corpus the true best chunk for a project-heavy query fell outside the top 50
behind near-duplicate documentation chunks.

Byte-identical results collapse by `content_checksum` while preserving
alternate provenance. General results below `0.65` are omitted; if none remain,
the response explicitly abstains rather than returning confident-looking noise.

### Jev answerability gate (since 2026-09-17)

`final_score` measures similarity, not answerability: on the BEAM benchmark the
production score could not separate unanswerable questions from answerable ones
at any threshold (Youden J 0.075–0.131; 0 of 40 unanswerable questions abstained).
TypeSafe's Jev — a decision-only model that returns a yes/no probability — asked
per passage *"does this passage state information usable in a direct answer to
the query?"* separates them at AUC 0.865 / Youden J 0.642
(`../beam-eval/runs/20260917T0220Z_jev_abstention/JEV_ABSTENTION.md`).

So after the floor and the slice, `src/jevGate.ts` sends the query plus exactly
the passages the caller would receive to `api.typesafe.ai` in one request
(~200 ms) and attaches `jev_evidence` / `jev_relevant` to every result plus a
`jev` report on the payload. With `JEV_ABSTENTION_MODE=on`, if no returned
passage's evidence probability reaches `JEV_ABSTENTION_THRESHOLD` (0.2 in
production) the search abstains with `abstain_reason: "jev_no_answer_evidence"`.
`shadow` annotates without abstaining; `off` (or no `TYPESAFE_API_KEY`) is
byte-identical to the pre-gate search.

Rules the gate obeys:

- it can only **add** an abstention — never admit, never reorder;
- deterministic recovery is exempt: an explicitly named project, a strong exact
  lexical phrase, or an opaque identifier (digits, code punctuation, acronym,
  camelCase — *not* fragments of a hyphenated phrase like "Brazil-only") keeps
  its results regardless of the Jev score;
- it **fails open**: any error, timeout (2.5 s) or malformed reply leaves the
  ranked results untouched and reports `jev.status: "unavailable"`;
- the probe evaluator runs the same gate from the same env, so the promotion
  suite fails a candidate the gate would break.

Threshold 0.2 rather than BEAM's fitted 0.5: on the production probes the
lowest-scoring unprotected positive (`para_bbg_b`) sits at 0.29–0.34 and Jev's
score for the same input drifts by up to 0.08 between calls, so 0.5 and 0.3
both abstained on it. At 0.2 BEAM still gives 40% correct abstention at 6.4%
false abstention; raising it is an eval-gated change. Privacy: query and
passage text leave the machine on every search that clears the floor — a
decision Arjun made on 2026-09-16.

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

The GitHub Actions workflow `Source-First Memory Rebuild` is the only job that
builds, gates, and promotes a generation. Its cron is every two hours, but GitHub
cron is best-effort: between 2026-08-24 and 2026-09-04 it fired about 5.5 times a
day (median gap 4.2h, max 12.6h). A local LaunchAgent,
`launchd/com.arjundivecha.pks-rebuild-kicker.plist`, runs
`scripts/kick_source_first_rebuild.sh` every 30 minutes: if no run has started in
2h and none is queued, it dispatches the workflow; it also reads `/health` and
writes `ingestion/checkpoints/source_first_kicker.json` (ok=false when the
serving generation is older than 6h) so Overseer can alarm. The Worker's own
freshness threshold is `SOURCE_FIRST_MAX_AGE_SECONDS=21600` (6h, was 36h).

Recent sessions cover the full 30-day retention window (`max_total_chunks` 6000,
`max_sessions_per_surface` 2000; before 2026-09-04 the 250-chunk cap kept ~4% of
mapped sessions, about nine days). Sessions whose folder under a source root has
no authoritative file are named after the top-level folder instead of being
dropped (`map_unlisted_folders`); that synthetic project is not published to the
catalog. The manifest buckets remaining unmapped sessions by cause; in practice
~97% are headless `/tmp` and `/` runs.
