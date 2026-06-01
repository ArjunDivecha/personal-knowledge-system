// Tests for applyPercentileRetier — the Phase 3 (R3.3) cycle phase that
// recomputes injection_tier from the salience percentile of the whole active
// corpus and persists the entries whose tier changed. This is what keeps the
// Tier-1 share pinned at the configured top percentile instead of drifting up
// as new/reconsolidated entries inherit their static context-type tier.
//
// Uses in-memory fake Redis/Vector — verifies the percentile cutoffs, the
// identity floor, the change-only persistence, the per-run cap, and 503
// resilience. Does not exercise the real Upstash persist path.

import { describe, expect, it } from "vitest";

import { RETIER_PER_RUN_CAP, applyPercentileRetier } from "../src/dream";
import { assignTierByPercentile } from "../src/salience";

function makeFakeRedis() {
	const store = new Map<string, string>();
	return {
		store,
		async set(k: string, v: string) {
			store.set(k, v);
			return "OK";
		},
		async get(k: string) {
			return store.get(k) ?? null;
		},
	};
}

function makeFakeVector(opts: { failIds?: Set<string> } = {}) {
	const updates: any[] = [];
	return {
		updates,
		async upsert(arg: any) {
			updates.push(arg);
			return { upserted: 1 };
		},
		async update(arg: any) {
			if (opts.failIds?.has(String(arg.id))) {
				throw new Error("The vector store backend is currently unavailable.");
			}
			updates.push(arg);
			return { updated: 1 };
		},
	};
}

function makeEntry(args: {
	id: string;
	tier: 1 | 2 | 3;
	salience: number;
	contextType?: string;
	archived?: boolean;
}) {
	const metadata: Record<string, unknown> = {
		injection_tier: args.tier,
		salience_score: args.salience,
		context_type: args.contextType ?? "task_query",
		mention_count: 1,
		access_count: 0,
		confidence: "high",
		updated_at: "2026-05-01T00:00:00.000Z",
		archived: Boolean(args.archived),
	};
	const entry: Record<string, unknown> = {
		id: args.id,
		type: "knowledge",
		domain: `domain ${args.id}`,
		current_view: "view",
		confidence: "high",
		// Pin salience so the percentile ordering is deterministic regardless of
		// the live computeSalience formula: it reads metadata.salience_score only
		// as a fallback, so we drive ordering via the fields computeSalience uses.
		metadata,
	};
	return {
		id: args.id,
		type: "knowledge" as const,
		entry,
		metadata,
		label: args.id,
		updatedAt: "2026-05-01T00:00:00.000Z",
		contextType: args.contextType ?? "task_query",
		injectionTier: args.tier,
		mentionCount: 1,
		accessCount: 0,
		sourceConversationCount: 1,
		salienceScore: args.salience,
	};
}

// computeSalience is deterministic from entry fields. To get a controlled
// salience ORDERING we vary confidence/mention_count so the ranking is stable.
// Helper: build N entries with strictly increasing salience by mention_count.
function rankedEntries(n: number, tier: 1 | 2 | 3 = 3) {
	return Array.from({ length: n }, (_, i) => {
		const e = makeEntry({ id: `ke_${String(i).padStart(3, "0")}`, tier, salience: 0 });
		// Higher mention_count -> higher freqBoost -> higher salience. Distinct per i.
		e.metadata.mention_count = i + 1;
		e.mentionCount = i + 1;
		return e;
	});
}

describe("applyPercentileRetier", () => {
	it("assigns tiers by salience percentile over the active set", async () => {
		const redis = makeFakeRedis();
		const vector = makeFakeVector();
		// 20 entries, all currently tier 3, strictly increasing salience.
		const entries = rankedEntries(20, 3);

		const summary = await applyPercentileRetier(redis as any, vector as any, entries as any);

		expect(summary.evaluated).toBe(20);
		// Default policy: top 15% -> T1 (3 of 20), next 25% -> T2 (5 of 20), rest T3.
		expect(summary.tier_counts[1]).toBe(3);
		expect(summary.tier_counts[2]).toBe(5);
		expect(summary.tier_counts[3]).toBe(12);
		// The highest-salience entry (ke_019) must end up tier 1.
		const top = entries.find((e) => e.id === "ke_019")!;
		expect(top.metadata.injection_tier).toBe(1);
		// The lowest stays tier 3.
		const bottom = entries.find((e) => e.id === "ke_000")!;
		expect(bottom.metadata.injection_tier).toBe(3);
	});

	it("only persists entries whose tier actually changed", async () => {
		const redis = makeFakeRedis();
		const vector = makeFakeVector();
		const entries = rankedEntries(20, 3); // all start at tier 3

		const summary = await applyPercentileRetier(redis as any, vector as any, entries as any);

		// 3 promoted to T1 + 5 to T2 = 8 changed; the 12 that stay T3 are not written.
		expect(summary.changed).toBe(8);
		expect(vector.updates).toHaveLength(8);
	});

	it("excludes archived entries from the percentile population", async () => {
		const redis = makeFakeRedis();
		const vector = makeFakeVector();
		const entries = rankedEntries(10, 3);
		// Mark the top two as archived — they must not count toward cutoffs or be written.
		entries[9].metadata.archived = true;
		entries[8].metadata.archived = true;

		const summary = await applyPercentileRetier(redis as any, vector as any, entries as any);

		expect(summary.evaluated).toBe(8);
		// Archived entries never get a tier write.
		const archivedWritten = vector.updates.some(
			(u) => u.id === entries[9].id || u.id === entries[8].id,
		);
		expect(archivedWritten).toBe(false);
	});

	it("honors the identity floor (durable types never fall below tier 2)", () => {
		// Tested at the pure-function layer: assignTierByPercentile is where the
		// floor lives. (Routing it through computeSalience in the phase would
		// raise a professional_identity entry's salience via its type multiplier,
		// confounding the very thing we want to isolate.)
		const salienceById: Record<string, number> = {};
		const contextTypeById: Record<string, string> = {};
		for (let i = 0; i < 20; i += 1) {
			const id = `ke_${String(i).padStart(3, "0")}`;
			salienceById[id] = i / 100; // ke_000 lowest
			contextTypeById[id] = "task_query";
		}
		// Lowest-salience entry is durable identity -> percentile says T3, floor lifts to T2.
		contextTypeById["ke_000"] = "professional_identity";

		const tiers = assignTierByPercentile(salienceById, contextTypeById);
		expect(tiers["ke_000"]).toBe(2);
		// A non-identity entry at the same low salience stays T3.
		expect(tiers["ke_001"]).toBe(3);
	});

	it("caps the number of persists per run at RETIER_PER_RUN_CAP", async () => {
		const redis = makeFakeRedis();
		const vector = makeFakeVector();
		// Enough entries that the count of tier CHANGES exceeds the cap.
		// All start at tier 1, so most will change (down to T2/T3) -> > cap changes.
		const entries = rankedEntries(RETIER_PER_RUN_CAP + 600, 1);

		const summary = await applyPercentileRetier(redis as any, vector as any, entries as any);

		expect(summary.cap_hit).toBe(true);
		expect(summary.changed).toBeLessThanOrEqual(RETIER_PER_RUN_CAP);
		expect(vector.updates.length).toBeLessThanOrEqual(RETIER_PER_RUN_CAP);
	});

	it("is resilient to per-entry vector failures (counts, does not throw)", async () => {
		const redis = makeFakeRedis();
		const entries = rankedEntries(20, 3);
		// Fail the vector update for the top entry that would be promoted to T1.
		const failIds = new Set(["ke_019"]);
		const vector = makeFakeVector({ failIds });

		const summary = await applyPercentileRetier(redis as any, vector as any, entries as any);

		expect(summary.failed).toBe(1);
		// 8 would change; 1 failed -> 7 succeeded.
		expect(summary.changed).toBe(7);
	});

	it("returns a no-op summary on an empty corpus", async () => {
		const redis = makeFakeRedis();
		const vector = makeFakeVector();
		const summary = await applyPercentileRetier(redis as any, vector as any, []);
		expect(summary).toMatchObject({ evaluated: 0, changed: 0, failed: 0, cap_hit: false });
	});
});
