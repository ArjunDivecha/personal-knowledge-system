import type { Redis } from "@upstash/redis/cloudflare";
import type { Index } from "@upstash/vector";

export const SOURCE_FIRST_CURRENT_KEY = "sf:current_generation";
export const SOURCE_FIRST_HEARTBEAT_KEY = "sf:heartbeat";

export interface SourceFirstFreshness {
	status: "fresh" | "stale" | "missing" | "invalid";
	age_seconds: number | null;
	max_age_seconds: number;
	as_of: string | null;
}

export interface SourceFirstEvidence {
	id: string;
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
	final_score: number;
	explicit_project_match: boolean;
	exact_identifier_match: boolean;
	exact_identifier_count: number;
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

function tokens(text: string): string[] {
	const stopWords = new Set([
		"about", "and", "are", "architecture", "as", "at", "by", "current", "for",
		"from", "how", "in", "into", "is", "now", "of", "on", "please", "project",
		"recent", "status", "system", "tell", "to",
		"that", "the", "this", "was", "were", "what", "when", "where", "with",
	]);
	return [...new Set(
		(normalizedPhrase(text).match(/[a-z0-9]{2,}/g) ?? [])
			.filter((token) => !stopWords.has(token)),
	)];
}

function queryIdentifierTerms(query: string): string[] {
	const rawTerms = query.match(/[A-Za-z0-9][A-Za-z0-9._/-]{2,}/g) ?? [];
	return [...new Set(rawTerms
		.filter((term) => /[A-Z0-9._/-]/.test(term))
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
	const finalScore = Math.round((
		0.70 * similarityScore +
		0.15 * lexicalScore +
		0.10 * authority +
		0.05 * recencyScore
	) * 10000) / 10000;
	return {
		...evidence,
		similarity_score: similarityScore,
		lexical_score: Math.round(lexicalScore * 10000) / 10000,
		recency_score: Math.round(recencyScore * 10000) / 10000,
		final_score: finalScore,
		explicit_project_match: false,
		exact_identifier_match: false,
		exact_identifier_count: 0,
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

export async function sourceFirstSearch(
	redis: Redis,
	vector: Index,
	queryEmbedding: number[],
	query: string,
	limit: number,
): Promise<Record<string, unknown>> {
	const generation = await getSourceFirstGeneration(redis);
	if (!generation) return { error: "source_first_generation_missing", results: [] };
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
	const lexicalIds: string[] = [];
	if (identifierTerms.length > 0) {
		const rawLexical = await redis.mget(
			...identifierTerms.map((term) => `sf:${generation}:lex:${term}`),
		);
		for (const raw of rawLexical) {
			try {
				const parsed = typeof raw === "string" ? JSON.parse(raw) : raw;
				if (Array.isArray(parsed)) lexicalIds.push(...parsed.map(String).slice(0, 200));
			} catch {
				// A missing or malformed optional lexical bucket must not break vector search.
			}
		}
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
		|| right.final_score - left.final_score
		|| right.similarity_score - left.similarity_score
	);
	return {
		mode: "source_first",
		generation,
		query,
		explicit_project: explicitProject?.name ?? null,
		results: results.slice(0, requested),
		scoring: "Exact identifiers and named projects are selected first; within that set: 0.70 semantic + 0.15 lexical + 0.10 source authority + 0.05 source recency. Explicit suppressions apply; no tiers, salience, classification, or access reinforcement.",
	};
}

export async function getSourceFirstEvidence(redis: Redis, id: string): Promise<Record<string, unknown>> {
	const generation = await getSourceFirstGeneration(redis);
	if (!generation) return { error: "source_first_generation_missing" };
	const evidence = parseEvidence(await redis.get(`sf:${generation}:evidence:${id}`));
	return evidence ? { mode: "source_first", generation, evidence } : { error: "not_found", id, generation };
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
