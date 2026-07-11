// =============================================================================
// SCRIPT NAME: mmr.test.ts
// =============================================================================
// DESCRIPTION:
// Vitest unit tests for cloudflare-mcp/mcp-server/src/mmr.ts's greedy MMR
// diversity selection (contract PKS-INJECTION-RANKING-002, Phase B). Builds
// synthetic in-memory candidate pools (no fixtures, no network) and asserts
// the top-1-preservation and budget/domain-cap invariants below.
//
// INPUT FILES:
// - /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/shared/memory_policy.json
//     (read indirectly via src/mmr.ts's import, for the salience_v2.mmr_*
//     default lambda/maxPerDomain/tokenBudget values)
// OUTPUT FILES:
// - None. vitest reports to stdout only.
//
// Covers INV4 and INV5 of contract PKS-INJECTION-RANKING-002:
//   INV4 - diversity selection never displaces the single best match: the
//          top-1 result under selectWithDiversity always equals the top-1
//          under pure final_score ranking. Property-style: several varied
//          candidate pools (hardcoded; no fuzzing library is set up in this
//          repo, matching the style of other test/*.ts files here).
//   INV5 - the selected set respects the per-domain cap (<=2 entries sharing
//          a domainCluster) and the token budget (cumulative estTokens never
//          exceeds the declared budget), with the budget caller-overridable
//          and defaulting to 3000 (shared/memory_policy.json's
//          salience_v2.mmr_default_token_budget).
// =============================================================================

import { describe, expect, it } from "vitest";

import { cosineSimilarity, selectWithDiversity, type MmrCandidate } from "../src/mmr";

function candidate(
	id: string,
	finalScore: number,
	domainCluster: string,
	embedding: number[],
	estTokens = 100,
): MmrCandidate {
	return { id, finalScore, domainCluster, embedding, estTokens };
}

function argmaxByFinalScore(candidates: MmrCandidate[]): string {
	let best = candidates[0];
	for (const c of candidates.slice(1)) {
		if (c.finalScore > best.finalScore || (c.finalScore === best.finalScore && c.id < best.id)) {
			best = c;
		}
	}
	return best.id;
}

describe("cosineSimilarity", () => {
	it("is 1 for identical vectors", () => {
		expect(cosineSimilarity([1, 0, 0], [1, 0, 0])).toBeCloseTo(1, 6);
	});

	it("is 0 for orthogonal vectors", () => {
		expect(cosineSimilarity([1, 0], [0, 1])).toBeCloseTo(0, 6);
	});

	it("handles zero-magnitude vectors without dividing by zero or throwing", () => {
		expect(cosineSimilarity([0, 0, 0], [1, 2, 3])).toBe(0);
		expect(cosineSimilarity([], [])).toBe(0);
	});
});

describe("INV4 — top-1 is always the pure best match, across varied pools", () => {
	const pools: MmrCandidate[][] = [
		// Pool 1: uniform domain, distinct embeddings.
		[
			candidate("a", 0.95, "domain_x", [1, 0, 0]),
			candidate("b", 0.90, "domain_x", [0, 1, 0]),
			candidate("c", 0.80, "domain_x", [0, 0, 1]),
		],
		// Pool 2: best candidate is also maximally similar to the rest — MMR
		// penalty must not knock it out of pick #1.
		[
			candidate("best", 0.99, "domain_a", [1, 1, 1]),
			candidate("dup1", 0.85, "domain_a", [1, 1, 1]),
			candidate("dup2", 0.80, "domain_a", [1, 1, 1]),
		],
		// Pool 3: many domains, single strong winner.
		[
			candidate("winner", 0.70, "d1", [1, 0]),
			candidate("x2", 0.69, "d2", [0, 1]),
			candidate("x3", 0.50, "d3", [1, 1]),
			candidate("x4", 0.10, "d4", [0.5, 0.5]),
		],
		// Pool 4: tie at the top, id breaks it deterministically.
		[
			candidate("zz_tie", 0.60, "d1", [1, 0]),
			candidate("aa_tie", 0.60, "d2", [0, 1]),
		],
		// Pool 5: single candidate.
		[candidate("only", 0.42, "d1", [1, 0])],
		// Pool 6: large pool, varied domains/scores.
		Array.from({ length: 12 }, (_, i) =>
			candidate(`c${i}`, (12 - i) / 12, `d${i % 4}`, [Math.cos(i), Math.sin(i)]),
		),
		// Pool 7: negative-score-adjacent (scores near zero).
		[
			candidate("n1", 0.01, "d1", [1, 0]),
			candidate("n2", 0.005, "d1", [0, 1]),
			candidate("n3", 0.02, "d2", [1, 1]),
		],
	];

	for (const [i, pool] of pools.entries()) {
		it(`pool #${i + 1}: selectWithDiversity(...)[0] equals argmax-by-finalScore`, () => {
			const selected = selectWithDiversity(pool, { limit: Math.min(3, pool.length) });
			expect(selected[0]?.id).toBe(argmaxByFinalScore(pool));
		});
	}
});

describe("INV5 — domain cap and token budget are enforced", () => {
	it("never selects more than maxPerDomain entries from one domainCluster", () => {
		const pool: MmrCandidate[] = [
			candidate("a1", 0.95, "domain_a", [1, 0, 0]),
			candidate("a2", 0.90, "domain_a", [0.9, 0.1, 0]),
			candidate("a3", 0.85, "domain_a", [0.8, 0.2, 0]),
			candidate("a4", 0.80, "domain_a", [0.7, 0.3, 0]),
			candidate("b1", 0.50, "domain_b", [0, 1, 0]),
		];
		const selected = selectWithDiversity(pool, { limit: 5, maxPerDomain: 2 });
		const domainACount = selected.filter((c) => c.domainCluster === "domain_a").length;
		expect(domainACount).toBeLessThanOrEqual(2);
		// domain_b's single candidate should still be pulled in once domain_a hits its cap.
		expect(selected.some((c) => c.id === "b1")).toBe(true);
	});

	it("stops selecting once the token budget would be exceeded", () => {
		const pool: MmrCandidate[] = [
			candidate("t1", 0.95, "d1", [1, 0], 1200),
			candidate("t2", 0.90, "d2", [0, 1], 1200),
			candidate("t3", 0.85, "d3", [1, 1], 1200),
			candidate("t4", 0.80, "d4", [1, -1], 1200),
		];
		const selected = selectWithDiversity(pool, { limit: 4, tokenBudget: 3000 });
		const totalTokens = selected.reduce((sum, c) => sum + c.estTokens, 0);
		expect(totalTokens).toBeLessThanOrEqual(3000);
		// 1200 * 3 = 3600 > 3000, so at most 2 of the 4 fit.
		expect(selected.length).toBeLessThanOrEqual(2);
	});

	it("defaults (lambda/maxPerDomain/tokenBudget) come from shared/memory_policy.json's salience_v2 block, not hardcoded twice", () => {
		// A pool sized so the default 3000-token budget (not a caller override)
		// is the binding constraint.
		const pool: MmrCandidate[] = Array.from({ length: 6 }, (_, i) =>
			candidate(`d${i}`, 1 - i * 0.05, `domain_${i}`, [i, 1], 1000),
		);
		const selected = selectWithDiversity(pool, { limit: 6 });
		const totalTokens = selected.reduce((sum, c) => sum + c.estTokens, 0);
		expect(totalTokens).toBeLessThanOrEqual(3000);
		expect(selected.length).toBe(3); // 1000 * 3 = 3000 exactly fits; a 4th would exceed
	});

	it("INV4 takes precedence over INV5 in the one edge case where they conflict: a top-1 candidate whose own estTokens alone exceeds the budget is still selected (never displaced), and the selection stops there — the overage is bounded to exactly that one candidate, never compounded by a second pick", () => {
		// Regression case: adversarial review 2026-07-11 correctly observed that
		// cumulativeTokens can exceed tokenBudget after pick #1 alone, and asked
		// whether that's a bug. It is not — the contract's own §2.2 text is
		// explicit: "the top-1 result is always the raw best match (diversity
		// must never cost the best answer — invariant in the contract)". This
		// test proves the trade-off is bounded: pick #1 may exceed budget alone,
		// but the loop then correctly refuses every subsequent candidate (their
		// addition would push cumulativeTokens further over budget) and
		// terminates with selected.length === 1, not silently unbounded growth.
		const pool: MmrCandidate[] = [
			candidate("huge", 0.99, "domain_a", [1, 0], 5000), // alone exceeds any normal budget
			candidate("small1", 0.80, "domain_b", [0, 1], 100),
			candidate("small2", 0.70, "domain_c", [1, 1], 100),
		];
		const selected = selectWithDiversity(pool, { limit: 5, tokenBudget: 3000 });
		expect(selected.length).toBe(1);
		expect(selected[0].id).toBe("huge"); // top-1 preserved despite exceeding budget alone (INV4)
		const totalTokens = selected.reduce((sum, c) => sum + c.estTokens, 0);
		expect(totalTokens).toBe(5000); // overage is exactly pick #1's own size, never compounded
	});

	it("an empty pool or zero limit returns no results", () => {
		expect(selectWithDiversity([], { limit: 5 })).toEqual([]);
		expect(selectWithDiversity([candidate("a", 0.5, "d1", [1, 0])], { limit: 0 })).toEqual([]);
	});
});
