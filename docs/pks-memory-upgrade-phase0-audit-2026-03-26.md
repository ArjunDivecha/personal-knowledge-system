# Phase 0 Audit: PKS Memory Upgrade

Date: 2026-03-26
Branch: `Dream`
Repo: [knowledge-system](/Users/arjundivecha/Dropbox/AAA%20Backup/A%20Working/Memory/knowledge-system)
Source PRD: [PKS-Upgrade-PRD.md](/Users/arjundivecha/Dropbox/AAA%20Backup/A%20Working/Memory/PKS-Upgrade-PRD.md)

## Summary

Phase 0 confirms that the PRD should be implemented against the production Worker in [cloudflare-mcp/mcp-server/src/index.ts](/Users/arjundivecha/Dropbox/AAA%20Backup/A%20Working/Memory/knowledge-system/cloudflare-mcp/mcp-server/src/index.ts), not the legacy [mcp-server/](/Users/arjundivecha/Dropbox/AAA%20Backup/A%20Working/Memory/knowledge-system/mcp-server) tree.

The Cloudflare account is currently on the **Workers Free** plan. Based on Cloudflare's current limits, that is sufficient for the existing personal MCP service pattern, but **not sufficient for the PRD's Dream job if it runs directly in a Cron Trigger**.

The live storage state is materially inconsistent with the desired architecture:

- Redis currently has `573` `knowledge:*` keys and `36` `project:*` keys.
- `index:current` currently claims `3861` topics and `42` projects, so the thin index is stale or duplicated relative to live Redis.
- Upstash Vector currently has `6529` vectors at `3072` dimensions with `COSINE` similarity.
- `0` entries are archived.
- `0` Dream runs exist.
- `0` pending-classification ids exist.

The main implication is that Phase 1 should not start with feature work in the Worker. The immediate priority is data-model normalization plus index cleanup.

## Audit Data

### Repo hygiene

- `.gitignore` covers:
  - `.env`
  - `**/.env`
  - `.dev.vars`
  - checkpoints
  - archives
  - logs
- `cloudflare-mcp/mcp-server/.dev.vars` is not tracked by git.
- Local env files are present at:
  - `knowledge-system/ingestion/.env`
  - `knowledge-system/distillation/.env`

### Live Redis / Vector state

| Metric | Value |
|---|---:|
| `knowledge:*` keys | 573 |
| `project:*` keys | 36 |
| `archived:knowledge:*` keys | 0 |
| `archived:project:*` keys | 0 |
| `dream:run:*` keys | 0 |
| `classification:pending` members | 0 |
| `index:current` present | yes |
| `index:current.topics` | 3861 |
| `index:current.projects` | 42 |
| `index:current.token_count` | 364133 |
| `index:current.generated_at` | `2026-03-26T07:37:23.851148` |
| Vector count | 6529 |
| Vector dimension | 3072 |
| Vector similarity | COSINE |

### Cloudflare Free-plan relevance

Using Cloudflare's current official limits:

- Workers Free:
  - `100,000` requests/day
  - `10 ms` CPU per HTTP request
  - `10 ms` CPU per Cron Trigger
  - `50` subrequests/request
  - `5` cron triggers/account
- Durable Objects Free:
  - available on free with SQLite storage
  - `100,000` requests/day
  - `13,000 GB-s/day` duration
  - documented `30 seconds` default CPU per Durable Object request/alarm
- Workers KV Free:
  - `100,000` reads/day
  - `1,000` writes/day
- Workers Logs Free:
  - `200,000` log events/day
  - `3` days retention

Implication:
- the current MCP service is plausibly fine on free for one-user usage
- the planned nightly Dream logic is **not** safe to run directly in a free-plan Cron Trigger

## Findings

### 1. The thin index is currently not trustworthy

Redis has `573` knowledge entries, but `index:current` contains `3861` topics. That is too large to represent the active keyspace faithfully and far beyond the intended "thin index" footprint.

Implication:
- Any tier-aware `get_index` work must include a rebuild/ownership strategy.
- Dream cannot safely assume `index:current` is a reliable reflection of active state.

### 2. Knowledge entries currently have at least two incompatible metadata shapes

Sampled knowledge entries show two materially different schemas:

- Distillation-style entries have metadata like:
  - `created_at`
  - `updated_at`
  - `source_conversations`
  - `source_messages`
  - `access_count`
  - `last_accessed`
- Ingestion-style entries have metadata like:
  - `updated_at`
  - `source_type`
  - `sources`
  - `project`
  - `github_repo`
  - `github_url`
  - `readme_summary`

Implication:
- Phase 1 must normalize metadata before retrieval logic changes.
- A Worker read-time shim is mandatory during rollout.

### 3. Most live knowledge entries do not even have the current access-tracking fields

Current counts:

- `507 / 573` knowledge entries are missing `access_count`
- `507 / 573` knowledge entries are missing `last_accessed`
- `573 / 573` knowledge entries are missing every new PRD metadata field
- `36 / 36` project entries are missing `access_count` and `last_accessed`

Implication:
- The PRD upgrade is not incremental behavior tuning; it is a real schema migration.

### 4. Current vector metadata is too thin for tier-aware retrieval

Sampled vector metadata currently only contains:

- `type`
- `domain`
- `state`
- `updated_at`

Missing from sampled vectors:

- `source`
- `context_type`
- `injection_tier`
- `archived`
- `salience_score`
- `classification_status`

Implication:
- Upstash Vector metadata filtering cannot support the planned tier/archive filters until a full normalization pass runs.

### 5. Project metadata is still on the old five-field schema

Sampled project entries only contain:

- `created_at`
- `updated_at`
- `source_conversations`
- `source_messages`
- `last_touched`

Implication:
- Phase 1 must extend project metadata at the same time as knowledge metadata.
- The PRD cannot be implemented knowledge-only.

### 6. There are two Worker config files, and `wrangler.json` appears to be the active one

The production Worker folder contains both:

- [wrangler.json](/Users/arjundivecha/Dropbox/AAA%20Backup/A%20Working/Memory/knowledge-system/cloudflare-mcp/mcp-server/wrangler.json)
- [wrangler.jsonc](/Users/arjundivecha/Dropbox/AAA%20Backup/A%20Working/Memory/knowledge-system/cloudflare-mcp/mcp-server/wrangler.jsonc)

`wrangler.json` includes the custom domain route and is more complete. It should be treated as the likely active config unless proven otherwise.

Implication:
- Dream cron work should target `wrangler.json`, not blindly edit `wrangler.jsonc`.

### 7. Free plan is enough for interactive MCP, but not for Dream-as-Cron

Cloudflare's current Workers limits document `10 ms` CPU per Cron Trigger on the free plan. That is too small for any meaningful Survey/Replay/Consolidate/Prune pass, even at the current scale.

At the same time, the free plan appears acceptable for the current interactive service pattern because:

- request volume is personal, not public-scale
- the Worker mostly orchestrates network calls to Upstash and GitHub
- the MCP runtime uses Durable Objects, and Cloudflare documents materially higher per-request CPU for Durable Objects than for free-plan Worker HTTP/Cron invocations

Operational conclusion:

- keep the existing MCP service on the free plan if desired
- do **not** implement Dream as a heavy Worker Cron Trigger on free
- instead choose one of:
  1. external runner or GitHub Actions for Dream
  2. free-plan Cron Trigger that only wakes a Durable Object or enqueues work
  3. upgrade to Workers Paid before implementing Dream in-Worker

## Manual Confirmations Still Needed

These were not fully verifiable from the repo plus current local access:

1. Deployed scheduled-path behavior:
   - Need to verify the deployed Worker rejects external scheduled invocation.
2. OAuth scope model:
   - Needed before adding `restore_archived` and `set_context_type`.
3. Active config ownership:
   - Need to confirm whether `wrangler.json` is the only file used in deployment automation.

## Ready For Phase 1

Phase 1 can start immediately on the following items:

- Add `schema_version`
- Add migration-safe metadata defaults
- Add `classification_status`
- Add Worker read-time migration shim
- Normalize project metadata contract
- Introduce `classification:pending`
- Add checklist/deprecation notes to reduce future confusion

## Recommended Immediate Next Actions

1. Add the Phase 1 schema/default helpers.
2. Add the Worker read-time migration shim.
3. Add `classification:pending` writes in the Python write paths.
4. Add a one-off script to rebuild `index:current` from active Redis state before any retrieval-tier changes ship.
5. Decide whether `wrangler.json` is the canonical deployment config and remove ambiguity before adding cron configuration.
6. Choose the free-plan-compatible Dream execution path before Phase 5 design starts.

## Notes

- This audit used the existing local env/config loading and live Redis/Vector connectivity.
- No live mutations were performed.
- No secrets are copied into this document.
