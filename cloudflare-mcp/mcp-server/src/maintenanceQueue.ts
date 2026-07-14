export type MaintenanceTaskKind =
	| "semantic_candidate"
	| "vector_outbox"
	| "recovery"
	| "lexical_bucket"
	| "retier_cursor"
	| "thin_index_patch";

export interface MaintenanceMessage {
	schema_version: 1;
	task_id: string;
	kind: MaintenanceTaskKind;
	plan_id?: string;
	candidate_ids?: string[];
	expected_revisions?: Record<string, number>;
	created_at: string;
}

export const MAX_MAINTENANCE_CLUSTER_SIZE = 6;
export const MAX_MAINTENANCE_REDIS_KEYS = 32;
export const MAX_MAINTENANCE_BYTES = 256 * 1024;
export const MAX_MAINTENANCE_ATTEMPTS = 5;

export function validateMaintenanceMessage(message: unknown): { ok: true; value: MaintenanceMessage } | { ok: false; reason: string } {
	if (!message || typeof message !== "object" || Array.isArray(message)) return { ok: false, reason: "message_not_object" };
	const value = message as Partial<MaintenanceMessage>;
	if (value.schema_version !== 1 || typeof value.task_id !== "string" || value.task_id.length === 0) {
		return { ok: false, reason: "message_identity_invalid" };
	}
	const kinds: MaintenanceTaskKind[] = ["semantic_candidate", "vector_outbox", "recovery", "lexical_bucket", "retier_cursor", "thin_index_patch"];
	if (typeof value.kind !== "string" || !kinds.includes(value.kind as MaintenanceTaskKind)) return { ok: false, reason: "message_kind_invalid" };
	if (value.candidate_ids !== undefined) {
		if (!Array.isArray(value.candidate_ids) || value.candidate_ids.length < 2 || value.candidate_ids.length > MAX_MAINTENANCE_CLUSTER_SIZE) {
			return { ok: false, reason: "candidate_ids_bound_invalid" };
		}
		if (new Set(value.candidate_ids).size !== value.candidate_ids.length || value.candidate_ids.some((id) => typeof id !== "string" || id.length === 0)) {
			return { ok: false, reason: "candidate_ids_invalid" };
		}
	}
	if (typeof value.created_at !== "string" || Number.isNaN(Date.parse(value.created_at))) return { ok: false, reason: "created_at_invalid" };
	return { ok: true, value: value as MaintenanceMessage };
}

export function maintenanceRetryDelaySeconds(attempts: number): number {
	const bounded = Math.max(0, Math.min(attempts, MAX_MAINTENANCE_ATTEMPTS));
	return Math.min(900, 2 ** bounded * 5);
}

export function maintenanceTaskKey(taskId: string): string {
	return `maintenance:task:${taskId}`;
}

export function maintenanceOutboxKey(taskId: string): string {
	return `maintenance:outbox:${taskId}`;
}
