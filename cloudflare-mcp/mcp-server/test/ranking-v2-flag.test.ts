// =============================================================================
// SCRIPT NAME: ranking-v2-flag.test.ts
// =============================================================================
// DESCRIPTION:
// Vitest unit tests for the RANKING_V2 flag wiring in
// cloudflare-mcp/mcp-server/src/index.ts's search tool handler (contract
// PKS-INJECTION-RANKING-002, Phase B). The search handler's flag branch is
// extracted into the pure, exported function selectSearchTopResults
// (src/index.ts, next to selectReconsolidationTargets) specifically so it is
// testable without standing up the full MCP Durable Object / HTTP transport
// (matching this repo's existing pattern: see
// test/reconsolidation-usage-signal.test.ts, which tests
// selectReconsolidationTargets the same way instead of mocking a full search
// request). This test exercises that pure function directly, plus
// getEffectiveMode (src/tripwires.ts) against an in-memory fake Redis
// (matching test/tripwires.test.ts's makeFakeRedis pattern) to prove the
// RANKING_V2 env-var/kill-flag resolution used to compute the boolean this
// function is called with.
//
// Covers INV1 (shadow phase / cutover flag is write-isolated for the
// ranking-selection path): with the flag off, selectSearchTopResults must be
// byte-identical to the pre-existing "sort, slice top-K" behavior regardless
// of whether shadow salience_v2 fields exist on the candidate objects — this
// is checked by construction (the off branch is one line, unconditionally
// ignoring MMR inputs) and by an explicit before/after-shaped comparison
// below.
//
// INPUT FILES: None. All fixtures are constructed in-memory; no file I/O.
// OUTPUT FILES: None. vitest reports to stdout only.
// =============================================================================

import { describe, expect, it } from "vitest";

import { selectSearchTopResults, type SearchRankingCandidate } from "../src/index";
import { getEffectiveMode } from "../src/tripwires";

function makeFakeRedis() {
	const store = new Map<string, string>();
	return {
		store,
		async get(k: string) {
			const v = store.get(k);
			return v === undefined ? null : v;
		},
		async set(k: string, v: string) {
			store.set(k, v);
			return "OK";
		},
		async del(k: string) {
			const had = store.has(k);
			store.delete(k);
			return had ? 1 : 0;
		},
	};
}

interface TestResult extends SearchRankingCandidate {
	extra: string;
}

function makeResults(): TestResult[] {
	return [
		{ id: "r1", final_score: 0.9, topic_bucket: "d1", label: "one", summary: "summary one", extra: "a" },
		{ id: "r2", final_score: 0.8, topic_bucket: "d1", label: "two", summary: "summary two", extra: "b" },
		{ id: "r3", final_score: 0.7, topic_bucket: "d2", label: "three", summary: "summary three", extra: "c" },
		{ id: "r4", final_score: 0.6, topic_bucket: "d2", label: "four", summary: "summary four", extra: "d" },
		{ id: "r5", final_score: 0.5, topic_bucket: "d3", label: "five", summary: "summary five", extra: "e" },
	];
}

describe("INV1 — RANKING_V2 flag off: byte-identical to the pre-existing plain slice", () => {
	it("returns exactly filteredResults.slice(0, requestedLimit) when disabled", () => {
		const results = makeResults();
		const embeddingById = new Map<string, number[]>(); // empty — simulates a pre-usage-loop / no-embedding state
		const selected = selectSearchTopResults(results, 3, false, embeddingById);
		expect(selected).toEqual(results.slice(0, 3));
	});

	it("ignores embeddingById entirely when disabled — a populated (shadow-pass-shaped) map changes nothing", () => {
		const results = makeResults();
		const emptyEmbeddings = new Map<string, number[]>();
		const populatedEmbeddings = new Map<string, number[]>(
			results.map((r) => [r.id, [Math.random(), Math.random()]]),
		);
		const withEmpty = selectSearchTopResults(results, 5, false, emptyEmbeddings);
		const withPopulated = selectSearchTopResults(results, 5, false, populatedEmbeddings);
		expect(withEmpty).toEqual(withPopulated);
		expect(withEmpty).toEqual(results.slice(0, 5));
	});

	it("is a strict prefix in original order, never reordering or dropping mid-list results", () => {
		const results = makeResults();
		const selected = selectSearchTopResults(results, results.length, false, new Map());
		expect(selected.map((r) => r.id)).toEqual(results.map((r) => r.id));
	});
});

describe("RANKING_V2 flag on: MMR wiring changes selection", () => {
	it("can select a different set/order than the plain slice when embeddings are near-duplicate", () => {
		// b duplicates a's embedding; its raw score (0.91) is high enough to
		// beat c (0.85) under plain sort-and-slice, but not high enough to
		// survive MMR's -0.30*cosine penalty against a (0.91 - 0.30*1 = 0.61
		// < 0.85 - 0.30*0 = 0.85), so pick #2 flips from b to c under MMR.
		const results: TestResult[] = [
			{ id: "a", final_score: 0.99, topic_bucket: "d1", label: "a", summary: "same content", extra: "x" },
			{ id: "b", final_score: 0.91, topic_bucket: "d1", label: "b", summary: "same content", extra: "x" },
			{ id: "c", final_score: 0.85, topic_bucket: "d2", label: "c", summary: "different content", extra: "x" },
		];
		const embeddingById = new Map<string, number[]>([
			["a", [1, 0, 0]],
			["b", [1, 0, 0]], // near-duplicate of a
			["c", [0, 1, 0]],
		]);
		const offSelection = selectSearchTopResults(results, 2, false, embeddingById);
		const onSelection = selectSearchTopResults(results, 2, true, embeddingById);
		expect(offSelection.map((r) => r.id)).toEqual(["a", "b"]); // plain slice: top 2 by score
		// MMR should prefer diversity for pick #2 given b duplicates a's embedding.
		expect(onSelection[0]?.id).toBe("a"); // INV4: pick #1 always the pure best match
		expect(onSelection.map((r) => r.id)).not.toEqual(offSelection.map((r) => r.id));
	});

	it("still returns pick #1 = argmax(final_score) even with the flag on (INV4, via the wiring)", () => {
		const results = makeResults();
		const embeddingById = new Map<string, number[]>(results.map((r) => [r.id, [1, 0]]));
		const selected = selectSearchTopResults(results, 3, true, embeddingById);
		expect(selected[0]?.id).toBe("r1");
	});
});

describe("RANKING_V2 effective-mode resolution (env var + kill flag, off wins)", () => {
	it("is off when the env var is unset (the production default today)", async () => {
		const redis = makeFakeRedis();
		const effective = await getEffectiveMode(redis as any, undefined, "RANKING_V2");
		expect(effective.effective).toBe("off");
	});

	it("is on when the env var is 'on' and no kill flag is set", async () => {
		const redis = makeFakeRedis();
		const effective = await getEffectiveMode(redis as any, "on", "RANKING_V2");
		expect(effective.effective).toBe("on");
	});

	it("falls back to off when a kill flag is set even if the env var is 'on'", async () => {
		const redis = makeFakeRedis();
		await redis.set("tripwire:kill:RANKING_V2", JSON.stringify({ reason: "test trip", at: "2026-07-10T00:00:00Z" }));
		const effective = await getEffectiveMode(redis as any, "on", "RANKING_V2");
		expect(effective.effective).toBe("off");
		expect(effective.tripped).toBe(true);
	});
});
