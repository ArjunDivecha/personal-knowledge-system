// =============================================================================
// DREAM JUDGE QUEUE — Worker side
// =============================================================================
// Border-case proposals that aren't safely decidable by deterministic rules
// get enqueued here. The Mac-side judge script (ingestion/dream_judge/run.py)
// reads pending items, calls Opus via the Claude Code CLI (subscription
// credits, not API fees), and writes verdicts back. On the next Worker cycle,
// the Worker reads the verdicts and applies or skips accordingly.
//
// Redis keys:
//   dream:judge:pending             — set of pending op_ids
//   dream:judge:item:{op_id}        — JSON of JudgeQueueItem
//   dream:judge:verdict:{op_id}     — JSON of JudgeVerdict (written by Mac script)
//   dream:judge:history:{op_id}     — JSON of historical decision (post-apply)
//
// Gated externally by env.DREAM_OPUS_MODE — when "off" (default), border-case
// ops are simply skipped (not enqueued, not applied). Applying skipped ops
// later is a future operator choice.
// =============================================================================

import type { Redis } from "@upstash/redis/cloudflare";

export const QUEUE_PENDING_SET = "dream:judge:pending";

export function judgeItemKey(opId: string): string {
	return `dream:judge:item:${opId}`;
}

export function judgeVerdictKey(opId: string): string {
	return `dream:judge:verdict:${opId}`;
}

export function judgeHistoryKey(opId: string): string {
	return `dream:judge:history:${opId}`;
}

export type JudgeOpType =
	| "duplicate_merge_borderline"
	| "promote_tier_borderline"
	| "demote_tier1_borderline"
	| "high_access_archive"
	| "hard_delete_borderline"
	// 3.4 — contradiction resolution: both entries are contested; judge decides
	// whether they are truly contradictory (skip = keep both contested, needs
	// Phase 7C supersession) or complementary/compatible (apply = restore active).
	| "contradiction_resolution";

export interface JudgeQueueItem {
	op_id: string;
	op_type: JudgeOpType;
	proposal_run_id: string;
	enqueued_at: string;
	target_entry_ids: string[];
	rubric: string;
	payload: Record<string, unknown>;
}

export interface JudgeVerdict {
	op_id: string;
	verdict: "apply" | "skip";
	reason: string;
	judged_at: string;
	judge_model: string;
	judge_source: "claude_cli" | "anthropic_api";
}

/**
 * Enqueue a border-case op for offline judging. Best-effort — never throws.
 * Returns true if enqueued, false on error or if the op was already queued.
 */
export async function enqueueJudgeItem(
	redis: Redis,
	item: JudgeQueueItem,
): Promise<boolean> {
	try {
		const itemKey = judgeItemKey(item.op_id);
		const existing = await redis.get(itemKey);
		if (existing) return false;
		await redis.set(itemKey, JSON.stringify(item));
		await (redis as unknown as { sadd: (k: string, v: string) => Promise<unknown> }).sadd(
			QUEUE_PENDING_SET,
			item.op_id,
		);
		return true;
	} catch {
		return false;
	}
}

/** List up to `limit` pending op_ids (for Mac script to fetch + judge). */
export async function listPendingOpIds(redis: Redis, limit = 100): Promise<string[]> {
	try {
		const members = await (redis as unknown as {
			smembers: (k: string) => Promise<string[]>;
		}).smembers(QUEUE_PENDING_SET);
		return (members ?? []).slice(0, limit);
	} catch {
		return [];
	}
}

export async function getJudgeItem(redis: Redis, opId: string): Promise<JudgeQueueItem | null> {
	const v = await redis.get(judgeItemKey(opId));
	if (!v) return null;
	if (typeof v === "string") {
		try { return JSON.parse(v) as JudgeQueueItem; } catch { return null; }
	}
	return v as JudgeQueueItem;
}

export async function getJudgeVerdict(redis: Redis, opId: string): Promise<JudgeVerdict | null> {
	const v = await redis.get(judgeVerdictKey(opId));
	if (!v) return null;
	if (typeof v === "string") {
		try { return JSON.parse(v) as JudgeVerdict; } catch { return null; }
	}
	return v as JudgeVerdict;
}

/**
 * Remove an op from the pending set and persist a record of its outcome
 * to dream:judge:history:{op_id} for the weekly digest and audit trail.
 */
export async function settleJudgeItem(
	redis: Redis,
	opId: string,
	outcome: "applied" | "skipped" | "stale",
	context: Record<string, unknown> = {},
): Promise<void> {
	const item = await getJudgeItem(redis, opId);
	const verdict = await getJudgeVerdict(redis, opId);
	const history = {
		op_id: opId,
		outcome,
		settled_at: new Date().toISOString(),
		item,
		verdict,
		context,
	};
	try {
		await redis.set(judgeHistoryKey(opId), JSON.stringify(history));
		await (redis as unknown as { srem: (k: string, v: string) => Promise<unknown> }).srem(
			QUEUE_PENDING_SET,
			opId,
		);
		await (redis as unknown as { del: (k: string) => Promise<unknown> }).del(judgeItemKey(opId));
		await (redis as unknown as { del: (k: string) => Promise<unknown> }).del(judgeVerdictKey(opId));
	} catch {
		// best-effort
	}
}

/**
 * Consume any pending verdicts that the Mac script has written since the
 * last cycle. Returns the verdicts paired with their original items so the
 * caller can act on each one (apply, skip, etc).
 */
export interface PendingVerdict {
	item: JudgeQueueItem;
	verdict: JudgeVerdict;
}

export async function readPendingVerdicts(redis: Redis): Promise<PendingVerdict[]> {
	const opIds = await listPendingOpIds(redis, 1000);
	const out: PendingVerdict[] = [];
	for (const opId of opIds) {
		const verdict = await getJudgeVerdict(redis, opId);
		if (!verdict) continue;
		const item = await getJudgeItem(redis, opId);
		if (!item) continue;
		out.push({ item, verdict });
	}
	return out;
}

/**
 * Classifier: would this proposed duplicate-merge plan be a border case?
 * Border case = canonical or any duplicate has access_count > 0.
 * Bright-line auto-apply case = all entries have access_count == 0
 * (no human ever retrieved them — merging is purely cleanup).
 */
export function isDuplicateMergeBorderline(params: {
	canonicalAccessCount: number;
	duplicateAccessCounts: number[];
}): boolean {
	if (params.canonicalAccessCount > 0) return true;
	return params.duplicateAccessCounts.some((c) => c > 0);
}

/**
 * Build the rubric text passed to the judge for a given border-case op type.
 * Kept here (not in the Mac script) so prompt + payload stay versioned with
 * the Worker code that produces them.
 */
export function buildJudgeRubric(opType: JudgeOpType): string {
	switch (opType) {
		case "duplicate_merge_borderline":
			return "Given two knowledge entries that have the same domain label and overlapping content, decide whether they represent the SAME memory (apply: merge them) or two DISTINCT memories that happen to share vocabulary (skip: keep both). Apply when their current_view text is substantively the same idea. Skip when they cover different aspects, projects, or time periods.";
		case "promote_tier_borderline":
			return "Decide whether this entry has earned promotion to a higher injection tier. Apply when access pattern + content quality justify higher prominence (e.g., reliably useful in many sessions). Skip when access is incidental or the content isn't durable.";
		case "demote_tier1_borderline":
			return "Decide whether this tier-1 (identity-level) entry should be demoted. Apply only when the entry's claim is no longer accurate, the user has visibly moved on, or it's been silent for months despite ample chances to reinforce. Skip when in doubt — tier-1 is identity.";
		case "high_access_archive":
			return "This archive candidate has been accessed in the past 90 days, which usually means it matters. Apply only if the access pattern is clearly noise (e.g., automated retrieval, single accidental query). Skip when in doubt.";
		case "hard_delete_borderline":
			return "Decide whether to permanently delete this archived entry. Apply only if there is no plausible future relevance: not a recurring project, not an identity element, not a returning interest. Skip when in doubt — hard-delete is the only irreversible step.";
		case "contradiction_resolution":
			return "Two knowledge entries were flagged as contradicting each other and both are now marked 'contested'. Read their current_view texts and the listed contradiction reasons. Apply if they are NOT genuinely contradictory (i.e., they are complementary, about different time periods, or the contradiction reasons are spurious) — this restores both entries to active. Skip if the contradiction is real and requires human resolution or a Phase 7C supersession step to reconcile the conflicting views.";
	}
}
