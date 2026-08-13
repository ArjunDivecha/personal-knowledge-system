# Eval probe suite (PRD-eval-baseline-v1)

One JSON file per axis. Runner: `scripts/run_eval.py`. Reports land in
`scripts/reports/eval_baseline_<UTC>.json`.

## Probe schema (superset of the legacy `tests/fixtures/recall_probes.json` schema)

| field | req | meaning |
|---|---|---|
| `id` | yes | unique probe id |
| `axis` | yes | one of the axes below |
| `enabled` | yes | disabled probes are loaded but not scored (drafts) |
| `priority` | no | `high` / `medium` / `low` (reporting only) |
| `query` | yes | the search query sent to the MCP `search` tool |
| `expect_entry_ids` | no | **DEAD since the 2026-08-09 source-first cutover — do not add new ones.** Source-first serving returns `ev_` evidence chunks, so a `ke_` id can never appear in top-k and the field silently contributes nothing. Every probe that carried one was migrated 2026-08-13; see their `notes` for the ids that were removed. |
| `expect_any_of` | no | pass if ANY of these strings appears (case-insensitive) in a top-k result's label/summary |
| `forbid_any_of` | no | FAIL (stale leak) if ANY of these strings appears un-flagged in top-k |
| `min_rank` | no | k for this probe (default: runner `--k`, 5) |
| `paraphrase_group` | no | probes sharing a group must retrieve a common top-k entry id |
| `notes` | no | provenance / OPERATOR notes |

A probe passes if `expect_entry_ids` OR `expect_any_of` matches (either is
sufficient), AND no `forbid_any_of` string leaks. Negative-axis probes pass when
the top result's `final_score` is below the runner's `--negative-threshold`.

## Axes

| file | axis | metric |
|---|---|---|
| `recall.json` | carry_forward_recall (legacy set, migrated verbatim) | recall_at_k |
| `project.json` | project_recall | recall_at_k |
| `explicit_save.json` | explicit_save_recall | recall_at_k |
| `exact_lexical.json` | exact_lexical | lexical_gap (recall_at_k on lexical queries) |
| `stale_fact.json` | stale_fact | stale_leak_rate |
| `supersession.json` | supersession | supersession_accuracy |
| `negative.json` | negative | negative_precision |
| `paraphrase.json` | paraphrase | paraphrase_consistency |

## Conventions

- Generated/draft probes ship `enabled:false` with an `OPERATOR:` note; a human
  flips them after verifying the expected fact (same discipline as the legacy
  fixtures).
- Every real-world retrieval miss becomes a probe (see PRD §4 seeding method 3).
- Probes contain real personal facts; the repo is private (decision 2026-07-06).
