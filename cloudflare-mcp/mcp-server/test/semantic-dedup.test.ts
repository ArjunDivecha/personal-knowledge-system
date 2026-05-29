// Phase 1 semantic entity resolution — unit tests for the pure grouping /
// classification logic. The nearest-neighbour lookup is injected as a fake so
// no vector store is needed.

import { describe, expect, it } from "vitest";

import {
	connectedComponents,
	classifyDuplicateComponent,
	buildReplayPlansWithSemantic,
	type NeighborFn,
	type SemanticDedupConfig,
} from "../src/dream";

const CONFIG: SemanticDedupConfig = { cosineThreshold: 0.86, neighborK: 10, maxQueries: 100 };

// Minimal LoadedEntry-shaped factory.
function mk(args: {
	id: string;
	label: string;
	view?: string;
	tier?: 1 | 2 | 3;
	mention?: number;
	access?: number;
	salience?: number;
}): any {
	return {
		id: args.id,
		type: "knowledge",
		entry: { id: args.id, current_view: args.view ?? args.label, confidence: "high" },
		metadata: { injection_tier: args.tier ?? 1 },
		label: args.label,
		updatedAt: "2026-05-29T00:00:00.000Z",
		contextType: "active_project",
		injectionTier: args.tier ?? 1,
		mentionCount: args.mention ?? 1,
		accessCount: args.access ?? 0,
		sourceConversationCount: 1,
		salienceScore: args.salience ?? 0.3,
	};
}

// Fake neighbour function from an explicit cosine matrix.
function neighborsFrom(matrix: Record<string, Array<[string, number]>>): NeighborFn {
	return async (entry: any) => (matrix[entry.id] ?? []).map(([id, score]) => ({ id, score }));
}

describe("connectedComponents (union-find)", () => {
	it("collapses A~B~C transitively", () => {
		const comps = connectedComponents(["A", "B", "C", "D"], [
			{ a: "A", b: "B" },
			{ a: "B", b: "C" },
		]);
		const sizes = comps.map((c) => c.length).sort();
		expect(sizes).toEqual([1, 3]);
	});

	it("keeps unrelated ids as singletons", () => {
		const comps = connectedComponents(["A", "B"], []);
		expect(comps.map((c) => c.length).sort()).toEqual([1, 1]);
	});

	it("ignores edges to unknown ids", () => {
		const comps = connectedComponents(["A", "B"], [{ a: "A", b: "Z" }]);
		// Z is not a known id; A and B stay singletons.
		const known = comps.filter((c) => c.some((x) => x === "A" || x === "B"));
		expect(known.flat().sort()).toEqual(["A", "B"]);
	});
});

describe("classifyDuplicateComponent", () => {
	it("merges a compatible group and picks the highest-priority canonical", () => {
		const a = mk({ id: "ke_a", label: "loop pilot architecture", tier: 2, mention: 1 });
		const b = mk({ id: "ke_b", label: "loop pilot architecture", tier: 1, mention: 3 });
		const res = classifyDuplicateComponent([a, b], new Map());
		expect(res?.kind).toBe("duplicate");
		if (res?.kind === "duplicate") {
			// tier 1 beats tier 2 → ke_b canonical
			expect(res.plan.canonical.id).toBe("ke_b");
			expect(res.plan.duplicates.map((d: any) => d.id)).toEqual(["ke_a"]);
		}
	});

	it("flags semantic_only when members have different fingerprints", () => {
		const a = mk({ id: "ke_a", label: "loop pilot architecture overview" });
		const b = mk({ id: "ke_b", label: "autonomous experiment loop runner design" });
		const pc = new Map<string, number>([["ke_a|ke_b", 0.91]]);
		const res = classifyDuplicateComponent([a, b], pc);
		expect(res?.kind).toBe("duplicate");
		if (res?.kind === "duplicate") {
			expect(res.plan.semanticOnly).toBe(true);
			expect(res.plan.maxCosine).toBeCloseTo(0.91, 4);
		}
	});

	it("does NOT flag semantic_only when all share a fingerprint", () => {
		const a = mk({ id: "ke_a", label: "loop pilot architecture" });
		const b = mk({ id: "ke_b", label: "loop pilot architecture" });
		const res = classifyDuplicateComponent([a, b], new Map());
		if (res?.kind === "duplicate") {
			expect(res.plan.semanticOnly).toBe(false);
		}
	});

	it("routes opposing-marker pairs to contradiction, never merge", () => {
		const a = mk({ id: "ke_a", label: "rates call", view: "We are bullish on duration here." });
		const b = mk({ id: "ke_b", label: "rates call", view: "We are bearish on duration here." });
		const res = classifyDuplicateComponent([a, b], new Map());
		expect(res?.kind).toBe("contradiction");
		if (res?.kind === "contradiction") {
			expect(res.plan.entryIds.sort()).toEqual(["ke_a", "ke_b"]);
			expect(res.plan.reasons.length).toBeGreaterThan(0);
		}
	});
});

describe("buildReplayPlansWithSemantic", () => {
	it("merges a semantic-only pair found via embeddings (different titles)", async () => {
		const a = mk({ id: "ke_a", label: "loop pilot architecture overview" });
		const b = mk({ id: "ke_b", label: "autonomous experiment loop runner" });
		const neighbor = neighborsFrom({ ke_a: [["ke_b", 0.9]], ke_b: [["ke_a", 0.9]] });
		const { duplicatePlans, semantic } = await buildReplayPlansWithSemantic([a, b], neighbor, CONFIG);
		expect(duplicatePlans.length).toBe(1);
		expect(duplicatePlans[0].semanticOnly).toBe(true);
		expect(semantic.enabled).toBe(true);
		expect(semantic.edges).toBeGreaterThan(0);
	});

	it("merges a lexical pair even with no neighbour edges", async () => {
		const a = mk({ id: "ke_a", label: "pattern cnn trading" });
		const b = mk({ id: "ke_b", label: "pattern cnn trading" });
		const neighbor = neighborsFrom({}); // no semantic edges
		const { duplicatePlans } = await buildReplayPlansWithSemantic([a, b], neighbor, CONFIG);
		expect(duplicatePlans.length).toBe(1);
		expect(duplicatePlans[0].semanticOnly).toBe(false);
	});

	it("collapses a 3-member transitive cluster into one plan", async () => {
		const a = mk({ id: "ke_a", label: "karpathy nanochat loop" });
		const b = mk({ id: "ke_b", label: "karpathy training loop walkthrough" });
		const c = mk({ id: "ke_c", label: "nanochat reproduction notes" });
		const neighbor = neighborsFrom({
			ke_a: [["ke_b", 0.9]],
			ke_b: [["ke_c", 0.88]],
			ke_c: [["ke_b", 0.88]],
		});
		const { duplicatePlans } = await buildReplayPlansWithSemantic([a, b, c], neighbor, CONFIG);
		expect(duplicatePlans.length).toBe(1);
		const ids = [duplicatePlans[0].canonical.id, ...duplicatePlans[0].duplicates.map((d: any) => d.id)].sort();
		expect(ids).toEqual(["ke_a", "ke_b", "ke_c"]);
	});

	it("does not merge below the cosine threshold", async () => {
		const a = mk({ id: "ke_a", label: "asado country rotation" });
		const b = mk({ id: "ke_b", label: "inmobi ipo advisory" });
		const neighbor = neighborsFrom({ ke_a: [["ke_b", 0.5]], ke_b: [["ke_a", 0.5]] });
		const { duplicatePlans } = await buildReplayPlansWithSemantic([a, b], neighbor, CONFIG);
		expect(duplicatePlans.length).toBe(0);
	});

	it("an opposing-marker semantic pair becomes contested, not merged", async () => {
		const a = mk({ id: "ke_a", label: "duration view", view: "bullish on rates and adding duration" });
		const b = mk({ id: "ke_b", label: "rates positioning", view: "bearish on rates and cutting duration" });
		const neighbor = neighborsFrom({ ke_a: [["ke_b", 0.95]], ke_b: [["ke_a", 0.95]] });
		const { duplicatePlans, contradictionPlans } = await buildReplayPlansWithSemantic([a, b], neighbor, CONFIG);
		expect(duplicatePlans.length).toBe(0);
		expect(contradictionPlans.length).toBeGreaterThanOrEqual(1);
	});

	it("falls back to lexical-only when no neighbour function is provided", async () => {
		const a = mk({ id: "ke_a", label: "shared exact title here" });
		const b = mk({ id: "ke_b", label: "shared exact title here" });
		const { duplicatePlans, semantic } = await buildReplayPlansWithSemantic([a, b], null, CONFIG);
		expect(semantic.enabled).toBe(false);
		expect(duplicatePlans.length).toBe(1);
		expect(duplicatePlans[0].semanticOnly).toBe(false);
	});

	it("respects maxQueries cap", async () => {
		const entries = Array.from({ length: 10 }, (_, i) => mk({ id: `ke_${i}`, label: `topic ${i} unique title` }));
		const neighbor = neighborsFrom({});
		const { semantic } = await buildReplayPlansWithSemantic(entries, neighbor, { ...CONFIG, maxQueries: 4 });
		expect(semantic.capped).toBe(true);
		expect(semantic.queriesRun).toBe(4);
	});
});
