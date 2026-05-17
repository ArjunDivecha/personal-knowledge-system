# PKS Current-System Audit (2026-05-07)

## Scope

This audit answers three questions:

1. What the current system is supposed to do.
2. What it actually does in production right now.
3. Whether the implementation is correct enough to trust for daily use and further development.

Repository audited: `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system`  
Branch audited: `main` (`6cda38f`)

---

## Expected Behavior (From Repo Contracts)

Primary expected behavior was taken from:

- `README.md`
- `docs/pks-memory-upgrade-checklist.md`
- `docs/testing-matrix.md`
- `cloudflare-mcp/mcp-server/src/index.ts`
- `cloudflare-mcp/mcp-server/src/dream.ts`
- `cloudflare-mcp/mcp-server/wrangler.json`
- `Makefile`

Expected system contract today:

- OAuth-protected MCP service with split surfaces:
  - `/openai/mcp` read-only (`mcp:read`)
  - `/mcp` read/write (`mcp:read`, `mcp:write`)
- Tier-aware retrieval with salience scoring.
- Reconsolidation on retrieval (`access_count`, `last_accessed`, context promotion).
- Scheduled Dream run via Cloudflare cron.
- Reversible archive/restore behavior.
- Repeatable validation commands in `Makefile`.

---

## Live/Code Verification Results

### A) Core runtime and deployment health: **PASS**

Verified production health:

- `GET https://mcp.dancing-ganesh.com/health` returned `status: "ok"`.
- Dream summary values in health are populated (`last_dream_run`, `last_dream_status`, `last_dream_dry_run`).
- Thin index totals are populated and non-empty.

Observed live values at audit time:

- `last_dream_run`: `2026-05-07T07:10:32.000Z`
- `last_dream_status`: `completed`
- `last_dream_dry_run`: `false`
- `thin_index.total_topic_count`: `2526`
- `thin_index.total_project_count`: `34`
- `thin_index.archived_count`: `4424`

### B) OAuth + surface split (`/openai/mcp` vs `/mcp`): **PASS**

Verified in code and live metadata:

- `/mcp/.well-known/oauth-protected-resource` advertises `["mcp:read","mcp:write"]`.
- `/openai/mcp/.well-known/oauth-protected-resource` advertises `["mcp:read"]`.
- Unauthed `POST /mcp` returns proper OAuth challenge (`401` with `WWW-Authenticate`).

Tool availability check via live OAuth session:

- `/openai/mcp`: 6 tools, includes `get_dream_summary`, excludes `create_entry`.
- `/mcp`: 14 tools, includes both `get_dream_summary` and write tools.

### C) Worker and Python test suites: **MIXED**

Passing:

- `make worker-typecheck` passed.
- `make worker-test` passed (`27` tests).
- `make test-python-checker` passed (`25` tests).

Failing:

- `make check-overnight-dream` fails with:
  - `NameError: name 'UTC' is not defined`
  - Root cause: `UTC = timezone.utc` is declared after `main()` is executed in `scripts/check_overnight_dream_run.py`.
- `make verify-memory-full` fails with:
  - `KeyError: 'delta'` in `KnowledgeEntry.from_dict` evolution parsing
  - Root cause: strict deserialization expects `evolution[].delta` on all legacy/live records.

### D) Dream scheduler behavior vs docs: **PARTIAL / DRIFT**

What code actually does:

- Scheduled handler passes caps:
  - `archiveLimit = 10`
  - `promotionLimit = 10`
  - Source: `cloudflare-mcp/mcp-server/src/index.ts`
- Cron is `10 7 * * *` UTC in `wrangler.json` (00:10 PDT during daylight time).

What live run showed:

- Latest scheduled run had:
  - `archive_limit: 10`
  - `promotion_limit: 10`
  - `archived: 10`

Drift:

- Several docs still claim nightly Dream is now “full live runs” with no scheduled caps.
- That is not true in current code/runtime.

### E) Testing/documentation freshness: **PARTIAL / DRIFT**

Found staleness:

- Legacy probe scripts at repo root still target old workers.dev endpoint (`personal-knowledge-mcp.arjun-divecha.workers.dev`).
- `docs/pks-memory-upgrade-checklist.md` contains contradictory statements:
  - says no scheduled caps
  - also says bounded nightly path with caps
- README operational counts are stale (March snapshot), while production now has materially different totals.

---

## Audit Verdict

## ✅ What is correct today

- Production Worker is up and serving correctly.
- OAuth and scope boundaries are correctly enforced.
- Tier-aware retrieval and reconsolidation pathways are implemented.
- Dream run is executing on schedule in live mode.

## ⚠️ What is not correct enough yet

- Two key validation gates in `Makefile` are broken (`check-overnight-dream`, `verify-memory-full`).
- Documentation currently overstates/contradicts scheduled Dream behavior.
- Legacy test scripts can mislead operators by probing old endpoints.

## Bottom line

The system is operational, but operational assurance is not.  
Core runtime works; verification and docs are currently not trustworthy enough for a “fully correct by contract” claim.

---

## Highest-Priority Gaps To Address Before/Within Rewrite

1. Fix validation gate reliability.
   - `check_overnight_dream_run.py` UTC initialization bug.
   - tolerant evolution parsing for legacy records in consistency verifier path.
2. Align docs with runtime truth.
   - explicitly document capped scheduled Dream behavior (or remove caps in code and then document that).
3. Retire or quarantine legacy endpoint probe scripts.
4. Separate “runtime health” from “system correctness” dashboards, so green `/health` cannot hide failing validation gates.

