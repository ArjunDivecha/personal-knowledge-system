import type { Redis } from "@upstash/redis/cloudflare";
import type { Index } from "@upstash/vector";

export const SOURCE_FIRST_CURRENT_KEY = "sf:current_generation";
export const SOURCE_FIRST_HEARTBEAT_KEY = "sf:heartbeat";
export const SOURCE_FIRST_MIN_FINAL_SCORE = 0.65;

export interface SourceFirstFreshness {
	status: "fresh" | "stale" | "missing" | "invalid";
	age_seconds: number | null;
	max_age_seconds: number;
	as_of: string | null;
}

export interface SourceFirstEvidence {
	id: string;
	source_id?: string;
	title: string;
	text: string;
	source_path: string;
	source_kind: string;
	project: string | null;
	source_modified_at: string;
	content_checksum: string;
	chunk_index: number;
	chunk_count: number;
	authority: number;
	evidence_role?: "authoritative" | "working_context";
	session_surface?: "claude_code" | "codex" | null;
	session_id?: string | null;
	session_started_at?: string | null;
	session_ended_at?: string | null;
	attention_observed_at?: string | null;
	pinned: boolean;
}

export interface SuppressionRule {
	id: string;
	terms?: string[];
	source_path_contains?: string[];
	reason?: string;
	allow_explicit_query?: boolean;
}

export interface SourceFirstSearchResult extends SourceFirstEvidence {
	similarity_score: number;
	lexical_score: number;
	recency_score: number;
	base_score: number;
	attention_score: number;
	working_context_bonus: number;
	final_score: number;
	explicit_project_match: boolean;
	exact_identifier_match: boolean;
	exact_identifier_count: number;
	exact_lexical_match: boolean;
	duplicate_count: number;
	alternate_sources: Array<{
		project: string | null;
		source_path: string;
		source_modified_at: string;
	}>;
}

function parseObject(raw: unknown): Record<string, unknown> | null {
	if (raw && typeof raw === "object" && !Array.isArray(raw)) return raw as Record<string, unknown>;
	if (typeof raw !== "string") return null;
	try {
		const parsed = JSON.parse(raw);
		return parsed && typeof parsed === "object" && !Array.isArray(parsed)
			? parsed as Record<string, unknown>
			: null;
	} catch {
		return null;
	}
}

function parseEvidence(raw: unknown): SourceFirstEvidence | null {
	const value = parseObject(raw);
	if (!value || typeof value.id !== "string" || typeof value.text !== "string") return null;
	return value as unknown as SourceFirstEvidence;
}

function parseRules(raw: unknown): SuppressionRule[] {
	const value = parseObject(raw);
	return Array.isArray(value?.rules)
		? value.rules.filter((rule): rule is SuppressionRule => Boolean(rule && typeof rule === "object" && typeof rule.id === "string"))
		: [];
}

function parseProjects(raw: unknown): Array<Record<string, unknown>> {
	try {
		const parsed = typeof raw === "string" ? JSON.parse(raw) : raw;
		return Array.isArray(parsed)
			? parsed.filter((value): value is Record<string, unknown> => Boolean(value && typeof value === "object"))
			: [];
	} catch {
		return [];
	}
}

function normalizedPhrase(value: string): string {
	return value
		.replace(/([a-z])([A-Z])/g, "$1 $2")
		.replace(/[_/-]+/g, " ")
		.toLowerCase()
		.replace(/[^a-z0-9]+/g, " ")
		.trim();
}

const GENERIC_PROJECT_NAMES = new Set([
	"data", "memory", "research", "system", "trading",
]);
const GENERIC_QUERY_ACRONYMS = new Set([
	"api", "http", "json", "llm", "ml", "pdf", "sql", "url",
]);

function queryAcronyms(query: string): string[] {
	return query.match(/\b[A-Z][A-Z0-9]{2,}\b/g)
		?.map((value) => value.toLowerCase())
		.filter((value) => !GENERIC_QUERY_ACRONYMS.has(value)) ?? [];
}

export function findExplicitProject(
	query: string,
	projects: Array<Record<string, unknown>>,
): Record<string, unknown> | null {
	const normalizedQuery = ` ${normalizedPhrase(query)} `;
	const candidates = projects
		.filter((project) => typeof project.id === "string" && typeof project.name === "string")
		.sort((left, right) => String(right.name).length - String(left.name).length);
	const acronyms = queryAcronyms(query);
	const exact = candidates.find((project) => {
		const projectName = normalizedPhrase(String(project.name));
		if (projectName.split(" ").length === 1 && GENERIC_PROJECT_NAMES.has(projectName)) return false;
		return normalizedQuery.includes(` ${projectName} `);
	});
	if (exact) return exact;

	// Source-first projects are often named more precisely than the way a user
	// asks about them (e.g. "T2 factor timing" for "T2 MEGA FACTOR TIMING V2",
	// or "law chatbot" for "California Law Chatbot"). Use a conservative
	// two-token / 60%-of-name overlap fallback to select that project's complete
	// evidence set without introducing a classifier or fuzzy global ranking.
	const queryTokens = new Set(tokens(query));
	return candidates.find((project) => {
		const nameTokens = tokens(String(project.name));
		if (nameTokens.length < 2) return false;
		if (acronyms.some((acronym) => !nameTokens.includes(acronym))) return false;
		const overlap = nameTokens.filter((token) => queryTokens.has(token)).length;
		return overlap >= 2 && overlap / nameTokens.length >= 0.6;
	}) ?? null;
}

const TOKEN_STOP_WORDS = new Set([
	"about", "and", "are", "architecture", "as", "at", "by", "current", "for",
	"from", "how", "in", "into", "is", "now", "of", "on", "please", "project",
	"recent", "status", "system", "tell", "to",
	"that", "the", "this", "was", "were", "what", "when", "where", "with",
]);

function orderedTokens(text: string): string[] {
	return (normalizedPhrase(text).match(/[a-z0-9]{2,}/g) ?? [])
		.filter((token) => !TOKEN_STOP_WORDS.has(token));
}

function tokens(text: string): string[] {
	return [...new Set(
		orderedTokens(text),
	)];
}

function hasStrongExactLexicalPhrase(query: string, evidence: SourceFirstEvidence): boolean {
	const needle = orderedTokens(query);
	if (needle.length < 3) return false;
	return [evidence.title, evidence.project ?? "", evidence.source_path, evidence.text]
		.some((field) => {
			const haystack = orderedTokens(field);
			for (let index = 0; index <= haystack.length - needle.length; index += 1) {
				if (needle.every((token, offset) => haystack[index + offset] === token)) return true;
			}
			return false;
		});
}

function queryIdentifierTerms(query: string): string[] {
	const rawTerms = query.match(/[A-Za-z0-9][A-Za-z0-9._/-]{2,}/g) ?? [];
	return [...new Set(rawTerms
		.filter((term) =>
			(/[0-9]/.test(term) && /[A-Za-z]/.test(term))
			|| /[._/-]/.test(term)
			|| /^[A-Z]{2,}$/.test(term)
			|| /[a-z][A-Z]/.test(term)
		)
		.flatMap((term) => normalizedPhrase(term).match(/[a-z0-9]{3,}/g) ?? []))];
}

export function lexicalOverlap(query: string, evidence: SourceFirstEvidence): number {
	const queryTokens = tokens(query);
	if (queryTokens.length === 0) return 0;
	const haystack = new Set(tokens(`${evidence.title}\n${evidence.project ?? ""}\n${evidence.source_path}\n${evidence.text}`));
	return queryTokens.filter((token) => haystack.has(token)).length / queryTokens.length;
}

export function sourceRecencyScore(sourceModifiedAt: string, now = new Date()): number {
	const timestamp = Date.parse(sourceModifiedAt);
	if (!Number.isFinite(timestamp)) return 0;
	const ageDays = Math.max(0, (now.getTime() - timestamp) / 86400000);
	return Math.exp(-Math.log(2) * ageDays / 180);
}

export function workingContextAttentionScore(
	evidence: SourceFirstEvidence,
	similarityScore: number,
	now = new Date(),
): number {
	if (evidence.evidence_role !== "working_context" || !evidence.attention_observed_at) return 0;
	const timestamp = Date.parse(evidence.attention_observed_at);
	if (!Number.isFinite(timestamp)) return 0;
	const ageDays = Math.max(0, (now.getTime() - timestamp) / 86400000);
	return Math.max(0, similarityScore) * Math.exp(-Math.log(2) * ageDays / 3);
}

export function isSuppressed(query: string, evidence: SourceFirstEvidence, rules: SuppressionRule[]): boolean {
	const normalizedQuery = query.toLowerCase();
	const recordText = `${evidence.title}\n${evidence.project ?? ""}\n${evidence.text}`.toLowerCase();
	const sourcePath = evidence.source_path.toLowerCase();
	for (const rule of rules) {
		const terms = (rule.terms ?? []).map((term) => term.toLowerCase()).filter(Boolean);
		if (rule.allow_explicit_query && terms.some((term) => normalizedQuery.includes(term))) continue;
		const termMatch = terms.some((term) => recordText.includes(term));
		const pathMatch = (rule.source_path_contains ?? [])
			.map((part) => part.toLowerCase())
			.some((part) => sourcePath.includes(part));
		if (termMatch || pathMatch) return true;
	}
	return false;
}

export function scoreSourceFirstResult(
	query: string,
	evidence: SourceFirstEvidence,
	similarityScore: number,
	now = new Date(),
): SourceFirstSearchResult {
	const lexicalScore = lexicalOverlap(query, evidence);
	const recencyScore = sourceRecencyScore(evidence.source_modified_at, now);
	const authority = Math.max(0, Math.min(1, Number(evidence.authority) || 0));
	const baseScore = (
		0.70 * similarityScore +
		0.15 * lexicalScore +
		0.10 * authority +
		0.05 * recencyScore
	);
	const attentionScore = workingContextAttentionScore(evidence, similarityScore, now);
	const workingContextBonus = 0.08 * attentionScore;
	const finalScore = Math.round(Math.min(1, baseScore + workingContextBonus) * 10000) / 10000;
	return {
		...evidence,
		evidence_role: evidence.evidence_role ?? "authoritative",
		session_surface: evidence.session_surface ?? null,
		session_id: evidence.session_id ?? null,
		attention_observed_at: evidence.attention_observed_at ?? null,
		similarity_score: similarityScore,
		lexical_score: Math.round(lexicalScore * 10000) / 10000,
		recency_score: Math.round(recencyScore * 10000) / 10000,
		base_score: Math.round(baseScore * 10000) / 10000,
		attention_score: Math.round(attentionScore * 10000) / 10000,
		working_context_bonus: Math.round(workingContextBonus * 10000) / 10000,
		final_score: finalScore,
		explicit_project_match: false,
		exact_identifier_match: false,
		exact_identifier_count: 0,
		exact_lexical_match: false,
		duplicate_count: 1,
		alternate_sources: [],
	};
}

export async function getSourceFirstGeneration(redis: Redis): Promise<string | null> {
	const value = await redis.get(SOURCE_FIRST_CURRENT_KEY);
	return typeof value === "string" && value ? value : null;
}

export async function getSourceFirstHeartbeat(redis: Redis): Promise<Record<string, unknown> | null> {
	return parseObject(await redis.get(SOURCE_FIRST_HEARTBEAT_KEY));
}

export function evaluateSourceFirstFreshness(
	manifest: Record<string, unknown> | null,
	heartbeat: Record<string, unknown> | null,
	now = new Date(),
	maxAgeSeconds = 36 * 60 * 60,
): SourceFirstFreshness {
	const asOf = (
		typeof heartbeat?.published_at === "string" ? heartbeat.published_at : null
	) || (
		typeof manifest?.published_at === "string" ? manifest.published_at : null
	) || (
		typeof manifest?.built_at === "string" ? manifest.built_at : null
	);
	if (!asOf) return { status: "missing", age_seconds: null, max_age_seconds: maxAgeSeconds, as_of: null };
	const timestamp = Date.parse(asOf);
	if (!Number.isFinite(timestamp)) {
		return { status: "invalid", age_seconds: null, max_age_seconds: maxAgeSeconds, as_of: asOf };
	}
	const ageSeconds = Math.max(0, Math.floor((now.getTime() - timestamp) / 1000));
	const heartbeatMatches = Boolean(heartbeat && heartbeat.generation === manifest?.generation);
	return {
		status: heartbeatMatches && ageSeconds <= maxAgeSeconds ? "fresh" : "stale",
		age_seconds: ageSeconds,
		max_age_seconds: maxAgeSeconds,
		as_of: asOf,
	};
}

export async function sourceFirstSearchGeneration(
	redis: Redis,
	vector: Index,
	queryEmbedding: number[],
	query: string,
	limit: number,
	generation: string,
): Promise<Record<string, unknown>> {
	const requested = Math.max(1, Math.min(limit, 20));
	const hits = await vector.query({
		vector: queryEmbedding,
		topK: Math.min(100, Math.max(30, requested * 10)),
		includeMetadata: false,
	}, { namespace: generation });
	const semanticIds = hits.map((hit) => String(hit.id));
	const [rawSuppressions, rawProjects] = await Promise.all([
		redis.get(`sf:${generation}:suppressions`),
		redis.get(`sf:${generation}:projects`),
	]);
	const explicitProject = findExplicitProject(query, parseProjects(rawProjects));
	let exactProjectIds: string[] = [];
	if (explicitProject && typeof explicitProject.id === "string") {
		const rawIds = await redis.get(`sf:${generation}:project_evidence:${explicitProject.id}`);
		try {
			const parsed = typeof rawIds === "string" ? JSON.parse(rawIds) : rawIds;
			if (Array.isArray(parsed)) exactProjectIds = parsed.map(String).slice(0, 500);
		} catch {
			exactProjectIds = [];
		}
	}
	const identifierTerms = queryIdentifierTerms(query);
	const lexicalCandidateTerms = [...new Set([
		...identifierTerms,
		...tokens(query).filter((term) => term.length >= 4),
	])].slice(0, 12);
	const lexicalIds: string[] = [];
	if (lexicalCandidateTerms.length > 0) {
		const rawLexical = await redis.mget(
			...lexicalCandidateTerms.map((term) => `sf:${generation}:lex:${term}`),
		);
		const candidateCounts = new Map<string, number>();
		for (const raw of rawLexical) {
			try {
				const parsed = typeof raw === "string" ? JSON.parse(raw) : raw;
				if (Array.isArray(parsed)) {
					for (const id of parsed.map(String).slice(0, 200)) {
						candidateCounts.set(id, (candidateCounts.get(id) ?? 0) + 1);
					}
				}
			} catch {
				// A missing or malformed optional lexical bucket must not break vector search.
			}
		}
		lexicalIds.push(...[...candidateCounts.entries()]
			.sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))
			.slice(0, 200)
			.map(([id]) => id));
	}
	const ids = [...new Set([...semanticIds, ...lexicalIds, ...exactProjectIds])];
	const rawRecords = ids.length > 0
		? await redis.mget(...ids.map((id) => `sf:${generation}:evidence:${id}`))
		: [];
	const rules = parseRules(rawSuppressions);
	const similarityById = new Map(hits.map((hit) => [String(hit.id), Number(hit.score) || 0]));
	const results: SourceFirstSearchResult[] = [];
	for (let index = 0; index < ids.length; index += 1) {
		const evidence = parseEvidence(rawRecords[index]);
		if (!evidence || isSuppressed(query, evidence, rules)) continue;
		const scored = scoreSourceFirstResult(query, evidence, similarityById.get(evidence.id) ?? 0);
		const evidenceTerms = new Set(tokens(`${evidence.title}\n${evidence.project ?? ""}\n${evidence.source_path}\n${evidence.text}`));
		scored.exact_identifier_count = identifierTerms.filter((term) => evidenceTerms.has(term)).length;
		scored.exact_identifier_match = scored.exact_identifier_count > 0;
		scored.exact_lexical_match = hasStrongExactLexicalPhrase(query, evidence);
		scored.explicit_project_match = Boolean(
			explicitProject && evidence.project === String(explicitProject.name),
		);
		results.push(scored);
	}
	results.sort((left, right) =>
		right.exact_identifier_count - left.exact_identifier_count
		|| Number(right.exact_identifier_match) - Number(left.exact_identifier_match)
		||
		Number(right.explicit_project_match) - Number(left.explicit_project_match)
		|| Number(right.exact_lexical_match) - Number(left.exact_lexical_match)
		|| right.final_score - left.final_score
		|| right.similarity_score - left.similarity_score
	);
	const diverse: SourceFirstSearchResult[] = [];
	const byChecksum = new Map<string, SourceFirstSearchResult>();
	for (const result of results) {
		const existing = byChecksum.get(result.content_checksum);
		if (existing) {
			existing.duplicate_count += 1;
			existing.alternate_sources.push({
				project: result.project,
				source_path: result.source_path,
				source_modified_at: result.source_modified_at,
			});
			continue;
		}
		byChecksum.set(result.content_checksum, result);
		diverse.push(result);
	}
	const eligible = diverse.filter((result) =>
		result.final_score >= SOURCE_FIRST_MIN_FINAL_SCORE
		|| result.exact_identifier_match
		|| result.exact_lexical_match
		|| result.explicit_project_match
	);
	const abstained = eligible.length === 0;
	return {
		mode: "source_first",
		generation,
		query,
		explicit_project: explicitProject?.name ?? null,
		results: eligible.slice(0, requested),
		abstained,
		abstain_reason: abstained ? "no_relevant_evidence_above_threshold" : null,
		minimum_final_score: SOURCE_FIRST_MIN_FINAL_SCORE,
		deduplication: "content_checksum",
		scoring: "Named projects, opaque identifiers, and strong exact lexical phrase matches receive deterministic candidate recovery; otherwise results must clear 0.65. Base: 0.70 semantic + 0.15 lexical + 0.10 source authority + 0.05 source recency. Working context adds 0.08 * semantic relevance * 3-day attention decay. Byte-identical chunks collapse by content checksum; explicit suppressions apply; no tiers, salience, classification, or access reinforcement.",
	};
}

export async function sourceFirstSearch(
	redis: Redis,
	vector: Index,
	queryEmbedding: number[],
	query: string,
	limit: number,
): Promise<Record<string, unknown>> {
	const generation = await getSourceFirstGeneration(redis);
	if (!generation) {
		return {
			error: "source_first_generation_missing",
			results: [],
			abstained: true,
			abstain_reason: "source_first_generation_missing",
		};
	}
	return sourceFirstSearchGeneration(redis, vector, queryEmbedding, query, limit, generation);
}

export async function getSourceFirstEvidence(redis: Redis, id: string): Promise<Record<string, unknown>> {
	const generation = await getSourceFirstGeneration(redis);
	if (!generation) return { error: "source_first_generation_missing" };
	const evidence = parseEvidence(await redis.get(`sf:${generation}:evidence:${id}`));
	if (!evidence) return { error: "not_found", id, generation };
	let chunkIds: string[] = [];
	if (evidence.source_id) {
		try {
			const raw = await redis.get(`sf:${generation}:source_evidence:${evidence.source_id}`);
			const parsed = typeof raw === "string" ? JSON.parse(raw) : raw;
			if (Array.isArray(parsed)) chunkIds = parsed.map(String);
		} catch {
			chunkIds = [];
		}
	}
	if (chunkIds.length === 0) chunkIds = [id];
	const rawChunks = await redis.mget(...chunkIds.map((chunkId) => `sf:${generation}:evidence:${chunkId}`));
	const chunks = rawChunks
		.map(parseEvidence)
		.filter((value): value is SourceFirstEvidence => value !== null)
		.sort((left, right) => left.chunk_index - right.chunk_index);
	return {
		mode: "source_first",
		generation,
		requested_evidence_id: id,
		evidence,
		source_id: evidence.source_id ?? null,
		source_path: evidence.source_path,
		chunk_count: chunks.length,
		chunks,
		complete_source: Boolean(evidence.source_id && chunks.length === evidence.chunk_count),
	};
}

export async function getSourceFirstOperationalStatus(
	redis: Redis,
	maxAgeSeconds = 36 * 60 * 60,
): Promise<Record<string, unknown>> {
	const generation = await getSourceFirstGeneration(redis);
	const manifest = generation
		? parseObject(await redis.get(`sf:manifest:${generation}`))
		: null;
	const heartbeat = await getSourceFirstHeartbeat(redis);
	const freshness = evaluateSourceFirstFreshness(manifest, heartbeat, new Date(), maxAgeSeconds);
	const passed = Boolean(
		generation
		&& manifest?.generation === generation
		&& heartbeat?.generation === generation
		&& freshness.status === "fresh"
	);
	const recentSessions = parseObject(manifest?.recent_sessions) ?? (
		manifest?.recent_sessions && typeof manifest.recent_sessions === "object"
			? manifest.recent_sessions as Record<string, unknown>
			: null
	);
	const sessionEnabled = recentSessions?.enabled === true;
	const sessionAgeSeconds = freshness.age_seconds;
	const newestSessionTimestamp = [
		recentSessions?.claude_code_newest_observed_at,
		recentSessions?.codex_newest_observed_at,
	]
		.filter((value): value is string => typeof value === "string" && Number.isFinite(Date.parse(value)))
		.map((value) => Date.parse(value))
		.sort((left, right) => right - left)[0];
	const newestSessionAgeSeconds = newestSessionTimestamp === undefined
		? null
		: Math.max(0, Math.floor((Date.now() - newestSessionTimestamp) / 1000));
	const totalSessionChunks = Number(recentSessions?.claude_code_chunks ?? 0)
		+ Number(recentSessions?.codex_chunks ?? 0);
	const sessionFreshness = !sessionEnabled
		? "disabled"
		: !passed
			? "error"
			: sessionAgeSeconds !== null && sessionAgeSeconds > 4 * 60 * 60
				? "stale"
				: totalSessionChunks > 0 ? "fresh" : "empty";
	return {
		schema_version: 2,
		mode: "source_first",
		enabled: true,
		overall_status: passed ? "green" : "red",
		overall_passed: passed,
		generation,
		built_at: manifest?.built_at ?? null,
		published_at: manifest?.published_at ?? heartbeat?.published_at ?? null,
		evidence_count: manifest?.evidence_count ?? null,
		project_count: manifest?.project_count ?? null,
		source_file_count: manifest?.source_file_count ?? null,
		source_checksum: manifest?.source_checksum ?? null,
		freshness,
		recent_sessions: recentSessions ? {
			...recentSessions,
			newest_included_age_seconds: newestSessionAgeSeconds,
			freshness_status: sessionFreshness,
		} : {
			enabled: false,
			freshness_status: "disabled",
		},
		gates: {
			source_first_generation: {
				status: passed ? "pass" : "fail",
				passed,
				generation,
				manifest_matches: manifest?.generation === generation,
				heartbeat_matches: heartbeat?.generation === generation,
				freshness,
			},
			legacy_dream: {
				status: "retired",
				passed: null,
				note: "Dream does not maintain the production source-first corpus.",
			},
		},
	};
}

export async function getSourceFirstIndex(redis: Redis): Promise<Record<string, unknown>> {
	const generation = await getSourceFirstGeneration(redis);
	if (!generation) return { mode: "source_first", error: "source_first_generation_missing", projects: [] };
	const [rawManifest, rawProjects] = await Promise.all([
		redis.get(`sf:manifest:${generation}`),
		redis.get(`sf:${generation}:projects`),
	]);
	const manifest = parseObject(rawManifest);
	const projects = parseProjects(rawProjects);
	const byRecentSource = (left: Record<string, unknown>, right: Record<string, unknown>): number => {
		const leftTime = typeof left.last_touched === "string" ? Date.parse(left.last_touched) : 0;
		const rightTime = typeof right.last_touched === "string" ? Date.parse(right.last_touched) : 0;
		return rightTime - leftTime || String(left.name ?? "").localeCompare(String(right.name ?? ""));
	};
	const activeProjects = projects.filter((project) => project.status === "active").sort(byRecentSource);
	const dormantProjects = projects.filter((project) => project.status !== "active").sort(byRecentSource);
	return {
		mode: "source_first",
		generation,
		built_at: manifest?.built_at ?? null,
		evidence_count: manifest?.evidence_count ?? null,
		source_file_count: manifest?.source_file_count ?? null,
		project_count: projects.length,
		projects: activeProjects.slice(0, 100),
		dormant_projects: dormantProjects.slice(0, 100),
		note: "Projects are derived from authoritative folders and current source timestamps. Search returns exact source paths and dates.",
	};
}
