# PKS Phase 6.5 Builder Plan - Outcome Quality Baseline

- Status: Builder-ready for GPT-5.5 LOW
- Date: 2026-06-04
- Scope: Complete the read-only outcome-quality baseline before Phase 7
- Executor: Low-reasoning code builder
- Repo root: `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system`

## 0. One-sentence objective

Extend the existing memory-quality audit into an OpenAI-style outcome baseline that measures carry-forward recall, temporal freshness, and project lifecycle staleness without changing production memory entries, Dream apply logic, or the Phase 7 schema.

## 1. Current state to preserve

The repo already has these Phase 0 / quality pieces:

- `scripts/audit_memory_quality.py`
  - read-only guard wrappers for Redis and Vector
  - M1 tier distribution
  - M3 salience degeneracy
  - M4 duplicate cluster estimate
  - M5 growth from Dream ledger
  - M6 recall against `tests/fixtures/recall_probes.json`
  - M7 access-signal coverage
  - `evaluate_gate()`
  - optional `--write-gate`
- `tests/fixtures/recall_probes.json`
- `tests/python/test_audit_memory_quality.py`
- `shared/memory_policy.json`
  - `quality_gate`
  - `dedup`
  - `tier_percentiles`
  - `dream_thresholds`
- root `Makefile`
  - `audit-memory-quality`
  - `verify-memory-quality`
  - `test-python-checker`

Do not replace these. Extend them.

## 2. Non-goals

Do not implement Phase 7.

Do not create an evidence-log / compiled-view schema.

Do not modify `cloudflare-mcp/mcp-server/src/dream.ts` apply behavior.

Do not archive, merge, demote, promote, or otherwise mutate live memory entries.

Do not run `make verify-memory-quality` against production unless explicitly instructed; that writes a validation ledger entry.

Do not invent user facts for temporal probes. Synthetic facts belong only in unit tests.

Do not use the live MCP `search` tool inside `audit_memory_quality.py`; production `search` can reconsolidate entries and therefore is not read-only. Use direct Vector query plus Redis fetch, as the current script does.

## 3. Files to edit

Required edits:

- `scripts/audit_memory_quality.py`
- `tests/python/test_audit_memory_quality.py`
- `shared/memory_policy.json`
- `tests/fixtures/recall_probes.json`
- `tests/fixtures/README.md`

Required new file:

- `tests/fixtures/temporal_staleness_probes.json`

Optional doc updates, only after code/tests pass:

- `docs/pks-memory-upgrade-checklist.md`
- `docs/testing-matrix.md`

## 4. Desired output schema

Update `audit_memory_quality.py` report schema from version `1` to version `2`.

Keep all existing fields and add:

```json
{
  "m8_temporal_freshness": {
    "skipped": false,
    "probe_count": 0,
    "passed_count": 0,
    "freshness_at_5": null,
    "probes": []
  },
  "m9_project_lifecycle": {
    "active_project_count": 0,
    "stale_active_project_count": 0,
    "stale_active_project_share": 0.0,
    "stale_after_days": 90,
    "stale_projects": []
  }
}
```

The gate block must include the new thresholds:

```json
{
  "gate": {
    "name": "verify_memory_quality",
    "passed": false,
    "issues": [],
    "thresholds": {}
  }
}
```

## 5. Policy additions

Edit `shared/memory_policy.json`.

Add a top-level `project_lifecycle` block:

```json
"project_lifecycle": {
  "active_stale_after_days": 90,
  "active_recent_access_grace_days": 30
}
```

Extend `quality_gate`:

```json
"quality_gate": {
  "threshold_tier1": 0.40,
  "threshold_dup": 0.20,
  "threshold_recall": 0.60,
  "threshold_temporal_freshness": 0.80,
  "threshold_stale_active_projects": 0
}
```

If the existing numbers differ, preserve existing values and add only the missing keys.

## 6. Fixture changes

### 6.1 Recall probes

Keep `tests/fixtures/recall_probes.json` backward-compatible.

For each existing probe, add these optional fields:

```json
{
  "id": "recall_dip_factor",
  "axis": "carry_forward_recall",
  "enabled": true,
  "priority": "high"
}
```

Rules:

- Keep existing `query`, `expect_any_of`, and `notes`.
- Do not invent exact entry IDs.
- If a probe has `OPERATOR: verify` in notes, keep it enabled but leave the note intact.
- Make IDs stable, lowercase, and snake_case.

### 6.2 Temporal probes

Create `tests/fixtures/temporal_staleness_probes.json`.

Start with an empty array unless you can identify source-verified stale facts from existing docs or fixtures. Do not fabricate user-specific stale facts.

Schema:

```json
[
  {
    "id": "temporal_example_disabled",
    "axis": "temporal_freshness",
    "enabled": false,
    "query": "example stale plan query",
    "as_of": "2026-06-04T00:00:00+00:00",
    "stale_after": "2026-01-01T00:00:00+00:00",
    "expect_no_text_any_of": ["example stale phrase"],
    "expect_text_any_of": [],
    "notes": "Disabled schema example only. Do not enable without source verification."
  }
]
```

The audit must ignore disabled probes.

Unit tests may use synthetic temporal probes in Python code. They do not need to be in the production fixture file.

## 7. `audit_memory_quality.py` implementation steps

### 7.1 Constants and loaders

Add:

```python
TEMPORAL_PROBES_PATH = REPO_ROOT / "tests" / "fixtures" / "temporal_staleness_probes.json"
```

Add:

```python
def load_temporal_staleness_probes() -> list[dict[str, Any]]:
    ...
```

Behavior:

- Return `[]` when the file is missing.
- Load JSON array when present.
- Drop probes where `enabled is False`.
- Keep probes where `enabled` is missing or true.

### 7.2 Text extraction helper

Add a pure helper:

```python
def entry_text_for_probe(entry: Any) -> str:
    ...
```

It should concatenate lowercased text from:

- `id`
- `domain` or `name`
- `current_view`
- `goal`
- `current_phase`
- `blocked_on`
- `key_insights[].insight`
- `positions[].view`
- `evolution[].to_view`
- `evolution[].from_view`
- `decisions_made[].decision`

The helper must tolerate dataclasses and dictionaries.

### 7.3 Temporal probe pure evaluator

Add:

```python
def evaluate_temporal_probe_text(entry_texts: list[str], probe: dict[str, Any]) -> tuple[bool, list[str]]:
    ...
```

Rules:

- `expect_no_text_any_of`: fail if any phrase appears in any returned entry text.
- `expect_text_any_of`: if non-empty, pass only if at least one phrase appears in any returned entry text.
- Phrases compare case-insensitively.
- Return `(passed, issues)`.

### 7.4 M8 temporal freshness

Add:

```python
def compute_m8_temporal_freshness(
    vector: Any,
    by_id: dict[str, Any],
    *,
    recall_k: int,
    skip: bool,
) -> dict[str, Any]:
    ...
```

Implementation:

- If `skip`, return skipped shape.
- Load enabled temporal probes.
- If no enabled probes, return `freshness_at_5: None` and note `"no enabled temporal_staleness_probes.json probes"`.
- For each probe:
  - embed `probe["query"]` using the same `get_embedding()` import used by M6
  - Vector query top `recall_k`
  - Fetch returned entries from `by_id`
  - Build entry texts with `entry_text_for_probe`
  - Evaluate with `evaluate_temporal_probe_text`
- Report:
  - `probe_count`
  - `passed_count`
  - `freshness_at_5`
  - per-probe `returned_ids`, `hit`, `issues`, `expect_no_text_any_of`, `expect_text_any_of`

If embedding fails for a probe, mark that probe failed with an `error` field.

### 7.5 M9 project lifecycle

Add:

```python
def compute_m9_project_lifecycle(
    projects: list[Any],
    policy: dict[str, Any],
    *,
    now_iso: str | None = None,
) -> dict[str, Any]:
    ...
```

Definitions:

- Consider only non-archived projects with `status == "active"`.
- Project last activity timestamp is the latest parseable timestamp among:
  - `metadata.last_touched`
  - `metadata.last_seen`
  - `metadata.updated_at`
  - `metadata.last_accessed`
- A project is stale-active when:
  - `status == "active"`
  - days since last activity is greater than `project_lifecycle.active_stale_after_days`
  - and it has not been accessed within `project_lifecycle.active_recent_access_grace_days`
- Missing timestamps count as stale-active.

Report each stale project as:

```json
{
  "id": "pe_...",
  "name": "...",
  "status": "active",
  "last_activity": "...",
  "days_since_activity": 123,
  "reason": "active_project_older_than_90_days"
}
```

This metric is read-only. It must not change project status.

### 7.6 CLI args

Add:

```text
--skip-temporal
--now-utc
```

`--now-utc` is for deterministic tests and M9. It must accept ISO timestamps with timezone.

### 7.7 Run audit integration

Update `run_audit()`:

- Accept `skip_temporal` and `now_utc`.
- Keep existing M1-M7 behavior.
- Build `by_id` once for active entries and reuse it.
- Add M8 after M6.
- Add M9 after M8.
- Print compact M8/M9 summary lines.
- Include M8/M9 in the report.

### 7.8 Gate evaluation

Update `evaluate_gate()`.

Existing failures remain:

- M1 high Tier 1 share
- M4 duplicate share
- M6 low recall

Add:

- M8 low temporal freshness, only when `freshness_at_5 is not None`
- M9 stale active project count above `threshold_stale_active_projects`

Issue strings must include metric names:

- `M8 temporal_freshness_at_5 ...`
- `M9 stale_active_project_count ...`

If M8 has no enabled probes, do not fail the gate.

### 7.9 Validation-ledger details

When `--write-gate` is used, add these details:

```python
"temporal_freshness_at_5": report["m8_temporal_freshness"].get("freshness_at_5"),
"stale_active_project_count": report["m9_project_lifecycle"]["stale_active_project_count"],
"stale_active_project_share": report["m9_project_lifecycle"]["stale_active_project_share"],
```

## 8. Unit tests

Edit `tests/python/test_audit_memory_quality.py`.

Add tests for:

1. `load_temporal_staleness_probes()` ignores disabled probes.
2. `entry_text_for_probe()` extracts text from dict-shaped knowledge entries.
3. `entry_text_for_probe()` extracts text from dataclass-shaped project entries.
4. `evaluate_temporal_probe_text()` fails when stale phrase appears.
5. `evaluate_temporal_probe_text()` passes when stale phrase is absent.
6. `evaluate_temporal_probe_text()` honors `expect_text_any_of`.
7. `compute_m9_project_lifecycle()` flags an active project older than threshold.
8. `compute_m9_project_lifecycle()` does not flag a recently touched active project.
9. `compute_m9_project_lifecycle()` does not flag completed/paused/abandoned projects.
10. `evaluate_gate()` fails on low M8 when M8 is populated.
11. `evaluate_gate()` fails on stale-active project count above threshold.
12. `evaluate_gate()` does not fail when M8 has no enabled probes.

Use synthetic objects only. No network. No Redis. No Vector.

## 9. Validation commands

Run from repo root:

```bash
cd "/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system"
```

Validate JSON fixtures:

```bash
python3 -m json.tool tests/fixtures/recall_probes.json >/dev/null
python3 -m json.tool tests/fixtures/temporal_staleness_probes.json >/dev/null
```

Run Python tests:

```bash
make test-python-checker
```

Run a read-only local/live smoke only if credentials are available:

```bash
oprun -- make audit-memory-quality AUDIT_MEMORY_QUALITY_ARGS="--skip-dup --skip-recall --skip-temporal"
```

Do not run this unless the executor has credentials and understands it reads live Redis:

```bash
oprun -- make audit-memory-quality AUDIT_MEMORY_QUALITY_ARGS="--skip-dup --skip-recall"
```

Do not run this against production unless explicitly instructed because it writes the validation ledger:

```bash
oprun -- make verify-memory-quality
```

Optional TypeScript sanity if no Worker code was edited:

```bash
make worker-typecheck
```

## 10. Expected success output

The builder may report success only if:

- JSON fixtures validate.
- `make test-python-checker` passes.
- `audit_memory_quality.py` can generate a schema-version-2 report in read-only mode, at least with `--skip-dup --skip-recall --skip-temporal`.
- No production memory mutation commands were run.

Final status format for the builder:

```text
ITS_DONE_TESTED <passed_count>
```

Only use that if the validation commands actually ran. Otherwise use:

```text
NOT_DONE <failed_count>
```

## 11. Review checklist

Before handing back:

- `git diff --check` passes.
- `git status --short` shows only intended files.
- `audit_memory_quality.py` still contains the read-only guard.
- `--write-gate` remains the only validation-ledger write path.
- `temporal_staleness_probes.json` does not contain fabricated user facts.
- No Phase 7 schema migration was added.
- No Dream apply behavior was changed.

## 12. Follow-up phases after this builder packet

This packet stops at measurement.

After it lands, the next builder-ready packets should be written separately:

1. Phase 6.6 - source-aware mention counting and cross-source fusion metrics.
2. Phase 7 - evidence log plus compiled view data model and migration plan.
3. Phase 8 - pre/post Dream outcome eval harness.
4. Phase 9 - quality-gated Dream apply and rollback on outcome regression.
