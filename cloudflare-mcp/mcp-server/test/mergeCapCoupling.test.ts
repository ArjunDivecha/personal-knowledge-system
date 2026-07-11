// =============================================================================
// SCRIPT NAME: mergeCapCoupling.test.ts
// =============================================================================
// INPUT FILES:
// - /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/shared/memory_policy.json
//   (read indirectly via src/salience.ts's MEMORY_POLICY export — the same
//   checked-in policy the running Worker loads).
// OUTPUT FILES: None. vitest reports results to stdout only.
//
// Covers INV6 of contract PKS-SEMANTIC-CONSOLIDATION-001: the scheduled
// duplicate_merge cap is coupled to the merge hard-gates feature flag, both
// as a config assertion against the real checked-in policy file (a) and as a
// direct unit test of the cap-resolution function itself (b) — the latter is
// the "more defensive" option the contract calls out: the coupling must be
// enforced by the running code (resolveScheduledDuplicateMergeLimit,
// src/index.ts), not merely documented in the JSON.
// =============================================================================

import { describe, expect, it } from "vitest";

import { MEMORY_POLICY } from "../src/salience";
import { resolveScheduledDuplicateMergeLimit, SCHEDULED_DREAM_DUPLICATE_MERGE_LIMIT } from "../src/index";

describe("mergeCapCoupling — INV6 (config assertion against the real checked-in policy)", () => {
	it("cap > 10 is only allowed when merge_hard_gates_active is literally true", () => {
		const thresholds = MEMORY_POLICY.dream_thresholds as unknown as Record<string, unknown>;
		const gatesActive = thresholds.merge_hard_gates_active === true;
		const cap = thresholds.scheduled_duplicate_merge_limit as number;
		// This is the literal INV6 invariant: cap>10 with gates off must fail.
		expect(gatesActive || cap <= 10).toBe(true);
	});

	it("the exported runtime constant reflects the resolver, not the raw policy value", () => {
		const thresholds = MEMORY_POLICY.dream_thresholds as unknown as Record<string, unknown>;
		expect(SCHEDULED_DREAM_DUPLICATE_MERGE_LIMIT).toBe(resolveScheduledDuplicateMergeLimit(thresholds));
	});
});

describe("resolveScheduledDuplicateMergeLimit — INV6 (direct unit coverage, the defensive/enforced option)", () => {
	it("clamps a cap of 50 to 10 when merge_hard_gates_active is false", () => {
		const limit = resolveScheduledDuplicateMergeLimit({
			scheduled_duplicate_merge_limit: 50,
			merge_hard_gates_active: false,
		});
		expect(limit).toBe(10);
	});

	it("clamps a cap of 50 to 10 when merge_hard_gates_active is missing entirely", () => {
		const limit = resolveScheduledDuplicateMergeLimit({
			scheduled_duplicate_merge_limit: 50,
		});
		expect(limit).toBe(10);
	});

	it("honors a cap of 50 when merge_hard_gates_active is true", () => {
		const limit = resolveScheduledDuplicateMergeLimit({
			scheduled_duplicate_merge_limit: 50,
			merge_hard_gates_active: true,
		});
		expect(limit).toBe(50);
	});

	it("never raises the cap above what's configured, even with gates active", () => {
		const limit = resolveScheduledDuplicateMergeLimit({
			scheduled_duplicate_merge_limit: 3,
			merge_hard_gates_active: true,
		});
		expect(limit).toBe(3);
	});

	it("defaults to 10 when scheduled_duplicate_merge_limit is absent, regardless of the gate flag", () => {
		expect(resolveScheduledDuplicateMergeLimit({ merge_hard_gates_active: true })).toBe(10);
		expect(resolveScheduledDuplicateMergeLimit({ merge_hard_gates_active: false })).toBe(10);
		expect(resolveScheduledDuplicateMergeLimit({})).toBe(10);
	});

	it("does not let an unclamped-below-10 configured value slip through with gates off (a cap below 10 stays as configured, not raised)", () => {
		const limit = resolveScheduledDuplicateMergeLimit({
			scheduled_duplicate_merge_limit: 5,
			merge_hard_gates_active: false,
		});
		expect(limit).toBe(5);
	});
});
