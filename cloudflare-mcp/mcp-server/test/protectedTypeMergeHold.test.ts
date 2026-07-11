// =============================================================================
// SCRIPT NAME: protectedTypeMergeHold.test.ts
// =============================================================================
// INPUT FILES: None. This module has no file I/O of any kind — all proposal
//   and grade fixtures are constructed in-memory.
// OUTPUT FILES: None. This module has no file I/O of any kind — vitest
//   reports results to stdout only.
//
// Covers INV1 of contract PKS-SEMANTIC-CONSOLIDATION-001: a protected context
// type (professional_identity, stated_preference, explicit_save) must never
// be an automated merge loser. Exercises buildScheduledGovernedDecision
// (src/index.ts) directly with a constructed proposal/grade pair — the same
// pure-function-under-test pattern test/ranking-v2-flag.test.ts and
// test/scheduled.test.ts use, so no Worker HTTP transport or mocked Redis is
// needed for a function that takes plain objects and returns plain objects.
// =============================================================================

import { describe, expect, it } from "vitest";

import { buildScheduledGovernedDecision } from "../src/index";

function makeDuplicateMergeOperation(overrides: {
	operationId: string;
	keepId: string;
	archiveId: string;
	archiveContextType: string;
}): Record<string, unknown> {
	return {
		operation_id: overrides.operationId,
		type: "duplicate_merge",
		keep_id: overrides.keepId,
		archive_ids: [overrides.archiveId],
		expected_revisions: { [overrides.keepId]: 1, [overrides.archiveId]: 1 },
		semantic_only: false,
		reason: "Dream detected compatible duplicate entries with the same normalized topic fingerprint.",
		evidence: {
			fingerprint: "fp_test",
			canonical: { id: overrides.keepId, context_type: "recurring_pattern" },
			duplicates: [{ id: overrides.archiveId, context_type: overrides.archiveContextType }],
		},
		rollback: { method: "restore_archived", entry_ids: [overrides.archiveId] },
	};
}

function makeArchiveOperation(entryId: string): Record<string, unknown> {
	return {
		operation_id: `dop_archive_${entryId}`,
		type: "archive_entry",
		entry_id: entryId,
		expected_revision: 1,
		reason: "Dream found low-salience single-mention memory with no retrieval reinforcement.",
		evidence: { id: entryId },
		rollback: { method: "restore_archived", entry_id: entryId },
	};
}

function makeProposal(operations: Array<Record<string, unknown>>): Record<string, unknown> {
	const candidateIds = new Set<string>();
	for (const op of operations) {
		if (typeof op.keep_id === "string") candidateIds.add(op.keep_id);
		if (typeof op.entry_id === "string") candidateIds.add(op.entry_id);
		for (const id of (op.archive_ids as string[] | undefined) ?? []) candidateIds.add(id);
	}
	return {
		run_id: "dpr_protected_type_test",
		status: "proposal_ready",
		risk_score: "medium",
		candidate_ids: [...candidateIds],
		operations,
	};
}

function makePassedGrade(operations: Array<Record<string, unknown>>): Record<string, unknown> {
	return {
		grade_id: "dpg_protected_type_test",
		status: "passed",
		passed: true,
		operation_ids: operations.map((op) => op.operation_id),
	};
}

describe("buildScheduledGovernedDecision — INV1 protected-type merge-loser hold", () => {
	it("holds a duplicate_merge whose archive_ids includes an explicit_save entry, and does not auto-select it", () => {
		const op = makeDuplicateMergeOperation({
			operationId: "dop_merge_ke_1_ke_2",
			keepId: "ke_1",
			archiveId: "ke_2",
			archiveContextType: "explicit_save",
		});
		const proposal = makeProposal([op]);
		const grade = makePassedGrade([op]);

		const decision = buildScheduledGovernedDecision(proposal, grade);

		expect(decision.selectedOperationIds).not.toContain("dop_merge_ke_1_ke_2");
		expect(decision.selectedOperationIds).toHaveLength(0);
		const held = decision.heldOperations.find((h) => h.operation_id === "dop_merge_ke_1_ke_2");
		expect(held).toBeDefined();
		expect(held?.reason).toMatch(/^protected_type_requires_approval:ke_2:explicit_save$/);
	});

	it("holds for each protected type (professional_identity, stated_preference, explicit_save)", () => {
		for (const protectedType of ["professional_identity", "stated_preference", "explicit_save"]) {
			const op = makeDuplicateMergeOperation({
				operationId: `dop_merge_ke_1_ke_${protectedType}`,
				keepId: "ke_1",
				archiveId: `ke_${protectedType}`,
				archiveContextType: protectedType,
			});
			const proposal = makeProposal([op]);
			const grade = makePassedGrade([op]);

			const decision = buildScheduledGovernedDecision(proposal, grade);

			expect(decision.selectedOperationIds).toHaveLength(0);
			expect(decision.heldOperations[0]?.reason).toContain("protected_type_requires_approval");
		}
	});

	it("does NOT hold a duplicate_merge whose archive_ids are all unprotected types", () => {
		const op = makeDuplicateMergeOperation({
			operationId: "dop_merge_ke_1_ke_3",
			keepId: "ke_1",
			archiveId: "ke_3",
			archiveContextType: "task_query",
		});
		const proposal = makeProposal([op]);
		const grade = makePassedGrade([op]);

		const decision = buildScheduledGovernedDecision(proposal, grade);

		expect(decision.selectedOperationIds).toEqual(["dop_merge_ke_1_ke_3"]);
		expect(decision.heldOperations).toHaveLength(0);
	});

	it("holds only the protected-type merge while auto-selecting an unrelated unprotected operation in the same proposal", () => {
		const protectedOp = makeDuplicateMergeOperation({
			operationId: "dop_merge_ke_1_ke_2",
			keepId: "ke_1",
			archiveId: "ke_2",
			archiveContextType: "stated_preference",
		});
		const okOp = makeDuplicateMergeOperation({
			operationId: "dop_merge_ke_5_ke_6",
			keepId: "ke_5",
			archiveId: "ke_6",
			archiveContextType: "passing_reference",
		});
		const archiveOp = makeArchiveOperation("ke_9");
		const proposal = makeProposal([protectedOp, okOp, archiveOp]);
		const grade = makePassedGrade([protectedOp, okOp, archiveOp]);

		const decision = buildScheduledGovernedDecision(proposal, grade);

		expect(decision.selectedOperationIds.sort()).toEqual(["dop_archive_ke_9", "dop_merge_ke_5_ke_6"]);
		expect(decision.heldOperations.map((h) => h.operation_id)).toEqual(["dop_merge_ke_1_ke_2"]);
		expect(decision.heldOperations[0]?.reason).toBe("protected_type_requires_approval:ke_2:stated_preference");
	});
});
