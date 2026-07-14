import { describe, expect, it } from "vitest";
import {
	MAX_MAINTENANCE_CLUSTER_SIZE,
	maintenanceRetryDelaySeconds,
	validateMaintenanceMessage,
} from "../src/maintenanceQueue";

const base = {
	schema_version: 1 as const,
	task_id: "task-1",
	kind: "semantic_candidate" as const,
	created_at: "2026-07-14T00:00:00.000Z",
};

describe("bounded maintenance queue contract", () => {
	it("accepts only bounded unique candidate clusters", () => {
		expect(validateMaintenanceMessage({ ...base, candidate_ids: ["ke_a", "ke_b"] }).ok).toBe(true);
		expect(validateMaintenanceMessage({ ...base, candidate_ids: ["ke_a", "ke_a"] })).toMatchObject({ ok: false });
		expect(validateMaintenanceMessage({ ...base, candidate_ids: Array.from({ length: MAX_MAINTENANCE_CLUSTER_SIZE + 1 }, (_, i) => `ke_${i}`) })).toMatchObject({ ok: false });
	});

	it("uses bounded exponential retry delays", () => {
		expect(maintenanceRetryDelaySeconds(0)).toBe(5);
		expect(maintenanceRetryDelaySeconds(4)).toBe(80);
		expect(maintenanceRetryDelaySeconds(100)).toBeLessThanOrEqual(900);
	});
});
