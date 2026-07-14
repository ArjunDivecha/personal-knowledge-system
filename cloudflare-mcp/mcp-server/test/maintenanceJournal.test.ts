import { describe, expect, it } from "vitest";
import { ATOMIC_MERGE_COMMIT_LUA, buildMaintenanceJournal, isTerminalMaintenanceStatus } from "../src/maintenanceJournal";

describe("durable maintenance journal", () => {
	it("binds revisions and outbox intent in one CAS script", () => {
		expect(ATOMIC_MERGE_COMMIT_LUA).toContain("HGET");
		expect(ATOMIC_MERGE_COMMIT_LUA).toContain("revision");
		expect(ATOMIC_MERGE_COMMIT_LUA).toContain("LPUSH");
	});
	it("records every touched entry and has explicit terminal states", () => {
		const entry = { id: "k1", metadata: { revision: 4 } } as any;
		const journal = buildMaintenanceJournal("task-1", entry, [{ id: "k2", metadata: { revision: 2 } } as any], "2026-07-14T00:00:00Z", "maintenance:outbox:task-1");
		expect(journal.expected_revisions).toEqual({ k1: 4, k2: 2 });
		expect(journal.status).toBe("prepared");
		expect(isTerminalMaintenanceStatus("derived_complete")).toBe(true);
		expect(isTerminalMaintenanceStatus("prepared")).toBe(false);
	});
});
