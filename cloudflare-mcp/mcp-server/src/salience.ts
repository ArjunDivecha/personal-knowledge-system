import memoryPolicy from "../../../shared/memory_policy.json";
import salienceFixtures from "../../../shared/salience_fixtures.json";

type JsonRecord = Record<string, unknown>;

export const MEMORY_POLICY = memoryPolicy;
export const SALIENCE_FIXTURES = salienceFixtures;

function toNumber(value: unknown): number | null {
	if (typeof value === "number" && Number.isFinite(value)) {
		return value;
	}
	if (typeof value === "string" && value.trim() !== "") {
		const parsed = Number(value);
		return Number.isFinite(parsed) ? parsed : null;
	}
	return null;
}

function toDate(value: unknown): Date | null {
	if (typeof value !== "string" || value.length === 0) {
		return null;
	}
	const parsed = new Date(value);
	return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function defaultInjectionTier(contextType: unknown): 1 | 2 | 3 {
	if (typeof contextType === "string") {
		const tier = MEMORY_POLICY.default_injection_tier_by_context_type[contextType as keyof typeof MEMORY_POLICY.default_injection_tier_by_context_type];
		if (tier === 1 || tier === 2 || tier === 3) {
			return tier;
		}
	}
	return 3;
}

export function resolveStoredInjectionTier(metadata: JsonRecord | null | undefined): 1 | 2 | 3 {
	const tier = toNumber(metadata?.injection_tier);
	if (tier === 1 || tier === 2 || tier === 3) {
		return tier;
	}
	return defaultInjectionTier(metadata?.context_type);
}

export function computeSalience(entry: JsonRecord, now: Date = new Date()): number {
	const metadata = (entry.metadata as JsonRecord | undefined) ?? {};
	const contextType =
		typeof metadata.context_type === "string" ? metadata.context_type : "task_query";
	const mentionCount = Math.max(1, Math.trunc(toNumber(metadata.mention_count) ?? 1));
	const confidenceRaw = typeof entry.confidence === "string" ? entry.confidence : "medium";
	const confidence =
		MEMORY_POLICY.confidence_map[confidenceRaw as keyof typeof MEMORY_POLICY.confidence_map] ??
		MEMORY_POLICY.confidence_map.medium;

	const lastSeenValue = metadata.last_seen ?? metadata.updated_at;
	const lastSeen = toDate(lastSeenValue) ?? now;
	const halfLifeRaw =
		MEMORY_POLICY.half_lives_days[contextType as keyof typeof MEMORY_POLICY.half_lives_days] ??
		MEMORY_POLICY.half_lives_days.task_query;

	let decay = 1.0;
	if (halfLifeRaw !== "infinity") {
		const halfLife = Number(halfLifeRaw);
		const daysSince = Math.max(0, (now.getTime() - lastSeen.getTime()) / 86400000);
		decay = 0.5 ** (daysSince / halfLife);
	}

	const freqBoost = Math.min(1.0, Math.log1p(mentionCount) / Math.log1p(20));
	const typeMultiplier =
		MEMORY_POLICY.type_multipliers[contextType as keyof typeof MEMORY_POLICY.type_multipliers] ??
		MEMORY_POLICY.type_multipliers.task_query;

	let retrievalBoost = 0.0;
	const lastAccessed = toDate(metadata.last_accessed);
	if (lastAccessed) {
		const daysSinceRetrieved = Math.max(0, (now.getTime() - lastAccessed.getTime()) / 86400000);
		retrievalBoost = 0.15 * (0.5 ** (daysSinceRetrieved / 60));
	}

	const raw = confidence * decay * typeMultiplier * freqBoost + retrievalBoost;
	return Math.round(Math.min(1.0, raw) * 10000) / 10000;
}

export function deriveSearchTier(
	entry: JsonRecord,
	similarity: number,
): 1 | 2 | 3 {
	const metadata = (entry.metadata as JsonRecord | undefined) ?? {};
	const storedTier = resolveStoredInjectionTier(metadata);
	if (storedTier === 2 && similarity < MEMORY_POLICY.tier_rules.tier_2_similarity_min) {
		return 3;
	}
	return storedTier;
}

export function getTierMultiplier(tier: 1 | 2 | 3): number {
	return MEMORY_POLICY.search_tier_multipliers[String(tier) as "1" | "2" | "3"];
}

export function getSourceWeightFromMetadata(metadata: JsonRecord | null | undefined): number {
	const sourceWeights = metadata?.source_weights;
	if (sourceWeights && typeof sourceWeights === "object" && !Array.isArray(sourceWeights)) {
		let bestWeight = 1.0;
		for (const sourceType of Object.keys(sourceWeights)) {
			const configuredWeight =
				MEMORY_POLICY.source_type_weights[sourceType as keyof typeof MEMORY_POLICY.source_type_weights];
			if (typeof configuredWeight === "number") {
				bestWeight = Math.max(bestWeight, configuredWeight);
			}
		}
		return bestWeight;
	}

	const sourceRaw = typeof metadata?.source === "string" ? metadata.source.toLowerCase() : "";
	if (sourceRaw.includes("gmail") || sourceRaw.includes("email") || sourceRaw.includes("mbox")) {
		return MEMORY_POLICY.source_type_weights.email;
	}
	if (sourceRaw.includes("readme")) {
		return MEMORY_POLICY.source_type_weights.github_readme;
	}
	if (sourceRaw.includes("github") || sourceRaw.includes("repo")) {
		return MEMORY_POLICY.source_type_weights.github_commits;
	}
	return 1.0;
}

export function computeSearchScore(params: {
	similarity: number;
	recency: number;
	salience: number;
	tier: 1 | 2 | 3;
	sourceWeight: number;
}): number {
	const weights = MEMORY_POLICY.search_scoring_weights;
	const base =
		params.similarity * weights.semantic +
		params.recency * weights.recency +
		params.salience * weights.salience;
	const adjusted = base * getTierMultiplier(params.tier) * params.sourceWeight;
	return Math.round(adjusted * 10000) / 10000;
}
