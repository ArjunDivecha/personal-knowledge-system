// =============================================================================
// PHASE 8 — LIVE RETRIEVAL CONTRACT
// =============================================================================
// Deterministic read-path scoring for the live Worker search route. This mirrors
// the offline Phase 8 harness while consuming the current Redis entry shape.
// =============================================================================

export type Phase8Intent =
	| "current_answer"
	| "evidence_history"
	| "point_in_time"
	| "procedural_policy";

export type Phase8TemporalMode = "current" | "history" | "point_in_time" | "policy";

export interface Phase8QueryIntent {
	intent: Phase8Intent;
	temporal_mode: Phase8TemporalMode;
	matched_terms: string[];
	as_of: string | null;
}

export interface Phase8CandidateScores {
	final_score: number;
	lexical_score: number;
	entity_score: number;
	vector_score: number;
	temporal_score: number;
	lane_score: number;
	source_priority_score: number;
	score_multiplier: number;
	reasons: string[];
}

export interface Phase8CandidateInput {
	entry: Record<string, unknown>;
	metadata?: Record<string, unknown>;
	label?: string | null;
	summary?: string | null;
	entryType?: "knowledge" | "project";
	vectorScore?: number;
	now?: Date;
}

const STOPWORDS = new Set([
	"a",
	"about",
	"and",
	"are",
	"as",
	"can",
	"did",
	"do",
	"does",
	"for",
	"from",
	"have",
	"how",
	"in",
	"is",
	"it",
	"now",
	"of",
	"on",
	"or",
	"our",
	"that",
	"the",
	"this",
	"to",
	"was",
	"what",
	"when",
	"where",
	"which",
	"why",
]);

const EVIDENCE_TERMS = [
	"audit",
	"auditable",
	"changed",
	"evidence",
	"history",
	"origin",
	"source",
	"sources",
	"support",
	"why",
];

const POLICY_TERMS = [
	"agents.md",
	"guardrail",
	"policy",
	"procedure",
	"procedural",
	"rule",
	"rules",
];

const POINT_IN_TIME_TERMS = [
	"as of",
	"back then",
	"previously",
	"used to",
	"was true",
];

const MONTHS: Record<string, number> = {
	january: 1,
	february: 2,
	march: 3,
	april: 4,
	may: 5,
	june: 6,
	july: 7,
	august: 8,
	september: 9,
	october: 10,
	november: 11,
	december: 12,
};

export function classifyPhase8Query(query: string): Phase8QueryIntent {
	const lowered = query.toLowerCase();
	const asOf = extractAsOfDate(lowered);
	if (asOf || containsPhrase(lowered, POINT_IN_TIME_TERMS)) {
		return {
			intent: "point_in_time",
			temporal_mode: "point_in_time",
			matched_terms: dedupe([
				...(asOf ? [`as_of:${asOf}`] : []),
				...matchedTerms(lowered, POINT_IN_TIME_TERMS),
			]),
			as_of: asOf,
		};
	}

	const policyMatches = matchedTerms(lowered, POLICY_TERMS);
	if (policyMatches.length > 0) {
		return {
			intent: "procedural_policy",
			temporal_mode: "policy",
			matched_terms: policyMatches,
			as_of: null,
		};
	}

	const evidenceMatches = matchedTerms(lowered, EVIDENCE_TERMS);
	if (evidenceMatches.length > 0) {
		return {
			intent: "evidence_history",
			temporal_mode: "history",
			matched_terms: evidenceMatches,
			as_of: null,
		};
	}

	return {
		intent: "current_answer",
		temporal_mode: "current",
		matched_terms: [],
		as_of: null,
	};
}

export function scorePhase8Candidate(
	query: string,
	queryIntent: Phase8QueryIntent,
	input: Phase8CandidateInput,
): Phase8CandidateScores {
	const entry = input.entry;
	const metadata = input.metadata ?? getMetadata(entry);
	const text = candidateText(entry, metadata, input.label, input.summary);
	const lexicalScore = tokenOverlap(tokenize(query), text);
	const entityScore = entityOverlap(extractEntities(query), extractEntities(text));
	const vectorScore = clampScore(input.vectorScore ?? 0);
	const temporalScore = temporalScoreFor(queryIntent, text, entry, metadata, input.now ?? new Date());
	const laneScore = laneScoreFor(queryIntent, entry, metadata, input.entryType);
	const sourcePriorityScore = sourcePriorityFor(queryIntent, entry, metadata, input.entryType);
	const weights = weightsFor(queryIntent.intent);

	const finalScore = clampScore(
		weights.lexical * lexicalScore +
		weights.entity * entityScore +
		weights.vector * vectorScore +
		weights.temporal * temporalScore +
		weights.lane * laneScore +
		weights.source * sourcePriorityScore,
	);

	return {
		final_score: finalScore,
		lexical_score: lexicalScore,
		entity_score: entityScore,
		vector_score: vectorScore,
		temporal_score: temporalScore,
		lane_score: laneScore,
		source_priority_score: sourcePriorityScore,
		score_multiplier: phase8Multiplier(finalScore),
		reasons: scoreReasons(queryIntent, {
			lexicalScore,
			entityScore,
			vectorScore,
			temporalScore,
			laneScore,
			sourcePriorityScore,
			entry,
			metadata,
			entryType: input.entryType,
		}),
	};
}

function getMetadata(entry: Record<string, unknown>): Record<string, unknown> {
	const raw = entry.metadata;
	return raw && typeof raw === "object" && !Array.isArray(raw)
		? (raw as Record<string, unknown>)
		: {};
}

function candidateText(
	entry: Record<string, unknown>,
	metadata: Record<string, unknown>,
	label?: string | null,
	summary?: string | null,
): string {
	const parts: string[] = [];
	for (const value of [
		label,
		summary,
		entry.domain,
		entry.name,
		entry.current_view,
		entry.goal,
		entry.goal_summary,
		entry.current_phase,
		metadata.context_type,
		metadata.source,
		metadata.source_type,
		metadata.github_repo,
		metadata.artifact_path,
	]) {
		if (typeof value === "string" && value.trim()) {
			parts.push(value);
		}
	}
	return parts.join("\n");
}

function extractAsOfDate(query: string): string | null {
	const match = query.match(/\b(?:as\s+of|on|at)\s+(20\d{2}-\d{2}-\d{2})\b/);
	return match?.[1] ?? null;
}

function containsPhrase(text: string, phrases: string[]): boolean {
	return phrases.some((phrase) => text.includes(phrase));
}

function matchedTerms(text: string, terms: string[]): string[] {
	return terms.filter((term) => text.includes(term)).sort();
}

function normalizeText(text: string): string {
	return text.toLowerCase().replace(/[^\p{L}\p{N}\s/_:.-]/gu, " ").replace(/\s+/g, " ").trim();
}

function tokenize(text: string): string[] {
	const rawTokens = normalizeText(text).match(/[a-z0-9]+(?:[-_/.:][a-z0-9]+)*/g) ?? [];
	const tokens: string[] = [];
	for (const raw of rawTokens) {
		const parts = raw.split(/[-_/.:]+/).filter(Boolean);
		for (const token of [raw, ...parts]) {
			if (!STOPWORDS.has(token) && token.length > 1 && !tokens.includes(token)) {
				tokens.push(token);
			}
		}
	}
	return tokens;
}

function tokenOverlap(queryTokens: string[], text: string): number {
	if (queryTokens.length === 0) return 0;
	const candidateTokens = new Set(tokenize(text));
	if (candidateTokens.size === 0) return 0;
	const matches = queryTokens.filter((token) => candidateTokens.has(token));
	return clampScore(matches.length / queryTokens.length);
}

function extractEntities(text: string): string[] {
	const entities: string[] = [];
	for (const match of text.matchAll(/`([^`]+)`/g)) {
		const value = normalizeEntity(match[1]);
		if (value) entities.push(value);
	}
	for (const match of text.matchAll(/\b[A-Z][A-Z0-9]{1,9}\b/g)) {
		const value = normalizeEntity(match[0]);
		if (value) entities.push(value);
	}
	for (const match of text.matchAll(/\b[A-Z][a-z]+(?:\s+\d+[A-Z]?)?(?:\s+[A-Z][a-z]+){0,3}\b/g)) {
		const value = normalizeEntity(match[0]);
		if (value) entities.push(value);
	}
	return dedupe(entities);
}

function normalizeEntity(value: string): string | null {
	const normalized = value.toLowerCase().trim().replace(/\s+/g, " ");
	if (normalized.length < 2 || STOPWORDS.has(normalized)) {
		return null;
	}
	return normalized;
}

function entityOverlap(queryEntities: string[], candidateEntities: string[]): number {
	if (queryEntities.length === 0) return 0;
	const candidateSet = new Set(candidateEntities);
	const matches = queryEntities.filter((entity) => candidateSet.has(entity));
	return clampScore(matches.length / queryEntities.length);
}

function temporalScoreFor(
	intent: Phase8QueryIntent,
	text: string,
	entry: Record<string, unknown>,
	metadata: Record<string, unknown>,
	now: Date,
): number {
	const temporalStatus = stringValue(metadata.temporal_status) ?? stringValue(entry.temporal_status);
	const state = stringValue(entry.state) ?? stringValue(entry.status);
	const window = temporalWindow(metadata, text, now);

	if (intent.intent === "current_answer") {
		if (temporalStatus === "expired" || temporalStatus === "historical" || state === "stale") {
			return 0.05;
		}
		if (window.status === "expired") return 0.08;
		if (window.status === "future") return 0.62;
		return 1.0;
	}

	if (intent.intent === "point_in_time") {
		if (isValidAt(window, intent.as_of)) return 1.0;
		if (temporalStatus === "historical" || temporalStatus === "expired" || window.status === "expired") {
			return 0.72;
		}
		return 0.42;
	}

	if (intent.intent === "evidence_history") {
		if (hasEvidence(metadata)) return 1.0;
		if (temporalStatus === "historical" || state === "contested") return 0.84;
		return 0.68;
	}

	return isPolicyLike(text, metadata) ? 1.0 : 0.45;
}

function laneScoreFor(
	intent: Phase8QueryIntent,
	entry: Record<string, unknown>,
	metadata: Record<string, unknown>,
	entryType?: "knowledge" | "project",
): number {
	const contextType = stringValue(metadata.context_type);
	const text = candidateText(entry, metadata);
	if (intent.intent === "procedural_policy") {
		if (isPolicyLike(text, metadata)) return 1.0;
		if (contextType === "active_project") return 0.65;
		return 0.42;
	}
	if (intent.intent === "evidence_history") {
		if (hasEvidence(metadata)) return 0.95;
		return 0.68;
	}
	if (intent.intent === "point_in_time") {
		if (hasEvidence(metadata)) return 0.82;
		return 0.66;
	}
	if (entryType === "project" || contextType === "active_project") return 0.92;
	if (contextType === "professional_identity" || contextType === "stated_preference") return 1.0;
	if (isPolicyLike(text, metadata)) return 0.55;
	return 0.78;
}

function sourcePriorityFor(
	intent: Phase8QueryIntent,
	entry: Record<string, unknown>,
	metadata: Record<string, unknown>,
	entryType?: "knowledge" | "project",
): number {
	const state = stringValue(entry.state) ?? stringValue(entry.status);
	if (intent.intent === "current_answer") {
		if (state === "stale" || state === "contested") return 0.40;
		if (entryType === "project") return 0.92;
		return 1.0;
	}
	if (intent.intent === "evidence_history" || intent.intent === "point_in_time") {
		if (hasEvidence(metadata)) return 1.0;
		if (state === "contested") return 0.82;
		return 0.66;
	}
	if (isPolicyLike(candidateText(entry, metadata), metadata)) return 1.0;
	return 0.55;
}

function weightsFor(intent: Phase8Intent): Record<"lexical" | "entity" | "vector" | "temporal" | "lane" | "source", number> {
	if (intent === "point_in_time") {
		return { lexical: 0.25, entity: 0.10, vector: 0.10, temporal: 0.30, lane: 0.05, source: 0.20 };
	}
	if (intent === "evidence_history") {
		return { lexical: 0.30, entity: 0.10, vector: 0.15, temporal: 0.15, lane: 0.05, source: 0.25 };
	}
	if (intent === "procedural_policy") {
		return { lexical: 0.25, entity: 0.10, vector: 0.10, temporal: 0.15, lane: 0.15, source: 0.25 };
	}
	return { lexical: 0.35, entity: 0.15, vector: 0.15, temporal: 0.10, lane: 0.05, source: 0.20 };
}

function phase8Multiplier(score: number): number {
	return round4(0.65 + clampScore(score) * 0.70);
}

function scoreReasons(
	intent: Phase8QueryIntent,
	scores: {
		lexicalScore: number;
		entityScore: number;
		vectorScore: number;
		temporalScore: number;
		laneScore: number;
		sourcePriorityScore: number;
		entry: Record<string, unknown>;
		metadata: Record<string, unknown>;
		entryType?: "knowledge" | "project";
	},
): string[] {
	const reasons: string[] = [];
	if (scores.lexicalScore > 0) reasons.push("lexical_match");
	if (scores.entityScore > 0) reasons.push("entity_match");
	if (scores.vectorScore > 0) reasons.push("vector_score");
	if (scores.temporalScore >= 0.9) reasons.push(`${intent.temporal_mode}_temporal_fit`);
	if (scores.laneScore >= 0.9) reasons.push("lane_fit");
	if (scores.sourcePriorityScore >= 0.9) {
		if (intent.intent === "procedural_policy") reasons.push("policy_pointer_preferred");
		else if (intent.intent === "evidence_history") reasons.push("evidence_source_preferred");
		else if (intent.intent === "point_in_time") reasons.push("point_in_time_source_fit");
		else reasons.push("current_surface_preferred");
	}
	return dedupe(reasons);
}

function temporalWindow(
	metadata: Record<string, unknown>,
	text: string,
	now: Date,
): { validFrom: Date | null; validTo: Date | null; status: "unknown" | "current" | "future" | "expired" } {
	const explicitFrom = parseDateLike(metadata.valid_from);
	const explicitTo = parseDateLike(metadata.valid_to);
	if (explicitFrom || explicitTo) {
		return classifyWindow(explicitFrom, explicitTo, now);
	}

	const lowered = text.toLowerCase();
	const monthMatch = lowered.match(/\b(?:in\s+)?(january|february|march|april|may|june|july|august|september|october|november|december)\s*(20\d{2})?\b/);
	if (!monthMatch) {
		return { validFrom: null, validTo: null, status: "unknown" };
	}
	const month = MONTHS[monthMatch[1]];
	let year = monthMatch[2] ? Number(monthMatch[2]) : now.getUTCFullYear();
	if (!monthMatch[2] && month < now.getUTCMonth() + 1) {
		year += 1;
	}
	const validFrom = new Date(Date.UTC(year, month - 1, 1));
	const validTo = new Date(Date.UTC(year, month, 0));
	return classifyWindow(validFrom, validTo, now);
}

function classifyWindow(
	validFrom: Date | null,
	validTo: Date | null,
	now: Date,
): { validFrom: Date | null; validTo: Date | null; status: "unknown" | "current" | "future" | "expired" } {
	const start = validFrom ?? validTo;
	const end = validTo ?? validFrom;
	if (!start || !end) return { validFrom, validTo, status: "unknown" };
	const today = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
	if (Date.UTC(end.getUTCFullYear(), end.getUTCMonth(), end.getUTCDate()) < today) {
		return { validFrom, validTo, status: "expired" };
	}
	if (Date.UTC(start.getUTCFullYear(), start.getUTCMonth(), start.getUTCDate()) > today) {
		return { validFrom, validTo, status: "future" };
	}
	return { validFrom, validTo, status: "current" };
}

function isValidAt(window: { validFrom: Date | null; validTo: Date | null }, asOf: string | null): boolean {
	const target = parseDateLike(asOf);
	if (!target) return false;
	const start = window.validFrom ?? window.validTo;
	const end = window.validTo ?? window.validFrom;
	if (!start || !end) return false;
	const targetDay = Date.UTC(target.getUTCFullYear(), target.getUTCMonth(), target.getUTCDate());
	const startDay = Date.UTC(start.getUTCFullYear(), start.getUTCMonth(), start.getUTCDate());
	const endDay = Date.UTC(end.getUTCFullYear(), end.getUTCMonth(), end.getUTCDate());
	return startDay <= targetDay && targetDay <= endDay;
}

function parseDateLike(value: unknown): Date | null {
	if (typeof value !== "string" || !value.trim()) return null;
	const parsed = new Date(value);
	return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function hasEvidence(metadata: Record<string, unknown>): boolean {
	return (
		arrayLength(metadata.source_conversations) > 0 ||
		arrayLength(metadata.source_messages) > 0 ||
		arrayLength(metadata.message_ids) > 0 ||
		typeof metadata.evidence === "object"
	);
}

function arrayLength(value: unknown): number {
	return Array.isArray(value) ? value.length : 0;
}

function isPolicyLike(text: string, metadata: Record<string, unknown>): boolean {
	const lowered = `${text}\n${stringValue(metadata.artifact_path) ?? ""}`.toLowerCase();
	return POLICY_TERMS.some((term) => lowered.includes(term)) || lowered.includes("agents.md");
}

function stringValue(value: unknown): string | null {
	return typeof value === "string" && value.length > 0 ? value : null;
}

function clampScore(value: number): number {
	if (!Number.isFinite(value)) return 0;
	return Math.round(Math.max(0, Math.min(value, 1)) * 1_000_000) / 1_000_000;
}

function round4(value: number): number {
	return Math.round(value * 10000) / 10000;
}

function dedupe(values: string[]): string[] {
	const result: string[] = [];
	for (const value of values) {
		if (value && !result.includes(value)) {
			result.push(value);
		}
	}
	return result;
}
