// Tests for the Dream judge queue module — Worker side of the offline
// judge architecture. The Mac script side is tested separately.

import { beforeEach, describe, expect, it } from "vitest";

import {
	type JudgeQueueItem,
	type JudgeVerdict,
	buildJudgeRubric,
	enqueueJudgeItem,
	getJudgeItem,
	getJudgeVerdict,
	isDuplicateMergeBorderline,
	judgeHistoryKey,
	judgeItemKey,
	judgeVerdictKey,
	listPendingOpIds,
	readPendingVerdicts,
	settleJudgeItem,
	QUEUE_PENDING_SET,
} from "../src/judgeQueue";

// In-memory fake Redis with the subset of methods judgeQueue uses.
function makeFakeRedis() {
	const kv = new Map<string, string>();
	const sets = new Map<string, Set<string>>();
	return {
		kv,
		sets,
		async get(k: string) {
			return kv.get(k) ?? null;
		},
		async set(k: string, v: string) {
			kv.set(k, v);
			return "OK";
		},
		async del(k: string) {
			const had = kv.has(k);
			kv.delete(k);
			return had ? 1 : 0;
		},
		async sadd(k: string, v: string) {
			let s = sets.get(k);
			if (!s) { s = new Set(); sets.set(k, s); }
			const had = s.has(v);
			s.add(v);
			return had ? 0 : 1;
		},
		async srem(k: string, v: string) {
			const s = sets.get(k);
			if (!s) return 0;
			const had = s.has(v);
			s.delete(v);
			return had ? 1 : 0;
		},
		async smembers(k: string) {
			return Array.from(sets.get(k) ?? []);
		},
	};
}

let redis: ReturnType<typeof makeFakeRedis>;

beforeEach(() => {
	redis = makeFakeRedis();
});

function makeItem(opId: string): JudgeQueueItem {
	return {
		op_id: opId,
		op_type: "duplicate_merge_borderline",
		proposal_run_id: "dr_test",
		enqueued_at: "2026-05-17T00:00:00.000Z",
		target_entry_ids: ["ke_a", "ke_b"],
		rubric: buildJudgeRubric("duplicate_merge_borderline"),
		payload: { canonical_id: "ke_a", duplicate_ids: ["ke_b"] },
	};
}

function makeVerdict(opId: string, verdict: "apply" | "skip"): JudgeVerdict {
	return {
		op_id: opId,
		verdict,
		reason: `judge said ${verdict}`,
		judged_at: "2026-05-17T01:00:00.000Z",
		judge_model: "claude-opus-5",
		judge_source: "claude_cli",
	};
}

describe("isDuplicateMergeBorderline", () => {
	it("returns true when canonical has access > 0", () => {
		expect(isDuplicateMergeBorderline({
			canonicalAccessCount: 3,
			duplicateAccessCounts: [0],
		})).toBe(true);
	});

	it("returns true when any duplicate has access > 0", () => {
		expect(isDuplicateMergeBorderline({
			canonicalAccessCount: 0,
			duplicateAccessCounts: [0, 1, 0],
		})).toBe(true);
	});

	it("returns false when all access counts are 0", () => {
		expect(isDuplicateMergeBorderline({
			canonicalAccessCount: 0,
			duplicateAccessCounts: [0, 0, 0],
		})).toBe(false);
	});
});

describe("enqueueJudgeItem + listPendingOpIds + getJudgeItem", () => {
	it("enqueues a new item and lists it", async () => {
		const item = makeItem("op_1");
		const enqueued = await enqueueJudgeItem(redis as any, item);
		expect(enqueued).toBe(true);
		const stored = await getJudgeItem(redis as any, "op_1");
		expect(stored?.op_id).toBe("op_1");
		const pending = await listPendingOpIds(redis as any);
		expect(pending).toEqual(["op_1"]);
	});

	it("does not re-enqueue an existing item", async () => {
		const item = makeItem("op_dup");
		expect(await enqueueJudgeItem(redis as any, item)).toBe(true);
		expect(await enqueueJudgeItem(redis as any, item)).toBe(false);
	});

	it("limit caps listPendingOpIds output", async () => {
		for (let i = 0; i < 5; i += 1) {
			await enqueueJudgeItem(redis as any, makeItem(`op_${i}`));
		}
		const limited = await listPendingOpIds(redis as any, 3);
		expect(limited).toHaveLength(3);
	});
});

describe("getJudgeVerdict + readPendingVerdicts", () => {
	it("readPendingVerdicts only returns op_ids that have BOTH item AND verdict", async () => {
		await enqueueJudgeItem(redis as any, makeItem("op_a"));
		await enqueueJudgeItem(redis as any, makeItem("op_b"));
		// Only op_a has a verdict written.
		await redis.set(judgeVerdictKey("op_a"), JSON.stringify(makeVerdict("op_a", "apply")));
		const pending = await readPendingVerdicts(redis as any);
		expect(pending).toHaveLength(1);
		expect(pending[0].item.op_id).toBe("op_a");
		expect(pending[0].verdict.verdict).toBe("apply");
	});
});

describe("settleJudgeItem", () => {
	it("removes item from pending set and writes history record", async () => {
		await enqueueJudgeItem(redis as any, makeItem("op_x"));
		await redis.set(judgeVerdictKey("op_x"), JSON.stringify(makeVerdict("op_x", "skip")));

		await settleJudgeItem(redis as any, "op_x", "skipped", { reason: "test" });

		const pending = await listPendingOpIds(redis as any);
		expect(pending).not.toContain("op_x");
		// Item and verdict cleared.
		expect(await redis.get(judgeItemKey("op_x"))).toBeNull();
		expect(await redis.get(judgeVerdictKey("op_x"))).toBeNull();
		// History written.
		const history = await redis.get(judgeHistoryKey("op_x"));
		expect(history).not.toBeNull();
		const parsed = JSON.parse(history as string);
		expect(parsed.outcome).toBe("skipped");
		expect(parsed.context.reason).toBe("test");
	});
});

describe("buildJudgeRubric", () => {
	it("returns non-empty text for every op type", () => {
		const types = [
			"duplicate_merge_borderline",
			"promote_tier_borderline",
			"demote_tier1_borderline",
			"high_access_archive",
			"hard_delete_borderline",
		] as const;
		for (const t of types) {
			expect(buildJudgeRubric(t).length).toBeGreaterThan(20);
		}
	});
});
