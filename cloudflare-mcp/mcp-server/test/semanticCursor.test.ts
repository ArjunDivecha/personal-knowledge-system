// =============================================================================
// SCRIPT NAME: semanticCursor.test.ts
// =============================================================================
// INPUT FILES:
// - None. All fixtures (a 1,000-id in-memory fake corpus) are constructed
//   in-memory; the fake Redis is an in-memory Map, not a file.
// OUTPUT FILES:
// - None. vitest reports to stdout only.
//
// Covers INV4 of contract PKS-SEMANTIC-CONSOLIDATION-001 (the persistent
// rolling cursor advances monotonically, wraps, and cannot starve any corpus
// region: every active entry id is visited within ceil(corpus_size/200)
// consecutive nights) and the resume-after-crash guarantee the cursor's
// design leans on (a run that crashes before calling advanceSemanticCursor
// must repeat, not skip, that night's slice).
// =============================================================================

import { describe, expect, it } from "vitest";

import {
	advanceSemanticCursor,
	loadSemanticCursor,
	selectCursorSlice,
	type SemanticCursorState,
} from "../src/semanticCursor";

// Minimal in-memory fake matching the subset of the Upstash Redis client
// surface (get/set) that semanticCursor.ts actually calls.
function makeFakeRedis() {
	const store = new Map<string, string>();
	return {
		store,
		async get(key: string) {
			return store.has(key) ? store.get(key)! : null;
		},
		async set(key: string, value: string) {
			store.set(key, value);
			return "OK";
		},
	} as unknown as import("@upstash/redis/cloudflare").Redis;
}

function makeCorpus(size: number): Array<{ id: string }> {
	return Array.from({ length: size }, (_, i) => ({ id: `ke_${String(i).padStart(4, "0")}` }));
}

describe("loadSemanticCursor", () => {
	it("defaults to position 0 on a fresh (unset) key", async () => {
		const redis = makeFakeRedis();
		const state = await loadSemanticCursor(redis);
		expect(state.position).toBe(0);
		expect(state.total_swept_this_cycle).toBe(0);
		expect(typeof state.cycle_started_at).toBe("string");
	});

	it("round-trips a persisted state written by advanceSemanticCursor", async () => {
		const redis = makeFakeRedis();
		const initial = await loadSemanticCursor(redis);
		await advanceSemanticCursor(redis, initial, 200, 1000);
		const reloaded = await loadSemanticCursor(redis);
		expect(reloaded.position).toBe(200);
	});

	it("falls back to a fresh cursor on malformed persisted JSON", async () => {
		const redis = makeFakeRedis();
		redis.store.set("dream:semantic_cursor", "{not valid json");
		const state = await loadSemanticCursor(redis);
		expect(state.position).toBe(0);
	});
});

describe("selectCursorSlice", () => {
	it("returns a contiguous non-wrapping slice when it fits before the end", () => {
		const corpus = makeCorpus(300);
		const slice = selectCursorSlice(corpus, 0, 200);
		expect(slice.map((e) => e.id)).toEqual(corpus.slice(0, 200).map((e) => e.id));
	});

	it("wraps to the start of the corpus per the contract's worked example (position=180, size=200, corpus=300)", () => {
		const corpus = makeCorpus(300);
		const slice = selectCursorSlice(corpus, 180, 200);
		const expectedIds = [
			...corpus.slice(180, 300).map((e) => e.id),
			...corpus.slice(0, 80).map((e) => e.id),
		];
		expect(slice.map((e) => e.id)).toEqual(expectedIds);
	});

	it("caps the slice at the corpus size when sliceSize exceeds it", () => {
		const corpus = makeCorpus(50);
		const slice = selectCursorSlice(corpus, 0, 200);
		expect(slice).toHaveLength(50);
	});

	it("returns an empty slice for an empty corpus", () => {
		expect(selectCursorSlice([], 0, 200)).toEqual([]);
	});

	it("returns the identical slice when called twice at the same position (interrupted-run resume)", () => {
		const corpus = makeCorpus(1000);
		const first = selectCursorSlice(corpus, 733, 200);
		const second = selectCursorSlice(corpus, 733, 200);
		expect(second).toEqual(first);
	});
});

describe("advanceSemanticCursor", () => {
	it("advances position by sweptCount without wrapping", async () => {
		const redis = makeFakeRedis();
		const state: SemanticCursorState = { position: 100, cycle_started_at: "2026-01-01T00:00:00.000Z", total_swept_this_cycle: 100 };
		const next = await advanceSemanticCursor(redis, state, 200, 1000);
		expect(next.position).toBe(300);
		expect(next.cycle_started_at).toBe("2026-01-01T00:00:00.000Z");
		expect(next.total_swept_this_cycle).toBe(300);
	});

	it("wraps and resets cycle bookkeeping when the new position is <= the old position", async () => {
		const redis = makeFakeRedis();
		const state: SemanticCursorState = { position: 900, cycle_started_at: "2026-01-01T00:00:00.000Z", total_swept_this_cycle: 900 };
		const next = await advanceSemanticCursor(redis, state, 200, 1000);
		// (900 + 200) % 1000 = 100, which is <= 900 -> wrapped.
		expect(next.position).toBe(100);
		expect(next.total_swept_this_cycle).toBe(200);
		expect(next.cycle_started_at).not.toBe("2026-01-01T00:00:00.000Z");
	});

	it("treats an empty corpus as an immediate wrap to position 0", async () => {
		const redis = makeFakeRedis();
		const state: SemanticCursorState = { position: 5, cycle_started_at: "2026-01-01T00:00:00.000Z", total_swept_this_cycle: 5 };
		const next = await advanceSemanticCursor(redis, state, 0, 0);
		expect(next.position).toBe(0);
	});
});

describe("full-coverage simulation over a 1,000-entry fixture corpus (INV4)", () => {
	it("visits every id within ceil(1000/200) = 5 simulated nightly 200-entry slices", async () => {
		const redis = makeFakeRedis();
		const corpus = makeCorpus(1000);
		const visited = new Set<string>();
		let cursor = await loadSemanticCursor(redis);

		const nightsNeeded = Math.ceil(corpus.length / 200);
		expect(nightsNeeded).toBe(5);

		for (let night = 0; night < nightsNeeded; night += 1) {
			const slice = selectCursorSlice(corpus, cursor.position, 200);
			for (const entry of slice) visited.add(entry.id);
			cursor = await advanceSemanticCursor(redis, cursor, slice.length, corpus.length);
		}

		expect(visited.size).toBe(1000);
		for (const entry of corpus) expect(visited.has(entry.id)).toBe(true);
		// 1000 is an exact multiple of 200, so 5 nights lands exactly back at 0
		// (a fresh wrap) — the cycle should have reset on the final night.
		expect(cursor.position).toBe(0);
	});

	it("still achieves full coverage within ceil(corpus/200) nights when the corpus size is not an exact multiple of 200", async () => {
		const redis = makeFakeRedis();
		const corpus = makeCorpus(950);
		const visited = new Set<string>();
		let cursor = await loadSemanticCursor(redis);

		const nightsNeeded = Math.ceil(corpus.length / 200);
		expect(nightsNeeded).toBe(5);

		for (let night = 0; night < nightsNeeded; night += 1) {
			const slice = selectCursorSlice(corpus, cursor.position, 200);
			for (const entry of slice) visited.add(entry.id);
			cursor = await advanceSemanticCursor(redis, cursor, slice.length, corpus.length);
		}

		expect(visited.size).toBe(950);
	});

	it("resumes mid-slice after a simulated crash: not calling advanceSemanticCursor repeats the same slice next time, never skipping ahead", async () => {
		const redis = makeFakeRedis();
		const corpus = makeCorpus(1000);
		let cursor = await loadSemanticCursor(redis);

		// Night 1 completes normally.
		const night1Slice = selectCursorSlice(corpus, cursor.position, 200);
		cursor = await advanceSemanticCursor(redis, cursor, night1Slice.length, corpus.length);
		expect(cursor.position).toBe(200);

		// Night 2 "crashes" after computing the slice but before persisting the
		// advance — simulate by reloading the cursor from Redis (as the next
		// invocation would) without ever calling advanceSemanticCursor.
		const night2SliceAttempt1 = selectCursorSlice(corpus, cursor.position, 200);
		const reloadedAfterCrash = await loadSemanticCursor(redis);
		expect(reloadedAfterCrash.position).toBe(200); // unchanged by the crashed attempt

		const night2SliceAttempt2 = selectCursorSlice(corpus, reloadedAfterCrash.position, 200);
		expect(night2SliceAttempt2.map((e) => e.id)).toEqual(night2SliceAttempt1.map((e) => e.id));
	});

	// Regression: adversarial review 2026-07-11 claimed a persisted position
	// from a LARGER prior corpus (e.g. 900) against a corpus that has since
	// SHRUNK (e.g. to 500, after archiving/merging) would skip indices 0-99
	// before "wrap bookkeeping" kicks in. Empirically false — selectCursorSlice
	// always wraps modulo the CURRENT corpus size within a single call, and
	// advanceSemanticCursor's (position + swept) % total uses the same modular
	// base, so the two stay self-consistent even when totalCorpusSize differs
	// night to night. These tests prove it rather than merely arguing it.
	it("a stale position from a larger prior corpus does not skip entries when the corpus has since shrunk", () => {
		const shrunkCorpus = makeCorpus(500);
		const staleHighPosition = 900; // valid position for a corpus that used to be >900 entries
		const slice = selectCursorSlice(shrunkCorpus, staleHighPosition, 200);
		const ids = new Set(slice.map((e) => e.id));
		expect(slice.length).toBe(200);
		// 900 % 500 = 400 -> covers [400..499] then wraps to [0..99]; nothing in
		// that logical window is skipped.
		for (let i = 0; i < 100; i += 1) expect(ids.has(`ke_${String(i).padStart(4, "0")}`)).toBe(true);
		for (let i = 400; i < 500; i += 1) expect(ids.has(`ke_${String(i).padStart(4, "0")}`)).toBe(true);
	});

	it("full coverage still holds across simulated nights even when the corpus shrinks partway through a cycle", async () => {
		const redis = makeFakeRedis();
		let corpus = makeCorpus(1000);
		let cursor = await loadSemanticCursor(redis);
		const visited = new Set<string>();

		// Nights 1-2 at full size (1000), then the corpus shrinks to 700
		// (200 entries archived/merged away) for the remaining nights.
		for (let night = 0; night < 2; night += 1) {
			const slice = selectCursorSlice(corpus, cursor.position, 200);
			for (const entry of slice) visited.add(entry.id);
			cursor = await advanceSemanticCursor(redis, cursor, slice.length, corpus.length);
		}
		corpus = makeCorpus(700);
		// advanceSemanticCursor's own modular arithmetic re-bases the position
		// against the new total on the very next call, so no special "corpus
		// changed size" handling is needed by the caller beyond passing the
		// current corpus.length each night (which the real wiring already does
		// by re-loading entries fresh every run).
		for (let night = 0; night < Math.ceil(700 / 200); night += 1) {
			const slice = selectCursorSlice(corpus, cursor.position, 200);
			for (const entry of slice) visited.add(entry.id);
			cursor = await advanceSemanticCursor(redis, cursor, slice.length, corpus.length);
		}
		for (let i = 0; i < 700; i += 1) {
			expect(visited.has(`ke_${String(i).padStart(4, "0")}`)).toBe(true);
		}
	});
});
