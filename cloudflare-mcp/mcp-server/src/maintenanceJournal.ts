import type { LoadedEntry } from "./dream";

export type MaintenanceJournalStatus = "prepared" | "redis_committed" | "derived_complete" | "held" | "failed";

export interface MaintenanceJournalRecord {
	schema_version: 1;
	task_id: string;
	status: MaintenanceJournalStatus;
	created_at: string;
	canonical_id: string;
	duplicate_ids: string[];
	expected_revisions: Record<string, number>;
	vector_outbox_key: string;
	before_snapshots?: Array<{ entry: Record<string, unknown>; metadata: Record<string, unknown> }>;
}

/** Lua CAS skeleton used by the production adapter and pinned by tests. The
 * adapter must pass all Redis keys and expected revisions in one invocation;
 * no derived-store acknowledgement is valid before this script returns 1. */
export const ATOMIC_MERGE_COMMIT_LUA = `
local journal = redis.call('GET', KEYS[1])
if journal and journal ~= ARGV[1] then return 0 end
for i = 2, #KEYS do
  local expected = ARGV[i]
  local current = redis.call('HGET', KEYS[i], 'revision')
  if expected ~= '' and current ~= expected then return 0 end
end
redis.call('SET', KEYS[1], ARGV[1])
redis.call('LPUSH', KEYS[#KEYS], ARGV[2])
return 1
`;

export function buildMaintenanceJournal(
	taskId: string,
	canonical: LoadedEntry,
	duplicates: LoadedEntry[],
	createdAt: string,
	vectorOutboxKey: string,
	beforeSnapshots?: Array<{ entry: Record<string, unknown>; metadata: Record<string, unknown> }>,
): MaintenanceJournalRecord {
	const expected_revisions: Record<string, number> = {};
	for (const entry of [canonical, ...duplicates]) {
		expected_revisions[entry.id] = Number.isInteger(entry.metadata.revision) ? Number(entry.metadata.revision) : 0;
	}
	return {
		schema_version: 1,
		task_id: taskId,
		status: "prepared",
		created_at: createdAt,
		canonical_id: canonical.id,
		duplicate_ids: duplicates.map((entry) => entry.id),
		expected_revisions,
		vector_outbox_key: vectorOutboxKey,
		before_snapshots: beforeSnapshots,
	};
}

export function isTerminalMaintenanceStatus(status: string): boolean {
	return status === "derived_complete" || status === "held" || status === "failed";
}
