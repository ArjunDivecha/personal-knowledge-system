import type { Redis } from "@upstash/redis/cloudflare";
import type { Index } from "@upstash/vector";

export const SOURCE_FIRST_CURRENT_KEY = "sf:current_generation";

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
	return value.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

export function findExplicitProject(
	query: string,
	projects: Array<Record<string, unknown>>,
): Record<string, unknown> | null {
	const normalizedQuery = ` ${normalizedPhrase(query)} `;
	return projects
		.filter((project) => typeof project.id === "string" && typeof project.name === "string")
		.sort((left, right) => String(right.name).length - String(left.name).length)
		.find((project) => normalizedQuery.includes(` ${normalizedPhrase(String(project.name))} `)) ?? null;
}

function tokens(text: string): string[] {
	const stopWords = new Set([
		"about", "and", "are", "architecture", "as", "at", "by", "current", "for",
		"from", "how", "in", "into", "is", "now", "of", "on", "please", "project",
		"recent", "status", "system", "tell", "to",
		"that", "the", "this", "was", "were", "what", "when", "where", "with",
	]);
	return [...new Set(
		(text.toLowerCase().match(/[a-z0-9][a-z0-9_-]{1,}/g) ?? [])
			.filter((token) => !stopWords.has(token)),
	)];
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
	};
}

export async function getSourceFirstGeneration(redis: Redis): Promise<string | null> {
	const value = await redis.get(SOURCE_FIRST_CURRENT_KEY);
	return typeof value === "string" && value ? value : null;
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
			if (Array.isArray(parsed)) exactProjectIds = parsed.map(String).slice(0, 100);
		} catch {
			exactProjectIds = [];
		}
	}
	const ids = [...new Set([...semanticIds, ...exactProjectIds])];
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
		scored.explicit_project_match = Boolean(
			explicitProject && evidence.project === String(explicitProject.name),
		);
		results.push(scored);
	}
	results.sort((left, right) =>
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
		scoring: "Exact named projects are selected first; within that set: 0.70 semantic + 0.15 lexical + 0.10 source authority + 0.05 source recency. Explicit suppressions apply; no tiers, salience, classification, or access reinforcement.",
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
