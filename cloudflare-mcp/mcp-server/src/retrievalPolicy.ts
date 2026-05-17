// =============================================================================
// LAYER 0 — RETRIEVAL POLICY
// =============================================================================
// Query-time relevance shaping. The PRIMARY fix for the "Panchakarma surfacing
// in a quant chat" problem.
//
// Two mechanisms:
//   1. Cross-context penalty: an entry tagged with a different topic bucket
//      than the query is scored down (multiplied by CROSS_BUCKET_PENALTY).
//   2. Quarantine suppression: an entry flagged injection_quarantine=true is
//      scored down (multiplied by QUARANTINE_PENALTY).
//
// All effects are score multipliers — never hard filters. The user can always
// find a quarantined or cross-context entry by direct query / explicit search;
// they just don't auto-surface uninvited in unrelated conversations.
//
// Gated externally by env.RETRIEVAL_POLICY_MODE — when "off", helpers here
// return identity penalty (1.0) and the search path is unchanged.
//
// Topic classification is keyword-based for simplicity. A future iteration
// could call an LLM, but for ~3500 entries with a small bucket set the
// keyword approach is fast, free, and good enough to ship.
// =============================================================================

export type TopicBucket =
	| "coding_dev"
	| "finance_quant"
	| "personal_health"
	| "legal_california"
	| "geopolitics_macro"
	| "general";

export interface QueryIntent {
	bucket: TopicBucket;
	confidence: number;       // 0..1
	matchedKeywords: string[];
}

// Score multipliers applied at retrieval time.
// Same-bucket or low-confidence → 1.0 (no effect).
// Cross-bucket high-confidence → CROSS_BUCKET_PENALTY.
// Quarantined → QUARANTINE_PENALTY (regardless of bucket match).
export const CROSS_BUCKET_PENALTY = 0.3;
export const QUARANTINE_PENALTY = 0.2;
export const CONFIDENCE_THRESHOLD = 0.75;

// Per-bucket keyword lists. Order matters only for human readability;
// matching is whole-word case-insensitive against the full text.
// Keep lists tight — overlap weakens classification confidence.
const BUCKET_KEYWORDS: Record<Exclude<TopicBucket, "general">, string[]> = {
	coding_dev: [
		"code", "function", "bug", "refactor", "deploy", "commit", "pr",
		"pull request", "merge", "branch", "test", "typescript", "python",
		"javascript", "react", "node", "npm", "compiler", "lint", "stacktrace",
		"runtime", "exception", "regex", "schema", "api", "endpoint", "worker",
		"cloudflare", "docker", "kubernetes", "yaml", "json", "claude code",
		"codex", "vscode", "git",
	],
	finance_quant: [
		"trading", "portfolio", "alpha", "factor", "backtest", "sharpe",
		"sortino", "rotation", "country", "equity", "bond", "yield", "credit",
		"volatility", "options", "futures", "bloomberg", "rebalance",
		"signal", "macro", "rates", "fed", "ecb", "fx", "currency",
		"emerging market", "developed market", "asado", "loop pilot",
		"factor timing", "ic", "information coefficient", "regression",
	],
	personal_health: [
		"health", "doctor", "medicine", "exercise", "diet", "sleep",
		"yoga", "meditation", "panchakarma", "ayurveda", "ayurvedic",
		"supplement", "vitamin", "fitness", "wellness", "therapy",
		"workout", "running", "weight", "diabetes", "blood",
	],
	legal_california: [
		"california", "statute", "code section", "ccpa", "ccp",
		"california law", "court", "appellate", "litigation", "tort",
		"contract law", "civil procedure", "discovery", "subpoena",
		"deposition", "calbar", "rule of court",
	],
	geopolitics_macro: [
		"geopolitics", "war", "sanctions", "trade war", "election",
		"government", "policy", "regime", "putin", "xi", "china",
		"russia", "iran", "ukraine", "nato", "eu", "brexit", "tariff",
		"diplomacy", "treaty", "un security council",
	],
};

const KEYWORDS_BY_BUCKET = BUCKET_KEYWORDS as Record<
	Exclude<TopicBucket, "general">,
	string[]
>;

const ALL_BUCKETS: Exclude<TopicBucket, "general">[] = [
	"coding_dev",
	"finance_quant",
	"personal_health",
	"legal_california",
	"geopolitics_macro",
];

function normalizeText(text: string): string {
	return text.toLowerCase().replace(/[^\p{L}\p{N}\s]/gu, " ").replace(/\s+/g, " ").trim();
}

function countMatches(text: string, keyword: string): number {
	if (!keyword) return 0;
	const normalizedKeyword = normalizeText(keyword);
	if (!normalizedKeyword) return 0;
	let count = 0;
	let from = 0;
	while (from < text.length) {
		const i = text.indexOf(normalizedKeyword, from);
		if (i === -1) break;
		// crude word-boundary check
		const before = i === 0 || text[i - 1] === " ";
		const after =
			i + normalizedKeyword.length === text.length ||
			text[i + normalizedKeyword.length] === " ";
		if (before && after) count += 1;
		from = i + normalizedKeyword.length;
	}
	return count;
}

interface ScoredBucket {
	bucket: Exclude<TopicBucket, "general">;
	matches: number;
	matchedKeywords: string[];
}

function scoreText(text: string): ScoredBucket[] {
	const normalized = normalizeText(text);
	if (!normalized) {
		return ALL_BUCKETS.map((b) => ({ bucket: b, matches: 0, matchedKeywords: [] }));
	}
	return ALL_BUCKETS.map((bucket) => {
		const matched: string[] = [];
		let matches = 0;
		for (const kw of KEYWORDS_BY_BUCKET[bucket]) {
			const c = countMatches(normalized, kw);
			if (c > 0) {
				matches += c;
				matched.push(kw);
			}
		}
		return { bucket, matches, matchedKeywords: matched };
	});
}

/**
 * Classify a query into a topic bucket with a confidence in [0, 1].
 * Higher confidence = single-bucket dominance.
 * Low confidence = ambiguous (multiple buckets compete) or empty (no keywords).
 *
 * Confidence is computed as:
 *   confidence = clamp((top.matches - second.matches) / max(top.matches, 1), 0, 1)
 * with a floor when top.matches is 0.
 */
export function classifyQueryIntent(query: string): QueryIntent {
	const scored = scoreText(query).sort((a, b) => b.matches - a.matches);
	const top = scored[0];
	const second = scored[1] ?? { matches: 0, matchedKeywords: [] };
	if (!top || top.matches === 0) {
		return { bucket: "general", confidence: 0, matchedKeywords: [] };
	}
	const raw = (top.matches - second.matches) / Math.max(top.matches, 1);
	const confidence = Math.max(0, Math.min(1, raw));
	return { bucket: top.bucket, confidence, matchedKeywords: top.matchedKeywords };
}

/**
 * Classify an entry into a topic bucket. Uses any combination of label,
 * domain, source, and project fields available on the entry to compose
 * a text bag. Returns "general" when nothing matches strongly.
 */
export function classifyEntryTopic(params: {
	label?: string | null;
	domain?: string | null;
	source?: string | null;
	project?: string | null;
	githubRepo?: string | null;
	currentView?: string | null;
}): TopicBucket {
	const parts: string[] = [];
	if (params.label) parts.push(params.label);
	if (params.domain) parts.push(params.domain);
	if (params.currentView) parts.push(params.currentView);
	if (params.project) parts.push(params.project);
	if (params.githubRepo) parts.push(params.githubRepo);
	if (params.source) parts.push(params.source);
	// source pathway hints — agent sessions are almost always coding
	const source = (params.source ?? "").toLowerCase();
	if (
		source.includes("claude_code") ||
		source.includes("codex_cli") ||
		source.includes("github")
	) {
		// give a strong nudge but still rely on text-based scoring as primary signal
		parts.push("code function commit pr");
	}
	const text = parts.join(" ");
	const scored = scoreText(text).sort((a, b) => b.matches - a.matches);
	const top = scored[0];
	if (!top || top.matches === 0) return "general";
	return top.bucket;
}

/**
 * Compute the multiplier to apply to an entry's final_score based on the
 * query intent and the entry's topic bucket.
 *
 * Rules:
 *   - mode === "off"               → 1.0 (Layer 0 disabled)
 *   - queryIntent.confidence < t   → 1.0 (uncertain → don't shape)
 *   - same bucket                  → 1.0
 *   - "general" on either side     → 1.0 (no opinion = no penalty)
 *   - cross-bucket confident       → CROSS_BUCKET_PENALTY
 */
export function computeCrossContextPenalty(params: {
	mode: "off" | "on" | undefined;
	queryIntent: QueryIntent;
	entryBucket: TopicBucket;
	threshold?: number;
}): number {
	const { mode, queryIntent, entryBucket } = params;
	if (mode !== "on") return 1.0;
	const threshold = params.threshold ?? CONFIDENCE_THRESHOLD;
	if (queryIntent.confidence < threshold) return 1.0;
	if (queryIntent.bucket === "general" || entryBucket === "general") return 1.0;
	if (queryIntent.bucket === entryBucket) return 1.0;
	return CROSS_BUCKET_PENALTY;
}

/**
 * Compute the multiplier for an entry that is flagged injection_quarantine.
 * Only applied when retrieval policy is "on" — quarantine has no effect when
 * Layer 0 is disabled. (Quarantine without Layer 0 is just an opaque flag.)
 */
export function computeQuarantinePenalty(params: {
	mode: "off" | "on" | undefined;
	isQuarantined: boolean;
}): number {
	if (params.mode !== "on") return 1.0;
	if (!params.isQuarantined) return 1.0;
	return QUARANTINE_PENALTY;
}
