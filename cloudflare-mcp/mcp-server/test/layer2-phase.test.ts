// Tests for applyLayer2QuarantineAndDemote — the phase that runs inside
// runDreamCycle to enact synaptic-weakening (quarantine → demote) on entries
// whose salience has dropped for a sustained streak of nights.
//
// Uses in-memory fake Redis/Vector — verifies behavioral correctness of the
// streak/threshold/cap rules; does not exercise the real persist path.

import { describe, expect, it } from "vitest";

import {
	LAYER2_QUARANTINE_AFTER_NIGHTS,
	LAYER2_DEMOTE_AFTER_NIGHTS,
	LAYER2_PER_RUN_CAP,
	applyLayer2QuarantineAndDemote,
} from "../src/dream";

// Minimal fakes that match the StorageClient surface used inside persistEntry.
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

function makeFakeVector() {
	const upserts: any[] = [];
	const updates: any[] = [];
	return {
		upserts,
		updates,
		async upsert(arg: any) {
			upserts.push(arg);
			return { upserted: 1 };
		},
		async update(arg: any) {
			updates.push(arg);
			return { updated: 1 };
		},
	};
}

interface FakeEntry {
	id: string;
	type: "knowledge";
	entry: Record<string, unknown>;
	metadata: Record<string, unknown>;
	label: string;
	updatedAt: string | null;
	contextType: string;
	injectionTier: 1 | 2 | 3;
	mentionCount: number;
	accessCount: number;
	sourceConversationCount: number;
	salienceScore: number;
}

function makeEntry(args: {
	id: string;
	tier: 1 | 2 | 3;
	salience: number;
	streakNights?: number;
	quarantined?: boolean;
}): FakeEntry {
	const metadata: Record<string, unknown> = {
		injection_tier: args.tier,
		salience_score: args.salience,
		quarantine_streak_nights: args.streakNights ?? 0,
		injection_quarantine: Boolean(args.quarantined),
		context_type: "recurring_pattern",
		mention_count: 2,
		access_count: 0,
		updated_at: "2026-05-01T00:00:00.000Z",
	};
	const entry: Record<string, unknown> = {
		id: args.id,
		type: "knowledge",
		domain: `domain ${args.id}`,
		current_view: "view",
		confidence: "medium",
		metadata,
	};
	return {
		id: args.id,
		type: "knowledge",
		entry,
		metadata,
		label: args.id,
		updatedAt: "2026-05-01T00:00:00.000Z",
		contextType: "recurring_pattern",
		injectionTier: args.tier,
		mentionCount: 2,
		accessCount: 0,
		sourceConversationCount: 1,
		salienceScore: args.salience,
	};
}

const TS = "2026-05-17T07:10:46.784Z";

describe("applyLayer2QuarantineAndDemote", () => {
	it("quarantines a tier-2 entry on its 3rd consecutive night below threshold", async () => {
		const redis = makeFakeRedis();
		const vector = makeFakeVector();
		const entry = makeEntry({ id: "ke_a", tier: 2, salience: 0.10, streakNights: 2 });

		const summary = await applyLayer2QuarantineAndDemote(redis as any, vector as any, [entry as any], TS);

		expect(summary.quarantined).toHaveLength(1);
		expect(summary.demoted).toHaveLength(0);
		expect(entry.metadata.injection_quarantine).toBe(true);
		expect(entry.metadata.quarantine_streak_nights).toBe(LAYER2_QUARANTINE_AFTER_NIGHTS);
		// Tier is NOT changed yet.
		expect(entry.metadata.injection_tier).toBe(2);
	});

	it("does not quarantine before the streak threshold", async () => {
		const redis = makeFakeRedis();
		const vector = makeFakeVector();
		const entry = makeEntry({ id: "ke_b", tier: 2, salience: 0.05, streakNights: 0 });

		const summary = await applyLayer2QuarantineAndDemote(redis as any, vector as any, [entry as any], TS);

		expect(summary.quarantined).toHaveLength(0);
		expect(summary.streak_increment).toBe(1);
		expect(entry.metadata.injection_quarantine).toBe(false);
		expect(entry.metadata.quarantine_streak_nights).toBe(1);
	});

	it("demotes a quarantined tier-2 entry once the streak hits the demote threshold", async () => {
		const redis = makeFakeRedis();
		const vector = makeFakeVector();
		// Entry is quarantined and one night below the demote threshold;
		// this night pushes it over.
		const entry = makeEntry({
			id: "ke_c",
			tier: 2,
			salience: 0.05,
			streakNights: LAYER2_DEMOTE_AFTER_NIGHTS - 1,
			quarantined: true,
		});

		const summary = await applyLayer2QuarantineAndDemote(redis as any, vector as any, [entry as any], TS);

		expect(summary.demoted).toHaveLength(1);
		expect(entry.metadata.injection_tier).toBe(3);
		// Demote clears the quarantine flag and resets streak.
		expect(entry.metadata.injection_quarantine).toBe(false);
		expect(entry.metadata.quarantine_streak_nights).toBe(0);
	});

	it("never demotes a tier-3 entry", async () => {
		const redis = makeFakeRedis();
		const vector = makeFakeVector();
		const entry = makeEntry({
			id: "ke_d",
			tier: 3,
			salience: 0.01,
			streakNights: 999,
			quarantined: true,
		});

		const summary = await applyLayer2QuarantineAndDemote(redis as any, vector as any, [entry as any], TS);

		expect(summary.demoted).toHaveLength(0);
		expect(summary.quarantined).toHaveLength(0);
		// processed counter excludes tier-3 entries (they are skipped before persistence).
		expect(summary.processed).toBe(0);
		expect(entry.metadata.injection_tier).toBe(3);
	});

	it("resets streak when salience recovers above the threshold", async () => {
		const redis = makeFakeRedis();
		const vector = makeFakeVector();
		const entry = makeEntry({
			id: "ke_e",
			tier: 2,
			salience: 0.50,
			streakNights: 5,
		});

		const summary = await applyLayer2QuarantineAndDemote(redis as any, vector as any, [entry as any], TS);

		expect(summary.streak_reset).toBe(1);
		expect(entry.metadata.quarantine_streak_nights).toBe(0);
	});

	it("stops at LAYER2_PER_RUN_CAP", async () => {
		const redis = makeFakeRedis();
		const vector = makeFakeVector();
		const entries = Array.from({ length: LAYER2_PER_RUN_CAP + 50 }, (_, i) =>
			makeEntry({ id: `ke_${i}`, tier: 2, salience: 0.05 }),
		);

		const summary = await applyLayer2QuarantineAndDemote(redis as any, vector as any, entries as any, TS);

		expect(summary.cap_hit).toBe(true);
		expect(summary.processed).toBeLessThanOrEqual(LAYER2_PER_RUN_CAP);
	});
});
