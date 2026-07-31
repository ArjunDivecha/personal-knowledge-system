import { describe, expect, it } from "vitest";
import {
	assertAutomaticMergeAllowed,
	isCurrentOmittedNeighbor,
	requiresEmbeddingRefresh,
	validateCandidateCluster,
} from "../src/semanticMaintenance";

const entry = (id: string, vector: number[], contextType = "task_query") => ({
	id,
	type: "knowledge" as const,
	contextType,
	vector,
});

describe("durable semantic consolidation authority", () => {
	it("uses the Upstash COSINE score scale when revalidating planner candidates", () => {
		// Raw cosine is 0.91, while the Upstash COSINE index score is 0.955.
		// A 0.95 planner candidate must therefore also pass Worker revalidation.
		const rawCosine = 0.91;
		const candidate = validateCandidateCluster(
			[
				entry("ke_a", [1, 0]),
				entry("ke_b", [rawCosine, Math.sqrt(1 - rawCosine ** 2)]),
			],
			["ke_a", "ke_b"],
			0.95,
			6,
		);
		expect(candidate).toMatchObject({ ok: true, component: ["ke_a", "ke_b"] });
	});

	it("rejects a disconnected planner component instead of trusting submitted membership", () => {
		const result = validateCandidateCluster(
			[
				entry("ke_a", [1, 0]),
				entry("ke_b", [0.999, 0.04]),
			],
			["ke_a", "ke_b"],
			0.95,
			6,
		);
		expect(result.ok).toBe(true);
		const incomplete = validateCandidateCluster(
			[
				entry("ke_a", [1, 0]),
				entry("ke_b", [0.999, 0.04]),
				entry("ke_c", [-1, 0]),
			],
			["ke_a", "ke_b", "ke_c"],
			0.95,
			6,
		);
		expect(incomplete).toMatchObject({
			ok: false,
			reason: "candidate_component_incomplete",
			component: ["ke_a", "ke_b"],
		});
	});

	it("ignores stale or cross-type vector neighbours", () => {
		const candidateIds = ["ke_a", "ke_b"];
		expect(
			isCurrentOmittedNeighbor(
				{ id: "ke_stale", score: 0.97, type: "knowledge" },
				candidateIds,
				new Set(),
				"knowledge",
				0.95,
			),
		).toBe(false);
		expect(
			isCurrentOmittedNeighbor(
				{ id: "pe_live", score: 0.97, type: "project" },
				candidateIds,
				new Set(["pe_live"]),
				"knowledge",
				0.95,
			),
		).toBe(false);
		expect(
			isCurrentOmittedNeighbor(
				{ id: "ke_live", score: 0.97, type: "knowledge" },
				candidateIds,
				new Set(["ke_live"]),
				"knowledge",
				0.95,
			),
		).toBe(true);
	});

	it("blocks protected types at the common automatic apply boundary", () => {
		expect(() => assertAutomaticMergeAllowed([entry("ke_explicit", [1, 0], "explicit_save")]))
			.toThrow("protected_type_requires_approval:ke_explicit");
	});

	it("detects canonical embedding refresh after merged content changes", () => {
		expect(requiresEmbeddingRefresh(
			{ embeddingInputSha256: "old", embeddingModel: "text-embedding-3-large", embeddingDimensions: 3072 },
			{ embeddingInputSha256: "new", embeddingModel: "text-embedding-3-large", embeddingDimensions: 3072 },
		)).toBe(true);
	});
});
