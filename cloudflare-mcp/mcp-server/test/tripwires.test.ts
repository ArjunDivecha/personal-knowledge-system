// Tests for the anomaly-tripwire module.
//
// Verifies that:
//   - destructive-action spike detection fires only after CONSECUTIVE_DAYS_REQUIRED
//     breaches of the 14-day median × DESTRUCTIVE_SPIKE_MULTIPLIER, with a
//     minimum-actionable floor to avoid tripping on quiet starts
//   - retrieval-hit collapse detection requires the baseline to be meaningful
//     and ignores days with too few samples
//   - getEffectiveMode collapses (env_var, kill_flag) into a single answer with
//     "off" winning
//   - recordDestructiveAction / recordSearchQuery / recordHardDelete update the
//     correct daily counters

import { beforeEach, describe, expect, it } from "vitest";

import {
	CONSECUTIVE_DAYS_REQUIRED,
	checkDestructiveTripwire,
	checkRetrievalTripwire,
	clearKillFlag,
	DESTRUCTIVE_SPIKE_MULTIPLIER,
	getEffectiveMode,
	HARD_DELETE_DAILY_CAP_DEFAULT,
	isHardDeleteCapReached,
	isoDate,
	recordDestructiveAction,
	recordHardDelete,
	recordSearchQuery,
	setKillFlag,
} from "../src/tripwires";

// Minimal in-memory fake Redis matching the upstash interface used by tripwires.
function makeFakeRedis() {
	const store = new Map<string, string | number>();
	return {
		store,
		async incr(k: string) {
			const v = Number(store.get(k) ?? 0) + 1;
			store.set(k, v);
			return v;
		},
		async set(k: string, v: string) {
			store.set(k, v);
			return "OK";
		},
		async get(k: string) {
			const v = store.get(k);
			return v === undefined ? null : v;
		},
		async del(k: string) {
			const had = store.has(k);
			store.delete(k);
			return had ? 1 : 0;
		},
	};
}

function daysAgo(d: Date, n: number): string {
	const out = new Date(d.getTime());
	out.setUTCDate(out.getUTCDate() - n);
	return out.toISOString().slice(0, 10);
}

let redis: ReturnType<typeof makeFakeRedis>;
const NOW = new Date("2026-05-17T07:00:00.000Z");

beforeEach(() => {
	redis = makeFakeRedis();
});

describe("recordDestructiveAction + checkDestructiveTripwire", () => {
	it("does not trip when no signal exists", async () => {
		const result = await checkDestructiveTripwire(redis as any, NOW);
		expect(result.tripped).toBe(false);
		expect(result.consecutive_breaches).toBe(0);
	});

	it("does not trip on a single bad day", async () => {
		// 14 quiet baseline days with 1 destructive each, then a single bad day
		for (let i = 3; i <= 16; i += 1) {
			await redis.set(`tripwire:destructive:${daysAgo(NOW, i)}`, 1);
		}
		// yesterday: spike
		await redis.set(`tripwire:destructive:${daysAgo(NOW, 1)}`, 100);
		// day-before-yesterday: quiet
		await redis.set(`tripwire:destructive:${daysAgo(NOW, 2)}`, 1);

		const result = await checkDestructiveTripwire(redis as any, NOW);
		expect(result.tripped).toBe(false);
		expect(result.consecutive_breaches).toBe(1);
	});

	it("trips when CONSECUTIVE_DAYS_REQUIRED days breach the threshold", async () => {
		// 14 quiet days with 2 destructive each → median 2 → threshold 6
		for (let i = 3; i <= 16; i += 1) {
			await redis.set(`tripwire:destructive:${daysAgo(NOW, i)}`, 2);
		}
		await redis.set(`tripwire:destructive:${daysAgo(NOW, 2)}`, 100);
		await redis.set(`tripwire:destructive:${daysAgo(NOW, 1)}`, 100);

		const result = await checkDestructiveTripwire(redis as any, NOW);
		expect(result.tripped).toBe(true);
		expect(result.consecutive_breaches).toBe(CONSECUTIVE_DAYS_REQUIRED);
		expect(result.baseline_median).toBe(2);
		expect(result.threshold).toBe(DESTRUCTIVE_SPIKE_MULTIPLIER * 2);
	});

	it("uses minimum-actionable floor when baseline median is 0", async () => {
		// Zero baseline + 2 days of 2 destructive each — would breach if floor
		// weren't applied; should NOT trip because min-actionable is 3.
		await redis.set(`tripwire:destructive:${daysAgo(NOW, 1)}`, 2);
		await redis.set(`tripwire:destructive:${daysAgo(NOW, 2)}`, 2);
		const result = await checkDestructiveTripwire(redis as any, NOW);
		expect(result.tripped).toBe(false);
		expect(result.threshold).toBeGreaterThanOrEqual(3);
	});

	it("recordDestructiveAction writes to today's counter", async () => {
		const today = isoDate(NOW);
		// recordDestructiveAction uses current date, so we just verify the
		// incr targets today's key when invoked.
		await recordDestructiveAction(redis as any, NOW);
		expect(Number(redis.store.get(`tripwire:destructive:${today}`))).toBe(1);
		await recordDestructiveAction(redis as any, NOW);
		expect(Number(redis.store.get(`tripwire:destructive:${today}`))).toBe(2);
	});
});

describe("recordSearchQuery + checkRetrievalTripwire", () => {
	it("does not trip on insufficient baseline data", async () => {
		const result = await checkRetrievalTripwire(redis as any, NOW);
		expect(result.tripped).toBe(false);
	});

	it("does not trip when baseline is too low (cold start)", async () => {
		// 14 baseline days, each with 50 queries, 5 hits (10% hit rate)
		// Two recent days at 0% — would breach ratio, but baseline is only 10%
		// which is at the bound; per the rule (baselineMedian > 0.1) it does not trip.
		for (let i = 3; i <= 16; i += 1) {
			await redis.set(`tripwire:retrieval:total:${daysAgo(NOW, i)}`, 50);
			await redis.set(`tripwire:retrieval:hits:${daysAgo(NOW, i)}`, 5);
		}
		await redis.set(`tripwire:retrieval:total:${daysAgo(NOW, 1)}`, 50);
		await redis.set(`tripwire:retrieval:hits:${daysAgo(NOW, 1)}`, 0);
		await redis.set(`tripwire:retrieval:total:${daysAgo(NOW, 2)}`, 50);
		await redis.set(`tripwire:retrieval:hits:${daysAgo(NOW, 2)}`, 0);
		const result = await checkRetrievalTripwire(redis as any, NOW);
		expect(result.tripped).toBe(false);
	});

	it("trips when retrieval hit-ratio collapses below threshold for required days", async () => {
		// 14 baseline days each at 80% hit rate (high baseline)
		for (let i = 3; i <= 16; i += 1) {
			await redis.set(`tripwire:retrieval:total:${daysAgo(NOW, i)}`, 50);
			await redis.set(`tripwire:retrieval:hits:${daysAgo(NOW, i)}`, 40);
		}
		// Recent 2 days collapsed to 10% hit rate
		await redis.set(`tripwire:retrieval:total:${daysAgo(NOW, 1)}`, 50);
		await redis.set(`tripwire:retrieval:hits:${daysAgo(NOW, 1)}`, 5);
		await redis.set(`tripwire:retrieval:total:${daysAgo(NOW, 2)}`, 50);
		await redis.set(`tripwire:retrieval:hits:${daysAgo(NOW, 2)}`, 5);
		const result = await checkRetrievalTripwire(redis as any, NOW);
		expect(result.tripped).toBe(true);
		expect(result.consecutive_breaches).toBe(CONSECUTIVE_DAYS_REQUIRED);
	});

	it("ignores days with too few samples (under 10 queries)", async () => {
		// Baseline 80% across 14 days
		for (let i = 3; i <= 16; i += 1) {
			await redis.set(`tripwire:retrieval:total:${daysAgo(NOW, i)}`, 50);
			await redis.set(`tripwire:retrieval:hits:${daysAgo(NOW, i)}`, 40);
		}
		// Recent days with only 2 queries each → ignored
		await redis.set(`tripwire:retrieval:total:${daysAgo(NOW, 1)}`, 2);
		await redis.set(`tripwire:retrieval:hits:${daysAgo(NOW, 1)}`, 0);
		await redis.set(`tripwire:retrieval:total:${daysAgo(NOW, 2)}`, 2);
		await redis.set(`tripwire:retrieval:hits:${daysAgo(NOW, 2)}`, 0);
		const result = await checkRetrievalTripwire(redis as any, NOW);
		expect(result.tripped).toBe(false);
	});

	it("recordSearchQuery updates both counters", async () => {
		const today = isoDate(NOW);
		await recordSearchQuery(redis as any, true, NOW);
		expect(Number(redis.store.get(`tripwire:retrieval:total:${today}`))).toBe(1);
		expect(Number(redis.store.get(`tripwire:retrieval:hits:${today}`))).toBe(1);
		await recordSearchQuery(redis as any, false, NOW);
		expect(Number(redis.store.get(`tripwire:retrieval:total:${today}`))).toBe(2);
		expect(Number(redis.store.get(`tripwire:retrieval:hits:${today}`))).toBe(1);
	});
});

describe("hard-delete cap", () => {
	it("isHardDeleteCapReached returns false when below cap", async () => {
		expect(await isHardDeleteCapReached(redis as any, HARD_DELETE_DAILY_CAP_DEFAULT, NOW)).toBe(false);
	});

	it("isHardDeleteCapReached returns true at the cap", async () => {
		await redis.set(`tripwire:hard_delete:${isoDate(NOW)}`, HARD_DELETE_DAILY_CAP_DEFAULT);
		expect(await isHardDeleteCapReached(redis as any, HARD_DELETE_DAILY_CAP_DEFAULT, NOW)).toBe(true);
	});

	it("recordHardDelete bumps the counter", async () => {
		const v1 = await recordHardDelete(redis as any, NOW);
		const v2 = await recordHardDelete(redis as any, NOW);
		expect(v1).toBe(1);
		expect(v2).toBe(2);
	});
});

describe("getEffectiveMode + kill flag", () => {
	it("off env → effective off, no trip lookup needed", async () => {
		const mode = await getEffectiveMode(redis as any, "off", "DREAM_AUTO_APPLY_MODE");
		expect(mode.effective).toBe("off");
		expect(mode.tripped).toBe(false);
	});

	it("undefined env → effective off", async () => {
		const mode = await getEffectiveMode(redis as any, undefined, "DREAM_AUTO_APPLY_MODE");
		expect(mode.effective).toBe("off");
	});

	it("full env, no kill flag → effective full", async () => {
		const mode = await getEffectiveMode(redis as any, "full", "DREAM_AUTO_APPLY_MODE");
		expect(mode.effective).toBe("full");
		expect(mode.tripped).toBe(false);
	});

	it("governed env, no kill flag → effective governed", async () => {
		const mode = await getEffectiveMode(redis as any, "governed", "DREAM_AUTO_APPLY_MODE");
		expect(mode.effective).toBe("governed");
		expect(mode.tripped).toBe(false);
	});

	it("on env + kill flag set → effective off + tripped", async () => {
		await setKillFlag(redis as any, "RETRIEVAL_POLICY_MODE", {
			tripped_at: "2026-05-17T00:00:00.000Z",
			reason: "test",
			source_tripwire: "retrieval_collapse",
		});
		const mode = await getEffectiveMode(redis as any, "on", "RETRIEVAL_POLICY_MODE");
		expect(mode.effective).toBe("off");
		expect(mode.tripped).toBe(true);
		expect(mode.trip_record?.reason).toBe("test");
	});

	it("clearKillFlag restores effective mode to env value", async () => {
		await setKillFlag(redis as any, "DREAM_AUTO_APPLY_MODE", {
			tripped_at: "2026-05-17T00:00:00.000Z",
			reason: "test",
			source_tripwire: "destructive_spike",
		});
		expect((await getEffectiveMode(redis as any, "full", "DREAM_AUTO_APPLY_MODE")).effective).toBe("off");
		await clearKillFlag(redis as any, "DREAM_AUTO_APPLY_MODE");
		expect((await getEffectiveMode(redis as any, "full", "DREAM_AUTO_APPLY_MODE")).effective).toBe("full");
	});
});
