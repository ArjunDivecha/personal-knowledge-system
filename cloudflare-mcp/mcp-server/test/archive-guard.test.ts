// Regression tests for two archive-safety guards in runDreamCycle:
//   [P1] archiveEntry must mark the IN-MEMORY LoadedEntry archived, so later
//        same-run phases (Layer 2, percentile re-tier) that operate on the
//        shared snapshot don't resurrect it.
//   [P1] isArchiveCandidate must never select a contested (review-pending)
//        entry for archival.
//
// In-memory fake Redis/Vector — exercises the behavior, not the real backend.

import { describe, expect, it } from "vitest";

import { archiveEntry, isArchiveCandidate, applyPercentileRetier } from "../src/dream";

function makeFakeRedis() {
	const store = new Map<string, string>();
	return {
		store,
		async get(k: string) {
			return store.has(k) ? store.get(k)! : null;
		},
		async set(k: string, v: string) {
			store.set(k, v);
			return "OK";
		},
		async incr(k: string) {
			const n = Number(store.get(k) ?? "0") + 1;
			store.set(k, String(n));
			return n;
		},
	};
}

function makeFakeVector() {
	const updates: any[] = [];
	const deletes: string[] = [];
	return {
		updates,
		deletes,
		async upsert(arg: any) {
			updates.push(arg);
			return { upserted: 1 };
		},
		async update(arg: any) {
			updates.push(arg);
			return { updated: 1 };
		},
		async delete(ids: string | string[]) {
			(Array.isArray(ids) ? ids : [ids]).forEach((i) => deletes.push(i));
			return { deleted: 1 };
		},
	};
}

function makeEntry(args: {
	id: string;
	state?: string;
	salience?: number;
	tier?: 1 | 2 | 3;
	contextType?: string;
}) {
	const metadata: Record<string, unknown> = {
		context_type: args.contextType ?? "task_query",
		injection_tier: args.tier ?? 3,
		salience_score: args.salience ?? 0.01,
		mention_count: 1,
		access_count: 0,
		confidence: "low",
		updated_at: "2026-05-01T00:00:00.000Z",
		revision: 0,
	};
	const entry: Record<string, unknown> = {
		id: args.id,
		type: "knowledge",
		state: args.state ?? "active",
		domain: `domain ${args.id}`,
		current_view: "view",
		confidence: "low",
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
		injectionTier: args.tier ?? 3,
		mentionCount: 1,
		accessCount: 0,
		sourceConversationCount: 1,
		salienceScore: args.salience ?? 0.01,
	};
}

const TS = "2026-06-01T07:10:00.000Z";

describe("archive safety guards", () => {
	it("[P1] archiveEntry marks the in-memory LoadedEntry archived (fresh-load path)", async () => {
		const redis = makeFakeRedis();
		const vector = makeFakeVector();
		const entry = makeEntry({ id: "ke_arch", tier: 1 });
		// Pre-store so archiveEntry's fresh redis.get returns a DISTINCT object —
		// the case where the original metadata ref would otherwise stay un-marked.
		redis.store.set("knowledge:ke_arch", JSON.stringify(entry.entry));

		expect(entry.metadata.archived).toBeFalsy();
		await archiveEntry(redis as any, vector as any, entry as any, "dr_test", TS, "test archive");

		expect(entry.metadata.archived).toBe(true);
		expect(vector.deletes).toContain("ke_arch"); // its vector was removed
	});

	it("[P1] percentile re-tier excludes an entry archived earlier in the same run", async () => {
		const redis = makeFakeRedis();
		const vector = makeFakeVector();
		const archived = makeEntry({ id: "ke_arch", tier: 1, salience: 0.01 });
		const active = makeEntry({ id: "ke_live", tier: 1, salience: 0.9 });
		redis.store.set("knowledge:ke_arch", JSON.stringify(archived.entry));
		redis.store.set("knowledge:ke_live", JSON.stringify(active.entry));

		await archiveEntry(redis as any, vector as any, archived as any, "dr_test", TS, "test archive");
		vector.updates.length = 0; // ignore archive-path writes; watch only re-tier

		const summary = await applyPercentileRetier(redis as any, vector as any, [archived, active] as any);

		// Only the still-active entry is in the percentile population.
		expect(summary.evaluated).toBe(1);
		// The archived entry must never be re-persisted (no vector write resurrecting it).
		const touchedArchived = vector.updates.some((u) => u.id === "ke_arch");
		expect(touchedArchived).toBe(false);
	});

	it("[P1] isArchiveCandidate skips contested entries", () => {
		const eligible = makeEntry({ id: "ke_a", state: "active", salience: 0.01 });
		const contested = makeEntry({ id: "ke_b", state: "contested", salience: 0.01 });

		// Same low-salience/zero-access shape; only state differs.
		expect(isArchiveCandidate(eligible as any)).toBe(true);
		expect(isArchiveCandidate(contested as any)).toBe(false);
	});
});
