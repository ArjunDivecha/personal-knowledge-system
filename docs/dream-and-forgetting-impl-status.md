# Dream + Forgetting — Implementation Status

**Branch:** `feature/dream-and-forgetting`
**Worktree:** `~/.claude-worktrees/dream-and-forgetting`
**Design doc:** `docs/dream-and-forgetting-design-2026-05-17.md` (Codex-approved)
**Implemented:** 2026-05-17 overnight, 7 commits, 122 tests passing, TypeScript clean

## What was built

All 7 stages from the staging plan, each in its own commit. Defaults are off everywhere — flipping no env vars preserves current behavior. The user explicitly authorized the work overnight and asked for everything done and tested before they wake up.

### Stage 1 — Foundations
- Three kill-switch env vars in `env.d.ts`: `DREAM_AUTO_APPLY_MODE`, `DREAM_OPUS_MODE`, `RETRIEVAL_POLICY_MODE` (all default `off`).
- New entry metadata fields: `injection_quarantine`, `quarantined_at`, `quarantine_streak_nights`.
- Three exported helpers in `dream.ts`: `quarantineEntryMetadata`, `liftQuarantineMetadata`, `demoteTierMetadata`.
- 8 new unit tests.

### Stage 2 — Layer 0 Retrieval Policy
- New module `src/retrievalPolicy.ts`: keyword-based query intent classifier (6 buckets), entry topic classifier, cross-context penalty, quarantine penalty.
- Wired into the search tool: `base_score → policy_multiplier → final_score`. Top-level result now includes a `retrieval_policy` block.
- Off by default; flipping `RETRIEVAL_POLICY_MODE=on` activates it.
- 19 new tests including the canonical Panchakarma integration scenario.

### Stage 3 — Scheduled Auto-Apply (L1 existing + L2 new)
- Scheduled handler now supports three modes:
  - unset / `off`: proposal-only (`runDreamProposal`)
  - `governed`: autonomous `runDreamProposal → gradeDreamProposal → applyDreamProposal` for low/medium-risk allowlisted operations within scheduled caps, with an apply-verification run record
  - `full`: legacy direct `runDreamCycle` path
- New Layer 2 phase `applyLayer2QuarantineAndDemote`: 3 nights below threshold → quarantine; 7 more nights → tier demote. Cap 100 ops/night. Tier-3 floor never demotes.
- `reconsolidateEntry` lifts quarantine on any retrieval reinforcement — closes the reversibility loop.
- New `phases.layer2_quarantine_and_demote` and per-counts in run record.
- 8 new tests covering scheduled handler modes + Layer 2 phase logic.

### Stage 4 — Anomaly Tripwires
- New module `src/tripwires.ts`: destructive-action spike + retrieval-hit collapse detectors. Both require 2 consecutive breach days with proper sample sizes; cold-start guards prevent false positives.
- Auto-flips a Redis kill flag when tripped; `getEffectiveMode` collapses (env_var, kill_flag) with off-wins semantics.
- Wired into `archiveEntry` (destructive counter) and the search tool (retrieval hit counter).
- Scheduled handler now checks tripwires at cycle start, sets kill flag if needed, and uses effective mode to decide between proposal and cycle.
- Two new operator HTTP endpoints: `GET /ops/dream/tripwire_status` and `POST /ops/dream/clear_kill_flag`.
- Hard-delete cap helpers (`isHardDeleteCapReached`, `recordHardDelete`) ready for L4 integration.
- 19 new tests + 1 scheduled-handler tripwire-fallback test.

### Stage 5a — Worker-Side Judge Queue
- New module `src/judgeQueue.ts`: queue primitives + 5 op_type variants + `buildJudgeRubric` per type.
- `isDuplicateMergeBorderline`: any entry with `access_count > 0` makes the merge borderline.
- `runDreamCycle` integration:
  - At cycle start with `DREAM_OPUS_MODE=on`: `readPendingVerdicts` and apply or settle each.
  - During duplicate-merge phase: bright-line auto-applies (current behavior); borderline enqueues to judge queue (only when Opus mode is on; otherwise still auto-applies).
- Two new operator HTTP endpoints: `GET /ops/dream/judge_queue` (Mac script polls) and `POST /ops/dream/judge_verdict` (Mac script posts).
- `judge_queue` block added to run record.
- 11 new tests.

### Stage 5b — Mac-Side Judge Script
- New module `ingestion/dream_judge/run.py`: nightly script that polls the Worker queue, asks Opus to decide each border case, posts verdicts back.
- **Subscription billing**: tries `claude --print --model claude-opus-4-6` first (Claude Code subscription credits, no API cost).
- **Fallback**: if the CLI is missing / times out / returns unparseable output, falls back to the Anthropic API with a logged warning, so a single failed CLI call doesn't break the queue.
- Conservative prompt with bias-to-skip ("cost of a wrong apply is higher than wrong skip").
- Robust verdict parser handles markdown fences, preamble, and code blocks.
- Wired into `scripts/run_nightly_ingestion.sh` as the 4th pipeline step. Non-fatal — judge failures don't block the rest of ingestion.
- 12 unit tests for the parser + prompt builder.

### Stage 6 — Weekly Digest
- New script `scripts/generate_weekly_dream_digest.py`: produces `scripts/reports/dream-weekly-YYYY-WW.md` from a week's worth of cycle records, judge history, and live tripwire status.
- Queries Upstash REST directly (no extra deps) + the Worker's `/ops/dream/tripwire_status` for kill flag state.
- 8 unit tests.

## Test summary

| Suite | Status |
|---|---|
| Worker TypeScript (vitest) | **102/102 passing** across 9 test files |
| Worker `tsc --noEmit` | **clean** (exit 0) |
| `scripts/test_weekly_dream_digest.py` (unittest) | **8/8 passing** |
| `ingestion/dream_judge/test_parser.py` (inline) | **all passing** |

## What you need to do when you wake up

### 0. Review the branch
```bash
cd "/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system"
git fetch origin
git log --oneline main..origin/feature/dream-and-forgetting   # 7 commits
git diff main...origin/feature/dream-and-forgetting --stat
```

Worktree is at `~/.claude-worktrees/dream-and-forgetting` if you want to inspect locally.

### 1. Merge or stash review
Either merge the feature branch:
```bash
cd "/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system"
git checkout main
git merge feature/dream-and-forgetting
```
Or keep reviewing in the worktree first.

Note: there are uncommitted changes on `main` (from prior work — `src/tweets/`, new tests, etc.) that need to be sorted before merging.

### 2. Deploy the Worker
The Worker code change is the substantive deploy. Everything new defaults to off so the deploy is safe.
```bash
cd "/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/cloudflare-mcp/mcp-server"
npx wrangler deploy
```
Wrangler vars to set (in Cloudflare dashboard or via `wrangler secret`):
- None required initially — defaults are `off`.

### 3. Confirm `DREAM_OPERATOR_TOKEN` in ingestion/.env
The Mac judge script reads it from `ingestion/.env`. If not there, copy the Worker's secret value into it.

### 4. Three-phase rollout (per the design)
Flip env vars one week at a time via Wrangler:

**Week 1 — Retrieval policy on**
```bash
echo "on" | npx wrangler secret put RETRIEVAL_POLICY_MODE
```
Watch search behavior. If anything feels wrong:
```bash
echo "off" | npx wrangler secret put RETRIEVAL_POLICY_MODE
```

**Week 2 — Deterministic auto-apply**
```bash
echo "governed" | npx wrangler secret put DREAM_AUTO_APPLY_MODE
```
After tonight, the scheduled cron will run live through the governed lifecycle: proposal, deterministic grade, bounded apply, and apply verification. Layer 2 quarantine/demote remains on the legacy `full` path until it is represented as proposal/apply operations.

**Week 3 — Opus border cases**
```bash
echo "on" | npx wrangler secret put DREAM_OPUS_MODE
```
At this point the Mac script (which is already running nightly per `run_nightly_ingestion.sh`) will start producing verdicts. Their effects apply next Worker cycle.

### 5. Generate first digest
After at least one cycle completes:
```bash
cd "/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system"
python scripts/generate_weekly_dream_digest.py
open scripts/reports/dream-weekly-*.md
```

## Things I didn't do (and why)

- **No L4 hard-delete implementation.** L4 was in the design but no L4 ops exist in `runDreamCycle` yet. The tripwire hard-delete cap helpers are ready (`isHardDeleteCapReached`, `recordHardDelete`), the judge queue supports `hard_delete_borderline` ops, but the actual soft-delete → hard-delete machinery isn't wired into the cycle. Belongs in a follow-up.
- **No `wrangler.jsonc` cron change.** Confirmed your cron is dashboard-managed (the Worker has been running nightly without anything in `wrangler.jsonc`). Left as-is so I don't accidentally double-fire.
- **No automatic deploy.** I made the changes; you run `wrangler deploy`. Auto-deploying overnight from a worktree felt one step too far.
- **No live Redis testing.** Vitest mocks Redis; I didn't hit production data with the new code. Real validation happens when you flip the first env var.

## Commit log

```
$ git log --oneline main..feature/dream-and-forgetting
```
(7 stage commits, see worktree for full messages)
