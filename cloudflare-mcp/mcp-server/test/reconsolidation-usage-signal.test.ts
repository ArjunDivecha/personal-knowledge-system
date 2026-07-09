/**
 * =============================================================================
 * SCRIPT NAME: reconsolidation-usage-signal.test.ts
 * =============================================================================
 *
 * INPUT FILES: none — all fixtures are constructed in-memory; no file I/O.
 * OUTPUT FILES: none — vitest reports to stdout only; no file I/O.
 *
 * VERSION: 1.0   LAST UPDATED: 2026-07-09   AUTHOR: Claude (Fable 5) for Arjun Divecha
 *
 * DESCRIPTION:
 * Codifies the usage-signal (reconsolidation) semantics of the production MCP
 * Worker so they cannot regress silently (contract PKS-USAGE-SIGNAL-001 in
 * /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/contracts/usage-signal-loop.spec.md).
 *
 * The mechanism under test has existed since 2026-03-27 and was deliberately
 * tightened on 2026-06-09 (commit 34c590b, "exposure != use"): only the rank-1
 * search result earns an access-signal write, because top-5 triggers were
 * granting every search result permanent archive immunity regardless of
 * relevance. These tests pin that decision, the minimal-write behavior of
 * applyAccessSignals, the new synthetic-traffic suppression flag, and the
 * salience ordering that makes the signal meaningful (retrievalBoost).
 *
 * DEPENDENCIES: vitest; ../src/index (exported helpers); ../src/salience.
 * USAGE: npm run test:worker   (or: npx vitest run test/reconsolidation-usage-signal.test.ts)
 * =============================================================================
 */
import { describe, expect, it } from "vitest";

import {
	applyAccessSignals,
	MAX_RECONSOLIDATION_SEARCH_RESULTS,
	selectReconsolidationTargets,
} from "../src/index";
import { computeSalience } from "../src/salience";

describe("exposure != use: the rank-1 cap (3.3, commit 34c590b)", () => {
	it("pins MAX_RECONSOLIDATION_SEARCH_RESULTS at 1 — raising it re-opens the archive-immunity bug and must be a deliberate, reviewed change", () => {
		expect(MAX_RECONSOLIDATION_SEARCH_RESULTS).toBe(1);
	});

	it("selects only the rank-1 result from a full result page", () => {
		const results = [{ id: "a" }, { id: "b" }, { id: "c" }, { id: "d" }, { id: "e" }];
		expect(selectReconsolidationTargets(results, undefined)).toEqual([{ id: "a" }]);
		expect(selectReconsolidationTargets(results, false)).toEqual([{ id: "a" }]);
	});

	it("selects nothing from an empty result page", () => {
		expect(selectReconsolidationTargets([], undefined)).toEqual([]);
	});
});

describe("synthetic traffic never reinforces (suppress_access_signals)", () => {
	it("returns zero targets when suppression is requested, regardless of results", () => {
		const results = [{ id: "a" }, { id: "b" }];
		expect(selectReconsolidationTargets(results, true)).toEqual([]);
		expect(selectReconsolidationTargets([], true)).toEqual([]);
	});
});

describe("applyAccessSignals writes are minimal and monotonic", () => {
	const baseEntry = () => ({
		id: "ke_test",
		confidence: "medium",
		state: "active",
		current_view: "unchanged view",
		metadata: {
			context_type: "task_query",
			mention_count: 3,
			last_seen: "2026-07-01T00:00:00Z",
			updated_at: "2026-07-01T00:00:00Z",
			access_count: 2,
			last_accessed: "2026-06-01T00:00:00Z",
			injection_tier: 2,
			github_repo: "ArjunDivecha/personal-knowledge-system",
		},
	});

	it("touches only access_count, last_accessed, and salience_score; all other fields survive byte-identical", () => {
		const entry = baseEntry();
		const before = JSON.parse(JSON.stringify(entry));
		applyAccessSignals(entry, 5, "2026-07-09T00:00:00Z");
		const metadata = entry.metadata as Record<string, unknown>;

		expect(metadata.access_count).toBe(5);
		expect(metadata.last_accessed).toBe("2026-07-09T00:00:00Z");
		expect(typeof (metadata as { salience_score?: unknown }).salience_score).toBe("number");

		// Everything else is untouched.
		const beforeMeta = before.metadata as Record<string, unknown>;
		for (const key of Object.keys(beforeMeta)) {
			if (key === "access_count" || key === "last_accessed" || key === "salience_score") continue;
			expect(metadata[key]).toEqual(beforeMeta[key]);
		}
		expect(entry.current_view).toBe(before.current_view);
		expect(entry.state).toBe(before.state);
		expect(entry.confidence).toBe(before.confidence);
	});

	it("access_count merges by max(stored, side-key) — a lagging side key never rolls the count backward", () => {
		const entry = baseEntry();
		applyAccessSignals(entry, 1, "2026-07-09T00:00:00Z"); // side key (1) < stored (2)
		expect((entry.metadata as Record<string, unknown>).access_count).toBe(2);
	});

	it("last_accessed keeps the latest of stored vs side-key — a stale side key never rewinds recency", () => {
		const entry = baseEntry();
		applyAccessSignals(entry, 5, "2026-05-01T00:00:00Z"); // side key older than stored 2026-06-01
		expect((entry.metadata as Record<string, unknown>).last_accessed).toBe("2026-06-01T00:00:00Z");
	});
});

describe("the signal is meaningful: computeSalience consumes last_accessed (retrievalBoost)", () => {
	const now = new Date("2026-07-09T00:00:00Z");
	const entryWithAccess = (lastAccessed: string | null) => ({
		confidence: "medium",
		metadata: {
			context_type: "task_query",
			mention_count: 2,
			last_seen: "2026-06-25T00:00:00Z",
			updated_at: "2026-06-25T00:00:00Z",
			last_accessed: lastAccessed,
		},
	});

	it("an entry accessed today scores strictly above an otherwise-identical entry never accessed", () => {
		const fresh = computeSalience(entryWithAccess("2026-07-09T00:00:00Z"), now);
		const never = computeSalience(entryWithAccess(null), now);
		expect(fresh).toBeGreaterThan(never);
	});

	it("the boost decays: accessed-today > accessed-45-days-ago > never-accessed", () => {
		const fresh = computeSalience(entryWithAccess("2026-07-09T00:00:00Z"), now);
		const stale = computeSalience(entryWithAccess("2026-05-25T00:00:00Z"), now);
		const never = computeSalience(entryWithAccess(null), now);
		expect(fresh).toBeGreaterThan(stale);
		expect(stale).toBeGreaterThan(never);
	});
});
