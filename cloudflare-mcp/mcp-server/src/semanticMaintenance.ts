export const DEFAULT_PROTECTED_CONTEXT_TYPES = [
	"explicit_save",
	"professional_identity",
	"stated_preference",
] as const;

export interface MaintenanceEntry {
	id: string;
	type: "knowledge" | "project";
	archived?: boolean;
	contextType?: string | null;
	revision?: number | null;
	vector?: number[] | null;
	salienceScore?: number;
	mentionCount?: number;
	updatedAt?: string | null;
}

export interface CandidateValidationResult {
	ok: boolean;
	reason?: string;
	component?: string[];
}

/**
 * Match the score returned by an Upstash Vector COSINE index.
 *
 * Upstash normalizes raw cosine similarity from [-1, 1] to [0, 1] as
 * `(1 + cosine) / 2`. The external planner compares the index score with the
 * policy threshold, so Worker-side revalidation must use the same scale.
 */
function cosineIndexScore(a: number[], b: number[]): number {
	if (a.length === 0 || a.length !== b.length) return 0;
	let dot = 0;
	let aa = 0;
	let bb = 0;
	for (let i = 0; i < a.length; i += 1) {
		dot += a[i] * b[i];
		aa += a[i] * a[i];
		bb += b[i] * b[i];
	}
	if (aa === 0 || bb === 0) return 0;
	const rawCosine = Math.max(-1, Math.min(1, dot / Math.sqrt(aa * bb)));
	return (1 + rawCosine) / 2;
}

function connectedComponent(entries: MaintenanceEntry[], threshold: number): string[] {
	const ids = entries.map((entry) => entry.id);
	const parent = new Map(ids.map((id) => [id, id]));
	const find = (id: string): string => {
		let root = parent.get(id) ?? id;
		while (parent.get(root) !== root) {
			root = parent.get(root) ?? root;
		}
		let current = id;
		while (parent.get(current) !== current) {
			const next = parent.get(current) ?? current;
			parent.set(current, root);
			current = next;
		}
		return root;
	};
	const union = (a: string, b: string): void => {
		const ra = find(a);
		const rb = find(b);
		if (ra !== rb) parent.set(ra, rb);
	};
	for (let i = 0; i < entries.length; i += 1) {
		for (let j = i + 1; j < entries.length; j += 1) {
			const left = entries[i].vector;
			const right = entries[j].vector;
			if (left && right && cosineIndexScore(left, right) >= threshold) union(entries[i].id, entries[j].id);
		}
	}
	const root = find(ids[0]);
	return ids.filter((id) => find(id) === root).sort();
}

/**
 * Validate a planner submission without trusting its winner, payload, or
 * similarity scores. The Worker calls this after re-reading current entries
 * and vectors. A candidate must be one complete connected component of the
 * current in-memory graph; a stale or incomplete plan is rejected.
 */
export function validateCandidateCluster(
	entries: MaintenanceEntry[],
	candidateIds: string[],
	threshold: number,
	maxClusterSize: number,
): CandidateValidationResult {
	const ids = [...new Set(candidateIds)];
	if (ids.length < 2) return { ok: false, reason: "candidate_cluster_too_small" };
	if (ids.length > maxClusterSize) return { ok: false, reason: "candidate_cluster_oversized" };
	if (entries.length !== ids.length || entries.some((entry) => entry.archived === true)) {
		return { ok: false, reason: "candidate_entries_not_current" };
	}
	if (new Set(entries.map((entry) => entry.id)).size !== ids.length) {
		return { ok: false, reason: "candidate_entries_not_unique" };
	}
	if (new Set(entries.map((entry) => entry.type)).size !== 1) {
		return { ok: false, reason: "candidate_cross_type" };
	}
	if (entries.some((entry) => !entry.vector || entry.vector.length === 0)) {
		return { ok: false, reason: "candidate_vector_missing" };
	}
	const component = connectedComponent(entries, threshold);
	if (component.length !== ids.length || component.some((id) => !ids.includes(id))) {
		return { ok: false, reason: "candidate_component_incomplete", component };
	}
	return { ok: true, component };
}

export function protectedLoserIds(
	duplicates: MaintenanceEntry[],
	protectedTypes: readonly string[] = DEFAULT_PROTECTED_CONTEXT_TYPES,
): string[] {
	const protectedSet = new Set(protectedTypes);
	return duplicates
		.filter((entry) => entry.contextType !== null && entry.contextType !== undefined && protectedSet.has(entry.contextType))
		.map((entry) => entry.id);
}

export function assertAutomaticMergeAllowed(
	duplicates: MaintenanceEntry[],
	protectedTypes: readonly string[] = DEFAULT_PROTECTED_CONTEXT_TYPES,
): void {
	const protectedLosers = protectedLoserIds(duplicates, protectedTypes);
	if (protectedLosers.length > 0) {
		throw new Error(`protected_type_requires_approval:${protectedLosers.join(",")}`);
	}
}

export interface EmbeddingIntent {
	entryId: string;
	redisRevision: number;
	embeddingModel: string;
	embeddingDimensions: number;
	embeddingInputSha256: string;
}

export function requiresEmbeddingRefresh(
	before: { embeddingInputSha256?: string | null; embeddingModel?: string | null; embeddingDimensions?: number | null },
	after: { embeddingInputSha256: string; embeddingModel: string; embeddingDimensions: number },
): boolean {
	return before.embeddingInputSha256 !== after.embeddingInputSha256 ||
		before.embeddingModel !== after.embeddingModel ||
		before.embeddingDimensions !== after.embeddingDimensions;
}
