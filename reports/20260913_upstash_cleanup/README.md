# Upstash cleanup — 2026-09-13

Follow-up to `reports/20260910_upstash_capacity_review/` (approved by Arjun 2026-09-13).

## Result

`knowledge-embeddings` went from **603,967 vectors (91.5% of the 660,000 quota) to 72,637 (11.0%)**.
Only the serving chain remains: `sf_20260913T155402Z` (live) + `sf_20260913T135356Z` + `sf_20260913T115349Z`.
The `knowledge-system` Redis went from 6,462,435 keys to 897,091 — each leaked generation had carried
~250K `sf:<gen>:*` keys as well. Serving verified healthy afterwards (`--verify-current`: passed,
24,257 vectors = 24,257 Redis records).

| Step | Removed |
|---|---:|
| 62 orphan `sf_*` namespaces (never promoted, or pre-cutover stragglers from 2026-08-09) | 513,082 vectors, ~5.5M Redis keys |
| Legacy `(default)` namespace (`ke_` index, read only by the retired `mcp-server/`) | 18,248 vectors |

## Fix shipped (this commit)

- `ingestion/source_first/publisher.py`: `discard_generation()` (refuses the live/rollback chain),
  `sweep_orphans()` (age-guarded, default 3h), shared one-pass `_collect_generation_keys()`
  (a SCAN per generation cost ~2 min each on this keyspace; Upstash caps SCAN at 1,000 keys/call,
  so a full pass is ~6,400 calls ≈ 9 min).
- `scripts/source_first_rebuild.py`: `--discard-generation`, `--sweep-orphans`, `--orphan-min-age-hours`.
- `.github/workflows/source-first-rebuild.yml`: discard step on `failure() || cancelled()`, sweep step
  `always()`; both `continue-on-error`. The sweep costs one `/list-namespaces` call when there is nothing
  to sweep.
- Tests: `tests/python/test_source_first.py` (+2, 20/20 pass).

## Still open

- The gate has rejected every candidate since 16:17Z today on a single probe, `para_asado_b`
  (paraphrase axis; expected evidence not in top-5). Same class as the 2026-08-21 incident. Not touched here.
- Index `indexSize` still reports ~6.6 GB; Upstash reclaims storage lazily.

## Files

- `delete_orphan_namespaces.py` — v1 (SCAN per generation; killed after 10 namespaces, see `orphan_deletion_v1_partial.log`)
- `delete_orphan_namespaces_v2.py` — single-pass version that did the remaining 52; `orphan_deletion_v2.log`,
  `orphan_deletion_v2_20260913T222828Z.json`, `orphan_deletion_20260913T221556Z_dryrun.json`
- `default_namespace_*` — before/after of the legacy namespace reset
