# PKS Phase 7A Builder Plan - Offline Observation And Claim Schema

- Status: Builder-ready for GPT-5.5 LOW after Opus blocker pass
- Date: 2026-06-05
- Scope: Offline schema contract and migration preview only
- Repo root: `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system`
- Python target: `distillation/venv/bin/python` is Python 3.14.3 in this workspace; use `from __future__ import annotations` and Python 3.10+ type syntax.
- Prerequisites:
  - `docs/pks-memory-research-refresh-design-memo-2026-06-05.md`
  - `docs/pks-memory-research-refresh-opus-review-2026-06-05.md`
  - `docs/pks-phase-7a-contradiction-supersession-taxonomy-2026-06-05.md`
  - `docs/pks-phase-7a-compile-latency-policy-2026-06-05.md`

## 0. One-Sentence Objective

Add an offline Phase 7A schema and migration preview that separates append-only observations from compiled semantic claims, with supersession/provisional metadata, without changing live Redis, Vector, Dream apply, or MCP retrieval behavior.

## 1. Why This Exists

PKS currently stores too much belief state directly in knowledge/project entries. When a belief changes, old and new facts compete inside the same entry. Phase 7A creates the offline contract for:

- what happened: observation
- what is currently believed: compiled claim
- why the system believes it: support observation IDs
- how beliefs changed: supersession edges
- whether uncompiled high-authority evidence can be temporarily visible: provisional claim metadata

This is a schema foundation, not a production migration.

## 2. Hard Non-Goals

Do not change:

- `cloudflare-mcp/mcp-server/src/dream.ts`
- `cloudflare-mcp/mcp-server/src/index.ts`
- Redis key format
- Vector metadata
- live retrieval behavior
- Dream apply behavior
- ingestion write behavior
- AGENTS.md or procedural memory behavior
- memory block generation

Do not run live write tools.

Do not add a graph backend.

Do not implement Phase 7B temporal/entity extraction.

Do not make provisional claims visible in the live MCP search route.

Do not implement automatic contradiction classification. Phase 7A must only
represent taxonomy outcomes and run pure duplicate grouping. Dream classification
logic belongs in Phase 7C.

## 3. Files To Add

Required new files:

- `distillation/models/phase7.py`
- `tests/python/test_phase7_schema.py`
- `tests/fixtures/phase7_migration_fixture.json`

Existing support files to import, not add:

- `distillation/utils/signal_flags.py`
  - import `EXPLICIT_SAVE_FLAG` and `CORRECTION_DERIVED_FLAG`
- `distillation/utils/__init__.py`
  - already exists; do not create another package marker

Optional new file if helper logic gets too large:

- `distillation/pipeline/phase7_preview.py`

Do not add production scripts yet unless the tests clearly need a CLI wrapper.

## 4. Files To Edit

Required edits:

- `distillation/models/__init__.py`
  - export Phase 7A dataclasses and helpers
- `docs/pks-memory-upgrade-checklist.md`
  - mark Phase 6.75 complete
  - add Phase 7A checklist

Optional edits:

- `tests/fixtures/README.md`
  - document the Phase 7 fixture

## 5. Schema To Implement

Import the legacy entry contracts from the existing model module:

```python
from .entries import KnowledgeEntry, ProjectEntry
```

Tests in `tests/python` usually put `distillation/` on `sys.path` and import
through `from models import ...`; production code inside `distillation/models`
should use the relative import above.

### 5.1 Constants

```python
PHASE7_SCHEMA_VERSION = 1
```

Allowed values:

```python
MemoryLane = Literal["semantic", "episodic", "procedural"]
SourceAuthority = Literal["explicit", "manual", "system", "inferred"]
ClaimStatus = Literal[
    "current",
    "historical",
    "superseded",
    "contested",
    "stale",
    "deprecated",
    "pending_compile",
    "expired",
]
TemporalStatus = Literal[
    "unknown",
    "timeless",
    "current",
    "future",
    "expired",
    "historical",
]
TaxonomyDecision = Literal[
    "duplicate",
    "refinement",
    "supersession",
    "scoped_exception",
    "contestation",
    "temporal_expiry",
    "deprecation",
]

AUTHORITY_RANK = {
    "inferred": 0,
    "system": 1,
    "manual": 2,
    "explicit": 3,
}
```

Add one helper for precedence:

```python
def highest_source_authority(authorities: list[SourceAuthority]) -> SourceAuthority:
    ...
```

All helper functions must use `AUTHORITY_RANK`; do not duplicate authority
ordering inline. `highest_source_authority([])` must raise `ValueError`.

### 5.2 Observation

Dataclass name:

```python
Phase7Observation
```

Fields:

```python
observation_id: str
subject_id: str
memory_lane: MemoryLane
source_authority: SourceAuthority
claim_text: str
source_type: str
source_id: str
message_ids: list[str]
source_path: str | None
snippet: str
observed_at: str | None
learned_at: str | None
valid_from: str | None = None
valid_to: str | None = None
invalidated_at: str | None = None
confidence: str = "medium"
entity_mentions: list[str] = field(default_factory=list)
relationship_edges: list[dict[str, Any]] = field(default_factory=list)
scope: dict[str, str] = field(default_factory=dict)
signal_flags: list[str] = field(default_factory=list)
extraction_method: str = "migration_preview"
schema_version: int = PHASE7_SCHEMA_VERSION
```

Methods:

- `to_dict()`
- `from_dict()`

Validation:

- `observation_id`, `subject_id`, `memory_lane`, `source_authority`, `claim_text`, `source_type`, and `source_id` are required and must be non-empty strings.
- `snippet` is required but may be an empty string.
- `message_ids` may be an empty list.
- `memory_lane == "procedural"` must be accepted as an observation but must not be compiled into semantic claims by helper functions.

### 5.2.1 Deserialization And Validation Contract

Implement `validate()` on every Phase 7A dataclass and call it from
`__post_init__`.

`from_dict()` behavior:

- ignore unknown keys
- fill optional/default fields from dataclass defaults
- raise `ValueError` on missing required fields
- raise `ValueError` on invalid allowed-value fields
- return an instance whose `validate()` has already run

`to_dict()` behavior:

- include all schema fields
- emit plain JSON-compatible values only
- preserve empty lists/dicts rather than omitting them

### 5.3 Compiled Claim

Dataclass name:

```python
Phase7CompiledClaim
```

Fields:

```python
claim_id: str
subject_id: str
memory_lane: Literal["semantic"]
compiled_text: str
status: ClaimStatus
support_observation_ids: list[str]
primary_source_authority: SourceAuthority = "inferred"
confidence: str = "medium"
temporal_status: TemporalStatus = "unknown"
valid_from: str | None = None
valid_to: str | None = None
ttl_expires_at: str | None = None
invalidated_at: str | None = None
supersedes_claim_ids: list[str] = field(default_factory=list)
superseded_by_claim_id: str | None = None
taxonomy_decision: TaxonomyDecision | None = None
scope: dict[str, str] = field(default_factory=dict)
compile_notes: list[str] = field(default_factory=list)
compiled_at: str | None = None
compiled_by: str = "migration_preview"
expected_source_revisions: dict[str, int] = field(default_factory=dict)
schema_version: int = PHASE7_SCHEMA_VERSION
```

Methods:

- `to_dict()`
- `from_dict()`

Validation:

- Compiled claims must be semantic only.
- `claim_id`, `subject_id`, `compiled_text`, and `status` are required and must be non-empty strings.
- `taxonomy_decision` accepts any `TaxonomyDecision` value on compiled claims.
- `support_observation_ids` cannot be empty unless status is `deprecated` and `compile_notes` explains why.
- `ttl_expires_at` is for provisional projection TTL only.
- `valid_to` is fact-validity only and must not be used as a provisional TTL.

### 5.4 Supersession Edge

Dataclass name:

```python
Phase7SupersessionEdge
```

Fields:

```python
from_claim_id: str
to_claim_id: str
decision: TaxonomyDecision
reason: str
observation_id: str
observed_at: str | None
schema_version: int = PHASE7_SCHEMA_VERSION
```

Methods:

- `to_dict()`
- `from_dict()`

Validation:

- `decision` must be one of `refinement`, `supersession`, `temporal_expiry`, or `deprecation`.
- `from_claim_id` and `to_claim_id` cannot be equal.
- `from_claim_id`, `to_claim_id`, `reason`, and `observation_id` are required and must be non-empty strings.

### 5.5 Provisional Claim

Do not create a separate dataclass unless necessary. Represent provisional state as a compiled claim with:

```python
status="pending_compile"
compiled_by="provisional_projection"
ttl_expires_at="<ttl timestamp>"
taxonomy_decision=None
```

Helper behavior must enforce:

- only `explicit`, `manual`, and limited `system` authority can create provisional claims
- `inferred` cannot create provisional claims
- procedural observations cannot create provisional claims

## 6. Legacy Source Contract

The implementation must support both dataclass instances and dictionaries created
by the existing `.to_dict()` methods in `distillation/models/entries.py`.

### 6.1 Knowledge Entry Contract

Dataclass: `KnowledgeEntry`.

Dictionary discriminator: `type == "knowledge"`.

Fields used by Phase 7A:

- `id`: legacy source ID and default `subject_id`
- `domain`: legacy topic label; do not add a Phase 7A `subject_label` field
- `state`, `detail_level`, `confidence`
- `current_view`: semantic observation when non-empty
- `positions[]`: each has `view`, `confidence`, `as_of`, `evidence`
- `key_insights[]`: each has `insight`, `evidence`
- `knows_how_to[]`: each has `capability`, `evidence`
- `open_questions[]`: each has `question`, optional `context`, optional `evidence`
- `metadata`: may contain `created_at`, `updated_at`, `source_conversations`, `source_messages`, `signal_flags`, `context_type`, `first_seen`, `last_seen`, `archived`

Evidence shape:

```python
{
    "conversation_id": str,
    "message_ids": list[str],
    "snippet": str,
}
```

Source-authority mapping for legacy knowledge entries:

- If `metadata.signal_flags` contains `EXPLICIT_SAVE_FLAG`, use `source_authority="explicit"`.
- If `metadata.signal_flags` contains `CORRECTION_DERIVED_FLAG` but not `EXPLICIT_SAVE_FLAG`, keep `source_authority="inferred"` and preserve the flag.
- Otherwise use `source_authority="inferred"`.

Canonical signal flags are defined in `distillation/utils/signal_flags.py`:

```python
EXPLICIT_SAVE_FLAG = "explicit_save"
CORRECTION_DERIVED_FLAG = "correction_derived"
```

### 6.2 Project Entry Contract

Dataclass: `ProjectEntry`.

Dictionary discriminator: `type == "project"`.

Fields used by Phase 7A:

- `id`: legacy source ID and default `subject_id`
- `name`: legacy project label; do not add a Phase 7A `subject_label` field
- `status`, `goal`, `current_phase`, `blocked_on`
- `decisions_made[]`: each has `decision`, optional `rationale`, `date`, optional `evidence`
- `metadata`: may contain `created_at`, `updated_at`, `source_conversations`, `source_messages`, `last_touched`, `context_type`, `first_seen`, `last_seen`, `archived`

Legacy project metadata has no `signal_flags` field today. Use
`source_authority="inferred"` unless the future caller explicitly passes manual
or system authority through a Phase 7-specific wrapper. Do not invent manual or
system authority from project status alone.

## 7. Helper Functions

Add pure helpers in `distillation/models/phase7.py`.

### 7.0 Observation And Claim Field Population Rules

Legacy-derived observations:

- `subject_id`: parent legacy entry `id`
- `source_type`: parent legacy entry `type` (`"knowledge"` or `"project"`)
- `source_id`: evidence `conversation_id` when item evidence exists and is non-empty, otherwise parent legacy entry `id`
- Treat item evidence as source-ID-bearing only when `conversation_id` is truthy.
- Scalar fields without item-level evidence (`current_view`, `goal`, `current_phase`, `status`, `blocked_on`) always use the parent legacy entry `id` as `source_id`, with `message_ids=[]` and `snippet=""`.
- `source_path`: field path for the source text, such as `current_view`, `positions[0]`, `key_insights[0]`, `knows_how_to[0]`, `open_questions[0]`, `goal`, `current_phase`, `status`, `blocked_on`, or `decisions_made[0]`
- For list-derived observations, use the actual list index (`positions[1]`, `key_insights[2]`, etc.). The index must make `source_path` unique within the parent entry so observation IDs do not collide.
- `message_ids`: evidence `message_ids` when present, otherwise `[]`
- `snippet`: evidence `snippet` when present, otherwise `""`
- `observed_at`: per-item timestamp when available (`positions[].as_of`, `decisions_made[].date`), otherwise metadata `created_at` when present, otherwise metadata `last_touched` when present, otherwise `None`
- `learned_at`: metadata `updated_at` when present, otherwise `observed_at`
- `valid_from`, `valid_to`, `invalidated_at`: leave `None` in Phase 7A unless the legacy source already has an exact matching field
- `confidence`: leave the Phase 7A default `"medium"`; do not propagate legacy confidence in 7A.

Migration preview must add a warning to `Phase7MigrationPreview.errors` when it
creates an observation with `message_ids=[]` or `snippet=""` because legacy
evidence was missing.

Compiled claims from duplicate grouping:

- grouping key is `(subject_id, normalize_claim_text(observation.claim_text))`
- preserve input order within each group
- `compiled_text` is the `claim_text` of the first observation in that group
- `claim_id` uses `stable_phase7_id("claim", subject_id, normalize_claim_text(compiled_text))`
- `support_observation_ids` are unique in first-seen order
- `expected_source_revisions` remains `{}`

Provisional claims:

- `compiled_text` is the raw `observation.claim_text`
- provisional claim IDs must be distinct from compiled current claim IDs for the same subject/text because they use the `"pending"` ID namespace

### 7.1 Stable IDs

```python
def stable_phase7_id(prefix: str, *parts: object) -> str:
    ...
```

Rules:

- SHA-256 hash
- 16 hex chars
- prefix followed by underscore, e.g. `obs_...`, `claim_...`
- canonicalize each part with `json.dumps(part, sort_keys=True, separators=(",", ":"), default=str)` before hashing
- do not include mutable aggregate fields such as `support_observation_ids` or `expected_source_revisions` in IDs

Object-specific ID inputs:

- Legacy-derived observation: `stable_phase7_id("obs", subject_id, source_type, source_id, source_path or "", claim_text, message_ids)`
- Compiled claim from duplicate grouping: `stable_phase7_id("claim", subject_id, normalize_claim_text(compiled_text))`
- Provisional claim: `stable_phase7_id("claim", subject_id, "pending", observation_id)`
- Hand-authored fixture claims may use explicit deterministic IDs, but tests should prefer the helper patterns above.

### 7.2 Claim Text Normalization

```python
def normalize_claim_text(text: str) -> str:
    ...
```

Exact rule for Phase 7A:

```python
return " ".join(text.casefold().strip().split())
```

Do not strip punctuation, stem words, remove stop words, or run semantic
similarity in Phase 7A. This intentionally makes duplicate grouping conservative
and deterministic.

### 7.3 Observation From Legacy Entry

```python
def observations_from_legacy_entry(entry: dict[str, Any] | KnowledgeEntry | ProjectEntry) -> list[Phase7Observation]:
    ...
```

Rules:

- Accept dicts and dataclasses.
- All observations derived from one legacy entry inherit the parent entry `id` as `subject_id`. Sub-claim identity is carried by `claim_text`, not by changing `subject_id`.
- For knowledge entries:
  - create exactly one semantic observation for non-empty `current_view`
  - create exactly one semantic observation per `positions[]` item with non-empty `view`
  - create exactly one semantic observation per `key_insights[]` item with non-empty `insight`
  - create exactly one semantic observation per `knows_how_to[]` item with non-empty `capability`
  - create exactly one episodic observation per `open_questions[]` item with non-empty `question`
- For project entries:
  - create exactly one semantic observation for non-empty `goal`
  - create exactly one semantic observation for non-empty `current_phase`
  - create exactly one semantic observation for non-empty `status`
  - create exactly one semantic observation for non-empty `blocked_on`
  - create exactly one semantic observation per `decisions_made[]` item with non-empty `decision`
- Preserve evidence snippets and message IDs where available.
- Use `source_authority="inferred"` for legacy extracted entries unless `signal_flags` contain explicit-save style flags.
- Use `memory_lane="semantic"` by default.
- Use `memory_lane="episodic"` only for open-question observations.
- Never fabricate user facts.
- For entries with missing evidence, still create the observation if the legacy entry has useful text, but add a short warning to the migration preview error list.

### 7.4 Initial Claims From Observations

```python
def compiled_claims_from_observations(observations: list[Phase7Observation], *, compiled_at: str | None = None) -> list[Phase7CompiledClaim]:
    ...
```

Rules:

- Compile semantic observations only.
- Ignore procedural observations.
- For Phase 7A, create one current claim per unique `(subject_id, normalized claim_text)` group.
- Merge duplicate support observation IDs.
- Do not classify supersession automatically in Phase 7A.
- Set `primary_source_authority` with `highest_source_authority()`.
- Leave `expected_source_revisions={}` in Phase 7A.

### 7.5 Provisional Projection

```python
def provisional_claim_from_observation(
    observation: Phase7Observation,
    *,
    now_utc: str,
) -> Phase7CompiledClaim | None:
    ...
```

Rules:

- explicit/manual TTL: 7 days
- system TTL: 48 hours, only when `observation.scope` is non-empty
- inferred: return `None`
- procedural: return `None`
- status: `pending_compile`
- compiled_by: `provisional_projection`
- set `ttl_expires_at`, not `valid_to`
- set `compiled_text=observation.claim_text`
- set `primary_source_authority` from the observation
- set `support_observation_ids=[observation.observation_id]`
- copy `observation.scope` to the provisional claim `scope`
- leave `expected_source_revisions={}` in Phase 7A.

### 7.6 Pure Retrieval Projection

```python
def retrieval_projection_from_claims(
    claims: list[Phase7CompiledClaim],
    *,
    now_utc: str,
) -> list[Phase7CompiledClaim]:
    ...
```

Rules:

- Return `status=="current"` claims.
- Return `status=="pending_compile"` claims only when `ttl_expires_at` exists and is later than `now_utc`.
- Never return expired provisional claims.
- Never return non-semantic claims.
- This is an offline test helper only; do not wire it into MCP retrieval in Phase 7A.

### 7.7 Migration Preview

```python
@dataclass
class Phase7MigrationPreview:
    observations: list[Phase7Observation]
    claims: list[Phase7CompiledClaim]
    supersession_edges: list[Phase7SupersessionEdge]
    skipped_count: int = 0
    errors: list[str] = field(default_factory=list)
```

Helper:

```python
def preview_phase7_migration(entries: list[dict[str, Any] | KnowledgeEntry | ProjectEntry]) -> Phase7MigrationPreview:
    ...
```

Rules:

- pure, offline, no network
- no storage writes
- keep moving if one entry fails
- expose `to_dict()`

## 8. Fixture Requirements

Create `tests/fixtures/phase7_migration_fixture.json`.

Include synthetic entries and schema cases only. Do not include live user facts.

Recommended top-level shape:

```json
{
  "legacy_entries": [],
  "observations": [],
  "compiled_claims": [],
  "supersession_edges": [],
  "malformed_entries": []
}
```

Required legacy entries:

1. Knowledge entry with one current view, one position, one key insight, one capability, and one open question. Expected observations: 5 total, 4 semantic and 1 episodic.
2. Project entry with active status and decision.
3. Explicit-save style entry with signal flag.
4. Procedural observation candidate to prove it is not compiled.
5. Duplicate semantic observations that collapse into one claim.
6. Multi-item list fixture with at least two positions sharing the same evidence conversation, to prove `source_path` indexes and observation IDs stay unique.

Required taxonomy representation cases:

1. Duplicate support merge.
2. Refinement preserving old evidence.
3. Supersession with fact `valid_to`.
4. Scoped exception keeping both claims current with different `scope`.
5. Contestation requiring review through `status="contested"`.
6. Temporal expiry with `status="expired"` and `temporal_status="expired"`.
7. Deprecation without replacement and with `compile_notes`.
8. Procedural-memory mutation rejected by helper behavior.

Coverage tests for hand-authored taxonomy fixture rows should read only the
needed fields (`taxonomy_decision`, `decision`, `compile_notes`, `status`) and
should not require full-object equality for rows that include fixture-only notes.
All seven taxonomy values must appear in `compiled_claims[].taxonomy_decision`.
Only the four edge-legal values (`refinement`, `supersession`,
`temporal_expiry`, `deprecation`) may appear in
`supersession_edges[].decision`.

Required provisional representation cases:

1. Explicit observation creates provisional claim.
2. Manual observation creates provisional claim.
3. System validation observation creates 48-hour scoped provisional claim.
4. Unscoped system observation remains evidence-only.
5. Inferred observation remains evidence-only.
6. Expired provisional claim is excluded by `retrieval_projection_from_claims`.
7. Conflicting provisional claim is representable as coexisting claims with distinct `status` values: one `current`, one `pending_compile`; do not add a conflict field in Phase 7A.
8. Promoted provisional outcome is representable as `status="current"`.
9. Expired reconciliation outcome is representable while preserving the observation.

The fixture should be small, synthetic, and deterministic. Coverage means schema
representation plus pure helper behavior in Phase 7A; automatic Dream
classification/reconciliation is still Phase 7C.

## 9. Unit Tests

Create `tests/python/test_phase7_schema.py`.

Test cases:

1. Dataclasses round-trip through `to_dict()` / `from_dict()` using fully populated instances with every optional field set to a non-default value, including at least one `None` optional.
2. Stable IDs are deterministic.
3. `normalize_claim_text("  The   QUICK, brown Fox.  ") == "the quick, brown fox."`
4. `stable_phase7_id` uses canonical JSON ordering for dict/list parts.
5. Legacy knowledge dataclass creates exactly 5 source-backed observations for the primary fixture entry: 4 semantic and 1 episodic.
6. Legacy knowledge dict creates the same observation count as the dataclass.
7. Legacy project dataclass creates project semantic observations with parent entry ID as `subject_id`.
8. Legacy project dict creates project semantic observations.
9. Every legacy-derived observation inherits the parent entry ID as `subject_id`.
10. `explicit_save` signal maps to `source_authority="explicit"`.
11. `correction_derived` flag is preserved but does not become explicit/manual/system authority.
12. Duplicate observations merge into one compiled claim with multiple support IDs.
13. Duplicate merge sets highest `primary_source_authority`.
14. `highest_source_authority([])` raises `ValueError`.
15. Compiled duplicate-group claim keeps `compiled_text` equal to the first observation's raw `claim_text` in input order.
16. Claim IDs from duplicate grouping use `("claim", subject_id, normalize_claim_text(compiled_text))`.
17. List-derived observations use actual source indexes and produce unique observation IDs.
18. Evidence-less legacy item creates an observation with `snippet==""`, `message_ids==[]`, `source_id` equal to the parent entry ID, and a migration-preview warning.
19. Phase 7A leaves legacy-derived observation confidence at `"medium"`.
20. Per-item timestamps map to `observed_at` and metadata `updated_at` maps to `learned_at`.
21. Scalar-field observations use parent entry ID as `source_id`, `message_ids=[]`, and `snippet=""`.
22. Procedural observations do not compile into semantic claims.
23. Explicit observation creates provisional pending-compile claim with 7-day `ttl_expires_at`.
24. Manual observation creates provisional pending-compile claim with 7-day `ttl_expires_at`.
25. Scoped system observation creates provisional pending-compile claim with 48-hour `ttl_expires_at`.
26. Provisional claim `compiled_text` equals raw `observation.claim_text`.
27. Unscoped system observation does not create provisional claim.
28. Inferred observation does not create provisional claim.
29. Expired provisional claim is excluded by `retrieval_projection_from_claims`.
30. Current compiled claim is included by `retrieval_projection_from_claims`.
31. Conflicting provisional state is represented by coexisting `current` and `pending_compile` claims, with no extra conflict field and distinct `claim_id` values.
32. Supersession edge rejects equal from/to claim IDs.
33. Supersession edge accepts only refinement, supersession, temporal expiry, and deprecation.
34. Compiled claim `taxonomy_decision` accepts all seven `TaxonomyDecision` values.
35. Fixture content covers every `TaxonomyDecision` value by parsing hand-authored `compiled_claims[].taxonomy_decision`.
36. Fixture content covers the four legal `Phase7SupersessionEdge.decision` values by parsing hand-authored `supersession_edges[].decision`.
37. Deprecated claim without support observations must include `compile_notes`.
38. Invalid enum values and missing required fields raise `ValueError`.
39. Unknown input keys are ignored by `from_dict`.
40. Migration preview returns observations and claims without errors on valid fixture entries.
41. Migration preview keeps moving and records errors for malformed input.

## 10. Validation Commands

Run:

```bash
distillation/venv/bin/python -m unittest tests.python.test_phase7_schema
make test-python-checker
python3 -m json.tool tests/fixtures/phase7_migration_fixture.json >/dev/null
git diff --check
```

If all pass, final status can say:

```text
ITS_DONE_TESTED <test_count>
```

Use the `make test-python-checker` test count for `<test_count>`.

## 11. Builder Stop Conditions

Stop and ask for review if:

- implementing this requires changing Worker TypeScript
- implementing this requires Redis/Vector writes
- inferred observations appear in provisional current retrieval
- procedural memory would be compiled into semantic claims
- contradiction/supersession classification is needed beyond duplicate grouping
- fixture requires real private facts rather than synthetic examples

## 12. Review Checklist

Before handing back:

- Phase 7A code is pure/offline.
- No live Dream or retrieval behavior changed.
- Every observation has source metadata.
- Every compiled claim has support observations.
- Provisional claims are TTL-bound and authority-gated.
- Procedural observations are excluded from semantic compilation.
- Tests cover the two Opus-gated policies.
- Fixtures cover every taxonomy decision and all four source-authority values.
