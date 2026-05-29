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

function toStringArray(value: unknown): string[] {
	if (!Array.isArray(value)) {
		return [];
	}
	return value.filter((item): item is string => typeof item === "string");
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
	const signalMultiplier = toStringArray(metadata.signal_flags).reduce(
		(multiplier, flag) => {
			const configured =
				MEMORY_POLICY.signal_flag_multipliers[
					flag as keyof typeof MEMORY_POLICY.signal_flag_multipliers
				];
			return multiplier * (typeof configured === "number" ? configured : 1.0);
		},
		1.0,
	);
	const combinedMultiplier = Math.min(
		MEMORY_POLICY.max_combined_salience_multiplier ?? 3.0,
		typeMultiplier * signalMultiplier,
	);

	let retrievalBoost = 0.0;
	const lastAccessed = toDate(metadata.last_accessed);
	if (lastAccessed) {
		const daysSinceRetrieved = Math.max(0, (now.getTime() - lastAccessed.getTime()) / 86400000);
		retrievalBoost = 0.15 * 0.5 ** (daysSinceRetrieved / 60);
	}

	const updatedAtAgeDays = Math.max(0, (now.getTime() - lastSeen.getTime()) / 86400000);
	// Phase 2: continuous lever is MULTIPLICATIVE on the base so it spreads the
	// populated salience bands without lifting genuinely-low-salience items
	// over the archive/decay thresholds (keeps those thresholds valid).
	const richness = computeRichness(entry, metadata, updatedAtAgeDays);
	const richnessWeight = getRichnessWeight();
	const base = confidence * decay * combinedMultiplier * freqBoost;

	const raw = base * (1 + richnessWeight * richness) + retrievalBoost;
	return Math.round(Math.min(1.0, raw) * 10000) / 10000;
}

// Phase 2 (PRD R2.3): continuous "evidence richness" term so salience
// discriminates even when mention_count is pinned at 1. Must stay in lockstep
// with compute_salience in distillation/utils/salience.py; all constants live
// in memory_policy.json (salience_continuous).
function saturating(n: number, cap: number): number {
	if (cap <= 0) return 0;
	const capped = Math.max(0, Math.min(n, cap));
	return Math.log1p(capped) / Math.log1p(cap);
}

function listLength(value: unknown): number {
	return Array.isArray(value) ? value.length : 0;
}

export function getRichnessWeight(): number {
	const cfg = (MEMORY_POLICY as JsonRecord).salience_continuous as JsonRecord | undefined;
	if (!cfg || cfg.enabled !== true) return 0;
	return toNumber(cfg.weight) ?? 0;
}

// Returns the richness scalar in [0,1] (the multiplicative lever applies
// getRichnessWeight() * richness as a (1 + ...) factor on the base).
export function computeRichness(
	entry: JsonRecord,
	metadata: JsonRecord,
	updatedAtAgeDays = 0,
): number {
	const cfg = (MEMORY_POLICY as JsonRecord).salience_continuous as JsonRecord | undefined;
	if (!cfg || cfg.enabled !== true) return 0;
	const components = (cfg.components as JsonRecord | undefined) ?? {};

	const sourceBreadth = new Set(
		toStringArray(metadata.source_conversations),
	).size;
	const keyInsights = listLength(entry.key_insights);
	const relatedLinks = listLength(entry.related_knowledge) + listLength(entry.related_repos);

	const part = (name: string, value: number): number => {
		const c = components[name] as JsonRecord | undefined;
		if (!c) return 0;
		const w = toNumber(c.weight) ?? 0;
		const cap = toNumber(c.cap) ?? 1;
		return w * saturating(value, cap);
	};

	// recency tiebreaker: continuous in [0,1], 1 for just-updated → 0 for old.
	const recencyCfg = components.recency_tiebreaker as JsonRecord | undefined;
	let recencyPart = 0;
	if (recencyCfg) {
		const w = toNumber(recencyCfg.weight) ?? 0;
		const hl = toNumber(recencyCfg.half_life_days) ?? 180;
		const recencyValue = hl > 0 ? 0.5 ** (updatedAtAgeDays / hl) : 0;
		recencyPart = w * recencyValue;
	}

	const richness =
		part("source_breadth", sourceBreadth) +
		part("key_insights", keyInsights) +
		part("related_links", relatedLinks) +
		recencyPart;

	return Math.max(0, Math.min(1, richness));
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
