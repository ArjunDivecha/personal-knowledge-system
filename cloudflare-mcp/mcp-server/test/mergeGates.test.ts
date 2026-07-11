// =============================================================================
// SCRIPT NAME: mergeGates.test.ts
// =============================================================================
// INPUT FILES:
// - None. All fixtures are constructed in-memory; no file I/O.
// OUTPUT FILES:
// - None. vitest reports to stdout only.
//
// Covers INV3 and INV5 of contract PKS-SEMANTIC-CONSOLIDATION-001's core
// safety mechanism: collapseNearDuplicateInsights (deterministic near-dup
// collapse with a drop-to-retained receipt mapping) and
// validateMergeConservation (the independent post-hoc hard gate that must
// block a merge apply when evidence or metadata conservation is violated).
// =============================================================================

import { describe, expect, it } from "vitest";

import {
	collapseNearDuplicateInsights,
	insightTextSimilarity,
	isProtectedContextType,
	validateMergeConservation,
} from "../src/mergeGates";

describe("insightTextSimilarity", () => {
	it("is 1.0 for identical text", () => {
		expect(insightTextSimilarity("Arjun prefers conventional commits", "Arjun prefers conventional commits")).toBe(1);
	});

	it("is high for near-identical paraphrases", () => {
		const sim = insightTextSimilarity(
			"Arjun prefers conventional commit messages in this repo",
			"Arjun prefers using conventional commits in this repo",
		);
		expect(sim).toBeGreaterThanOrEqual(0.6);
	});

	it("is 0 for disjoint text", () => {
		expect(insightTextSimilarity("Arjun uses Python for quant research", "The weather is nice today")).toBe(0);
	});

	it("is 0 when either side is empty or non-string", () => {
		expect(insightTextSimilarity("", "something")).toBe(0);
		expect(insightTextSimilarity(null, "something")).toBe(0);
		expect(insightTextSimilarity(undefined, undefined)).toBe(0);
	});

	it("is symmetric", () => {
		const a = "Arjun runs quant backtests on country ETFs";
		const b = "Arjun backtests quant strategies on country ETFs";
		expect(insightTextSimilarity(a, b)).toBe(insightTextSimilarity(b, a));
	});
});

describe("collapseNearDuplicateInsights", () => {
	it("keeps a single insight unchanged", () => {
		const insights = [{ insight: "Arjun prefers conventional commits" }];
		const result = collapseNearDuplicateInsights(insights);
		expect(result.kept).toEqual(insights);
		expect(result.drops).toEqual([]);
	});

	it("collapses near-duplicate paraphrases and maps drop to the first-seen retained insight", () => {
		const insights = [
			{ insight: "Arjun prefers conventional commit messages" },
			{ insight: "Arjun prefers conventional commit style" },
			{ insight: "Arjun really prefers conventional commit messages" },
		];
		const result = collapseNearDuplicateInsights(insights, 0.5);
		expect(result.kept.length).toBe(1);
		expect(result.kept[0]).toEqual(insights[0]);
		expect(result.drops.length).toBe(2);
		expect(result.drops[0]).toMatchObject({ droppedIndex: 1, retainedIndex: 0 });
		expect(result.drops[1]).toMatchObject({ droppedIndex: 2, retainedIndex: 0 });
	});

	it("keeps distinct insights separate below the threshold", () => {
		const insights = [
			{ insight: "Arjun prefers conventional commits" },
			{ insight: "Arjun works on quant investment research" },
		];
		const result = collapseNearDuplicateInsights(insights, 0.85);
		expect(result.kept.length).toBe(2);
		expect(result.drops).toEqual([]);
	});

	it("never mutates the input array", () => {
		const insights = [
			{ insight: "Arjun prefers conventional commits" },
			{ insight: "Arjun prefers conventional commits exactly" },
		];
		const before = JSON.stringify(insights);
		collapseNearDuplicateInsights(insights, 0.5);
		expect(JSON.stringify(insights)).toBe(before);
	});

	it("respects a stricter threshold to avoid over-collapsing", () => {
		const insights = [
			{ insight: "Arjun runs backtests on the country ETF universe" },
			{ insight: "Arjun manages a portfolio of country ETFs" },
		];
		const result = collapseNearDuplicateInsights(insights, 0.95);
		expect(result.kept.length).toBe(2);
	});
});

const emptyMetadata = () => ({
	source_conversations: [] as string[],
	source_messages: [] as string[],
	first_seen: "2026-01-01T00:00:00Z",
	last_seen: "2026-01-01T00:00:00Z",
	mention_count: 1,
});

describe("validateMergeConservation — clean and subset merges pass", () => {
	it("passes a clean two-parent merge with correctly unioned metadata", () => {
		const parentA = {
			entry: { key_insights: [{ insight: "A", evidence: { conversation_id: "c1", message_ids: ["m1"], snippet: "s1" } }] },
			metadata: { ...emptyMetadata(), source_conversations: ["c1"], source_messages: ["m1"], first_seen: "2026-01-01T00:00:00Z", last_seen: "2026-01-05T00:00:00Z", mention_count: 1 },
		};
		const parentB = {
			entry: { key_insights: [{ insight: "B", evidence: { conversation_id: "c2", message_ids: ["m2"], snippet: "s2" } }] },
			metadata: { ...emptyMetadata(), source_conversations: ["c2"], source_messages: ["m2"], first_seen: "2026-01-03T00:00:00Z", last_seen: "2026-01-10T00:00:00Z", mention_count: 1 },
		};
		const merged = {
			entry: { key_insights: [...parentA.entry.key_insights, ...parentB.entry.key_insights] },
			metadata: { source_conversations: ["c1", "c2"], source_messages: ["m1", "m2"], first_seen: "2026-01-01T00:00:00Z", last_seen: "2026-01-10T00:00:00Z", mention_count: 2 },
		};
		const result = validateMergeConservation({ parents: [parentA, parentB], merged, insightDrops: [] });
		expect(result.ok).toBe(true);
		expect(result.violations).toEqual([]);
	});

	it("passes a merge with a receipted near-duplicate insight drop (evidence forgiven by the drop mapping)", () => {
		const parentA = {
			entry: { key_insights: [{ insight: "Arjun prefers conventional commits", evidence: { conversation_id: "c1", message_ids: ["m1"], snippet: "s1" } }] },
			metadata: { ...emptyMetadata(), source_conversations: ["c1"], source_messages: ["m1"] },
		};
		const parentB = {
			entry: { key_insights: [{ insight: "Arjun likes conventional commit style", evidence: { conversation_id: "c2", message_ids: ["m2"], snippet: "s2" } }] },
			metadata: { ...emptyMetadata(), source_conversations: ["c2"], source_messages: ["m2"] },
		};
		// The merged entry only kept parentA's insight (parentB's was collapsed as a near-duplicate) —
		// so parentB's evidence key is legitimately absent from `merged`.
		const merged = {
			entry: { key_insights: [parentA.entry.key_insights[0]] },
			metadata: { source_conversations: ["c1", "c2"], source_messages: ["m1", "m2"], first_seen: "2026-01-01T00:00:00Z", last_seen: "2026-01-01T00:00:00Z", mention_count: 2 },
		};
		const drops = [{
			droppedIndex: 1, retainedIndex: 0,
			droppedInsight: "Arjun likes conventional commit style", retainedInsight: "Arjun prefers conventional commits",
			similarity: 0.6, droppedEvidenceKey: "c2::m2::s2",
		}];
		const result = validateMergeConservation({ parents: [parentA, parentB], merged, insightDrops: drops });
		expect(result.ok).toBe(true);
	});

	it("blocks when a missing evidence key does NOT match the receipted drop's own evidence key, even if the counts happen to line up (regression: adversarial review 2026-07-11 — count-based forgiveness could mask an unrelated real loss)", () => {
		const parentA = {
			entry: { key_insights: [
				{ insight: "Arjun prefers conventional commits", evidence: { conversation_id: "c1", message_ids: ["m1"], snippet: "s1" } },
				{ insight: "Arjun uses Python for backtests", evidence: { conversation_id: "c3", message_ids: ["m3"], snippet: "s3" } },
			] },
			metadata: { ...emptyMetadata(), source_conversations: ["c1", "c3"], source_messages: ["m1", "m3"] },
		};
		const parentB = {
			entry: { key_insights: [{ insight: "Arjun likes conventional commit style", evidence: { conversation_id: "c2", message_ids: ["m2"], snippet: "s2" } }] },
			metadata: { ...emptyMetadata(), source_conversations: ["c2"], source_messages: ["m2"] },
		};
		// The merge legitimately collapses parentB's near-duplicate (c2::m2::s2) —
		// BUT ALSO silently drops parentA's unrelated second insight (c3::m3::s3),
		// which no drop mapping explains. A count-based check (1 missing <= 1 drop)
		// would wrongly forgive this; the exact-identity check must not.
		const merged = {
			entry: { key_insights: [parentA.entry.key_insights[0]] },
			metadata: { source_conversations: ["c1", "c2", "c3"], source_messages: ["m1", "m2", "m3"], first_seen: "2026-01-01T00:00:00Z", last_seen: "2026-01-01T00:00:00Z", mention_count: 3 },
		};
		const drops = [{
			droppedIndex: 1, retainedIndex: 0,
			droppedInsight: "Arjun likes conventional commit style", retainedInsight: "Arjun prefers conventional commits",
			similarity: 0.6, droppedEvidenceKey: "c2::m2::s2",
		}];
		const result = validateMergeConservation({ parents: [parentA, parentB], merged, insightDrops: drops });
		expect(result.ok).toBe(false);
		expect(result.violations.some((v) => v.startsWith("evidence_conservation"))).toBe(true);
	});
});

describe("validateMergeConservation — lossy and incorrect merges are blocked", () => {
	it("blocks when an evidence entry disappears without a receipted drop", () => {
		const parentA = {
			entry: { key_insights: [{ insight: "A", evidence: { conversation_id: "c1", message_ids: ["m1"], snippet: "s1" } }] },
			metadata: { ...emptyMetadata(), source_conversations: ["c1"], source_messages: ["m1"] },
		};
		const parentB = {
			entry: { key_insights: [{ insight: "B", evidence: { conversation_id: "c2", message_ids: ["m2"], snippet: "s2" } }] },
			metadata: { ...emptyMetadata(), source_conversations: ["c2"], source_messages: ["m2"] },
		};
		// merged silently drops parentB's insight — no drop mapping recorded.
		const merged = {
			entry: { key_insights: [parentA.entry.key_insights[0]] },
			metadata: { source_conversations: ["c1", "c2"], source_messages: ["m1", "m2"], first_seen: "2026-01-01T00:00:00Z", last_seen: "2026-01-01T00:00:00Z", mention_count: 2 },
		};
		const result = validateMergeConservation({ parents: [parentA, parentB], merged, insightDrops: [] });
		expect(result.ok).toBe(false);
		expect(result.violations.some((v) => v.startsWith("evidence_conservation"))).toBe(true);
	});

	it("blocks when source_conversations union is not preserved", () => {
		const parentA = { entry: {}, metadata: { ...emptyMetadata(), source_conversations: ["c1"] } };
		const parentB = { entry: {}, metadata: { ...emptyMetadata(), source_conversations: ["c2"] } };
		const merged = { entry: {}, metadata: { source_conversations: ["c1"], source_messages: [], first_seen: "2026-01-01T00:00:00Z", last_seen: "2026-01-01T00:00:00Z", mention_count: 1 } };
		const result = validateMergeConservation({ parents: [parentA, parentB], merged, insightDrops: [] });
		expect(result.ok).toBe(false);
		expect(result.violations.some((v) => v.includes("source_conversations"))).toBe(true);
	});

	it("blocks when mention_count does not match the union-of-source-conversations rule", () => {
		const parentA = { entry: {}, metadata: { ...emptyMetadata(), source_conversations: ["c1"] } };
		const parentB = { entry: {}, metadata: { ...emptyMetadata(), source_conversations: ["c2"] } };
		const merged = { entry: {}, metadata: { source_conversations: ["c1", "c2"], source_messages: [], first_seen: "2026-01-01T00:00:00Z", last_seen: "2026-01-01T00:00:00Z", mention_count: 99 } };
		const result = validateMergeConservation({ parents: [parentA, parentB], merged, insightDrops: [] });
		expect(result.ok).toBe(false);
		expect(result.violations.some((v) => v.includes("mention_count"))).toBe(true);
	});

	it("blocks when first_seen regresses later than the true earliest parent", () => {
		const parentA = { entry: {}, metadata: { ...emptyMetadata(), first_seen: "2020-01-01T00:00:00Z" } };
		const parentB = { entry: {}, metadata: { ...emptyMetadata(), first_seen: "2026-01-01T00:00:00Z" } };
		const merged = { entry: {}, metadata: { source_conversations: [], source_messages: [], first_seen: "2026-01-01T00:00:00Z", last_seen: "2026-01-01T00:00:00Z", mention_count: 2 } };
		const result = validateMergeConservation({ parents: [parentA, parentB], merged, insightDrops: [] });
		expect(result.ok).toBe(false);
		expect(result.violations.some((v) => v.includes("first_seen"))).toBe(true);
	});

	it("blocks when last_seen regresses earlier than the true latest parent", () => {
		const parentA = { entry: {}, metadata: { ...emptyMetadata(), last_seen: "2026-06-01T00:00:00Z" } };
		const parentB = { entry: {}, metadata: { ...emptyMetadata(), last_seen: "2020-01-01T00:00:00Z" } };
		const merged = { entry: {}, metadata: { source_conversations: [], source_messages: [], first_seen: "2020-01-01T00:00:00Z", last_seen: "2020-01-01T00:00:00Z", mention_count: 2 } };
		const result = validateMergeConservation({ parents: [parentA, parentB], merged, insightDrops: [] });
		expect(result.ok).toBe(false);
		expect(result.violations.some((v) => v.includes("last_seen"))).toBe(true);
	});

	it("names every violated rule when multiple checks fail simultaneously", () => {
		const parentA = { entry: {}, metadata: { ...emptyMetadata(), source_conversations: ["c1"] } };
		const parentB = { entry: {}, metadata: { ...emptyMetadata(), source_conversations: ["c2"] } };
		const merged = { entry: {}, metadata: { source_conversations: [], source_messages: [], first_seen: "2026-01-01T00:00:00Z", last_seen: "2026-01-01T00:00:00Z", mention_count: 0 } };
		const result = validateMergeConservation({ parents: [parentA, parentB], merged, insightDrops: [] });
		expect(result.ok).toBe(false);
		expect(result.violations.length).toBeGreaterThanOrEqual(2);
	});
});

describe("isProtectedContextType", () => {
	const protectedTypes = ["explicit_save", "stated_preference", "professional_identity"];

	it("flags each protected type", () => {
		for (const t of protectedTypes) {
			expect(isProtectedContextType(t, protectedTypes)).toBe(true);
		}
	});

	it("does not flag an unprotected type", () => {
		expect(isProtectedContextType("task_query", protectedTypes)).toBe(false);
	});

	it("does not flag a non-string context type", () => {
		expect(isProtectedContextType(undefined, protectedTypes)).toBe(false);
		expect(isProtectedContextType(null, protectedTypes)).toBe(false);
	});
});
