import { describe, expect, it } from "vitest";
import {
	assertAutomaticMergeAllowed,
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
	it("rejects an incomplete planner component instead of trusting submitted membership", () => {
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
				entry("ke_c", [0.998, 0.06]),
			],
			["ke_a", "ke_b"],
			0.95,
			6,
		);
		expect(incomplete.ok).toBe(false);
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
