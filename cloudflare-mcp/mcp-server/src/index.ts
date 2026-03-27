import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { McpAgent } from "agents/mcp";
import { z } from "zod";
import { Redis } from "@upstash/redis/cloudflare";
import { Index } from "@upstash/vector";
import OpenAI from "openai";
import { OAuthProvider } from "@cloudflare/workers-oauth-provider";
import {
	computeSalience,
	computeSearchScore,
	deriveSearchTier,
	getSourceWeightFromMetadata,
	resolveStoredInjectionTier,
} from "./salience";

// GitHub accounts to query
const GITHUB_ACCOUNTS = ['arjun-via', 'ArjunDivecha'];
const MEMORY_SCHEMA_VERSION = 2;

function createRedisClient(env: Env): Redis {
	return new Redis({
		url: env.UPSTASH_REDIS_REST_URL,
		token: env.UPSTASH_REDIS_REST_TOKEN,
	});
}

function createVectorClient(env: Env): Index {
	return new Index({
		url: env.UPSTASH_VECTOR_REST_URL,
		token: env.UPSTASH_VECTOR_REST_TOKEN,
	});
}

function parseStoredObject(raw: unknown): Record<string, unknown> | null {
	if (typeof raw === "string") {
		try {
			const parsed = JSON.parse(raw);
			if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
				return parsed as Record<string, unknown>;
			}
		} catch {
			return null;
		}
	}

	if (raw && typeof raw === "object" && !Array.isArray(raw)) {
		return { ...(raw as Record<string, unknown>) };
	}

	return null;
}

function toStringArray(value: unknown): string[] {
	if (!Array.isArray(value)) return [];
	return value.filter((item): item is string => typeof item === "string");
}

function toOptionalNumber(value: unknown): number | null {
	if (typeof value === "number" && Number.isFinite(value)) {
		return value;
	}
	if (typeof value === "string" && value.trim() !== "") {
		const parsed = Number(value);
		return Number.isFinite(parsed) ? parsed : null;
	}
	return null;
}

function toOptionalInteger(value: unknown): number | null {
	const parsed = toOptionalNumber(value);
	return parsed === null ? null : Math.trunc(parsed);
}

function toSourceWeights(value: unknown): Record<string, number> {
	if (!value || typeof value !== "object" || Array.isArray(value)) {
		return {};
	}

	const normalized: Record<string, number> = {};
	for (const [key, rawValue] of Object.entries(value as Record<string, unknown>)) {
		const parsed = toOptionalNumber(rawValue);
		if (parsed !== null) {
			normalized[key] = parsed;
		}
	}
	return normalized;
}

function normalizeEntryMetadata(rawMetadata: unknown, entryType?: string): Record<string, unknown> {
	const metadata = parseStoredObject(rawMetadata) ?? {};
	const sourceConversations = toStringArray(metadata.source_conversations);
	const sourceMessages = toStringArray(metadata.source_messages);
	const updatedAt =
		typeof metadata.updated_at === "string"
			? metadata.updated_at
			: typeof metadata.last_touched === "string"
				? metadata.last_touched
				: typeof metadata.created_at === "string"
					? metadata.created_at
					: "";
	const createdAt = typeof metadata.created_at === "string" ? metadata.created_at : updatedAt;

	const normalized: Record<string, unknown> = {
		...metadata,
		created_at: createdAt,
		updated_at: updatedAt,
		source_conversations: sourceConversations,
		source_messages: sourceMessages,
		access_count: toOptionalInteger(metadata.access_count) ?? 0,
		last_accessed: typeof metadata.last_accessed === "string" ? metadata.last_accessed : null,
		schema_version: toOptionalInteger(metadata.schema_version) ?? MEMORY_SCHEMA_VERSION,
		classification_status:
			typeof metadata.classification_status === "string" && metadata.classification_status.length > 0
				? metadata.classification_status
				: "pending",
		context_type: typeof metadata.context_type === "string" ? metadata.context_type : null,
		mention_count: toOptionalInteger(metadata.mention_count) ?? Math.max(1, sourceConversations.length || 1),
		first_seen: typeof metadata.first_seen === "string" ? metadata.first_seen : null,
		last_seen: typeof metadata.last_seen === "string" ? metadata.last_seen : null,
		auto_inferred: typeof metadata.auto_inferred === "boolean" ? metadata.auto_inferred : null,
		source_weights: toSourceWeights(metadata.source_weights),
		injection_tier: toOptionalInteger(metadata.injection_tier),
		salience_score: toOptionalNumber(metadata.salience_score),
		last_consolidated: typeof metadata.last_consolidated === "string" ? metadata.last_consolidated : null,
		consolidation_notes: toStringArray(metadata.consolidation_notes),
		archived: Boolean(metadata.archived),
	};

	if (entryType === "project") {
		normalized.last_touched =
			typeof metadata.last_touched === "string" ? metadata.last_touched : updatedAt;
	}

	return normalized;
}

function normalizeEntry(raw: unknown, entryTypeHint?: string): Record<string, unknown> | null {
	const entry = parseStoredObject(raw);
	if (!entry) return null;

	const entryType = typeof entry.type === "string" ? entry.type : entryTypeHint;
	const normalized = {
		...entry,
		type: entryType ?? entry.type,
		metadata: normalizeEntryMetadata(entry.metadata, entryType),
	};
	const metadata = normalized.metadata as Record<string, unknown>;
	metadata.injection_tier = resolveStoredInjectionTier(metadata);
	metadata.salience_score = computeSalience(normalized);
	return normalized;
}

// GitHub API helper
async function githubRequest(
	endpoint: string,
	token: string,
	params: Record<string, string> = {}
): Promise<any> {
	const url = new URL(`https://api.github.com${endpoint}`);
	Object.entries(params).forEach(([key, value]) => {
		url.searchParams.append(key, value);
	});

	const response = await fetch(url.toString(), {
		headers: {
			'Authorization': `token ${token}`,
			'Accept': 'application/vnd.github.v3+json',
			'User-Agent': 'personal-knowledge-mcp',
		},
	});

	if (!response.ok) {
		if (response.status === 404) return null;
		throw new Error(`GitHub API error: ${response.status}`);
	}

	return response.json();
}

// Calculate recency score based on how recently the entry was updated
function calculateRecencyScore(updatedAt: string | undefined): number {
	if (!updatedAt) return 0.5;

	try {
		const entryDate = new Date(updatedAt);
		const now = new Date();
		const daysSinceUpdate = (now.getTime() - entryDate.getTime()) / (1000 * 60 * 60 * 24);

		if (daysSinceUpdate <= 7) return 1.0;
		if (daysSinceUpdate <= 30) return 0.9;
		if (daysSinceUpdate <= 90) return 0.75;
		if (daysSinceUpdate <= 180) return 0.6;
		if (daysSinceUpdate <= 365) return 0.45;
		if (daysSinceUpdate <= 730) return 0.3;
		return 0.2;
	} catch {
		return 0.5;
	}
}

function getEntryMetadata(entry: Record<string, unknown> | null): Record<string, unknown> {
	return (entry?.metadata as Record<string, unknown> | undefined) ?? {};
}

function getEntryUpdatedAt(entry: Record<string, unknown>): string | undefined {
	const metadata = getEntryMetadata(entry);
	return (
		(typeof metadata.last_seen === "string" && metadata.last_seen) ||
		(typeof metadata.updated_at === "string" && metadata.updated_at) ||
		(typeof metadata.last_touched === "string" && metadata.last_touched) ||
		undefined
	);
}

function getEntryState(entry: Record<string, unknown>): string | null {
	if (typeof entry.state === "string") return entry.state;
	if (typeof entry.status === "string") return entry.status;
	return null;
}

function getEntryLabel(entry: Record<string, unknown>): string {
	if (typeof entry.domain === "string") return entry.domain;
	if (typeof entry.name === "string") return entry.name;
	return String(entry.id ?? "unknown");
}

function getEntrySummary(entry: Record<string, unknown>): string {
	if (typeof entry.current_view === "string" && entry.current_view.length > 0) {
		return entry.current_view.slice(0, 160);
	}
	if (typeof entry.goal === "string" && entry.goal.length > 0) {
		return entry.goal.slice(0, 160);
	}
	return "";
}

async function buildHealthPayload(env: Env): Promise<Record<string, unknown>> {
	const redis = createRedisClient(env);
	const rawIndex = parseStoredObject(await redis.get("index:current")) ?? {};
	const dreamSummary = parseStoredObject(await redis.get("dream:last_run"));
	const backfillComplete = await redis.get("migration:backfill_complete");
	const pendingClassificationCount = await redis.scard("classification:pending") as number;
	const topics = Array.isArray(rawIndex.topics) ? rawIndex.topics : [];
	const projects = Array.isArray(rawIndex.projects) ? rawIndex.projects : [];

	return {
		status: "ok",
		retrieved_at: new Date().toISOString(),
		schema_version: MEMORY_SCHEMA_VERSION,
		migration_backfill_complete: backfillComplete,
		pending_classification_count: pendingClassificationCount || 0,
		last_dream_run: typeof dreamSummary?.run_at === "string" ? dreamSummary.run_at : null,
		thin_index: {
			generated_at: typeof rawIndex.generated_at === "string" ? rawIndex.generated_at : null,
			stored_topic_count: topics.length,
			stored_project_count: projects.length,
			total_topic_count:
				typeof rawIndex.total_topic_count === "number" ? rawIndex.total_topic_count : topics.length,
			total_project_count:
				typeof rawIndex.total_project_count === "number" ? rawIndex.total_project_count : projects.length,
			tier_1_count: typeof rawIndex.tier_1_count === "number" ? rawIndex.tier_1_count : null,
			tier_2_count: typeof rawIndex.tier_2_count === "number" ? rawIndex.tier_2_count : null,
			tier_3_count: typeof rawIndex.tier_3_count === "number" ? rawIndex.tier_3_count : null,
			archived_count: typeof rawIndex.archived_count === "number" ? rawIndex.archived_count : 0,
		},
	};
}

// Define our MCP agent with knowledge tools
export class KnowledgeMCP extends McpAgent<Env, unknown, { userId: string }> {
	server = new McpServer({
		name: "Personal Knowledge System",
		version: "1.0.0",
	});

	private getRedis(env: Env): Redis {
		return createRedisClient(env);
	}

	private getVector(env: Env): Index {
		return createVectorClient(env);
	}

	private async getEmbedding(env: Env, text: string): Promise<number[]> {
		if (!env.OPENAI_API_KEY) {
			throw new Error("OPENAI_API_KEY not configured");
		}
		try {
			const openai = new OpenAI({ apiKey: env.OPENAI_API_KEY });
			const response = await openai.embeddings.create({
				model: "text-embedding-3-large",
				input: text,
				dimensions: 3072,
			});
			return response.data[0].embedding;
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			throw new Error(`OpenAI embedding failed: ${msg}`);
		}
	}

	async init() {
		// Tool: get_index
		this.server.tool(
			"get_index",
			"Get the thin index - a compressed view of all knowledge topics and projects. Call this first to see what knowledge exists.",
			{},
			async () => {
				const redis = this.getRedis(this.env);
				const rawIndex = await redis.get("index:current") as {
					topics?: Array<{ id: string; domain: string; current_view_summary?: string; state?: string; confidence?: string; last_updated?: string; context_type?: string; injection_tier?: number; salience_score?: number; mention_count?: number; archived?: boolean }>;
					projects?: Array<{ id: string; name: string; goal_summary?: string; status?: string; current_phase?: string; last_touched?: string; context_type?: string; injection_tier?: number; salience_score?: number; mention_count?: number; archived?: boolean }>;
					generated_at?: string;
					token_count?: number;
					total_topic_count?: number;
					total_project_count?: number;
					tier_1_count?: number;
					tier_2_count?: number;
					tier_3_count?: number;
					archived_count?: number;
				} | null;
				const dreamSummary = parseStoredObject(await redis.get("dream:last_run"));

				if (!rawIndex) {
					return { content: [{ type: "text", text: JSON.stringify({ topics: [], projects: [], message: "No index found" }) }] };
				}

				const topics = (rawIndex.topics || []).filter((topic) => !topic.archived);
				const projects = (rawIndex.projects || []).filter((project) => !project.archived);

				const sortedTopics = [...topics].sort((a, b) => {
					const tierA = typeof a.injection_tier === "number" ? a.injection_tier : 3;
					const tierB = typeof b.injection_tier === "number" ? b.injection_tier : 3;
					if (tierA !== tierB) return tierA - tierB;
					const salienceA = typeof a.salience_score === "number" ? a.salience_score : 0;
					const salienceB = typeof b.salience_score === "number" ? b.salience_score : 0;
					if (salienceA !== salienceB) return salienceB - salienceA;
					const dateA = a.last_updated ? new Date(a.last_updated).getTime() : 0;
					const dateB = b.last_updated ? new Date(b.last_updated).getTime() : 0;
					return dateB - dateA;
				});

				const compactTopics = sortedTopics.slice(0, 100).map(t => ({
					id: t.id,
					domain: t.domain,
					summary: (t.current_view_summary || "").substring(0, 100),
					state: t.state,
					updated: t.last_updated ? t.last_updated.substring(0, 10) : null,
					injection_tier: typeof t.injection_tier === "number" ? t.injection_tier : 3,
					context_type: t.context_type || null,
					salience_score: typeof t.salience_score === "number" ? t.salience_score : null,
					mention_count: typeof t.mention_count === "number" ? t.mention_count : null,
				}));

				const sortedProjects = [...projects].sort((a, b) => {
					const tierA = typeof a.injection_tier === "number" ? a.injection_tier : 3;
					const tierB = typeof b.injection_tier === "number" ? b.injection_tier : 3;
					if (tierA !== tierB) return tierA - tierB;
					const salienceA = typeof a.salience_score === "number" ? a.salience_score : 0;
					const salienceB = typeof b.salience_score === "number" ? b.salience_score : 0;
					if (salienceA !== salienceB) return salienceB - salienceA;
					const dateA = a.last_touched ? new Date(a.last_touched).getTime() : 0;
					const dateB = b.last_touched ? new Date(b.last_touched).getTime() : 0;
					return dateB - dateA;
				});

				const compactProjects = sortedProjects.slice(0, 50).map(p => ({
					id: p.id,
					name: p.name,
					goal: (p.goal_summary || "").substring(0, 80),
					status: p.status,
					phase: (p.current_phase || "").substring(0, 60),
					touched: p.last_touched ? p.last_touched.substring(0, 10) : null,
					injection_tier: typeof p.injection_tier === "number" ? p.injection_tier : 3,
					context_type: p.context_type || null,
					salience_score: typeof p.salience_score === "number" ? p.salience_score : null,
					mention_count: typeof p.mention_count === "number" ? p.mention_count : null,
				}));

				const compactIndex = {
					total_topics: typeof rawIndex.total_topic_count === "number" ? rawIndex.total_topic_count : topics.length,
					total_projects: typeof rawIndex.total_project_count === "number" ? rawIndex.total_project_count : projects.length,
					tier_1_count: typeof rawIndex.tier_1_count === "number" ? rawIndex.tier_1_count : null,
					tier_2_count: typeof rawIndex.tier_2_count === "number" ? rawIndex.tier_2_count : null,
					tier_3_count: typeof rawIndex.tier_3_count === "number" ? rawIndex.tier_3_count : null,
					archived_count: typeof rawIndex.archived_count === "number" ? rawIndex.archived_count : 0,
					last_dream_run: typeof dreamSummary?.run_at === "string" ? dreamSummary.run_at : null,
					generated_at: rawIndex.generated_at || null,
					showing_recent: { topics: compactTopics.length, projects: compactProjects.length },
					topics: compactTopics,
					projects: compactProjects,
					note: "Showing the thin-index subset ordered by tier then salience. Use 'search' for query-specific retrieval or 'get_context' for the full entry."
				};

				return {
					content: [{ type: "text", text: JSON.stringify(compactIndex) }],
				};
			}
		);

		// Tool: get_context
		this.server.tool(
			"get_context",
			"Get the current view and key insights for a topic or project. Use when you need to understand a specific topic quickly.",
			{ topic: z.string().describe("Topic domain or project name to look up") },
			async ({ topic }) => {
				try {
					const redis = this.getRedis(this.env);
					const vector = this.getVector(this.env);

					let queryEmbedding: number[];
					try {
						queryEmbedding = await this.getEmbedding(this.env, topic);
					} catch (embErr) {
						const msg = embErr instanceof Error ? embErr.message : String(embErr);
						return { content: [{ type: "text", text: JSON.stringify({ error: `Embedding step failed: ${msg}` }) }] };
					}

					let results;
					try {
						results = await vector.query({
							vector: queryEmbedding,
							topK: 5,
							includeMetadata: true,
						});
					} catch (vecErr) {
						const msg = vecErr instanceof Error ? vecErr.message : String(vecErr);
						return { content: [{ type: "text", text: JSON.stringify({ error: `Vector query failed: ${msg}` }) }] };
					}

					const entry = await (async () => {
						for (const result of results) {
							const vectorMetadata = parseStoredObject(result.metadata) ?? {};
							if (vectorMetadata.archived === true) {
								continue;
							}
							const entryType = vectorMetadata.type === "project" ? "project" : "knowledge";
							const candidate = normalizeEntry(
								await redis.get(`${entryType}:${result.id}`),
								entryType,
							);
							if (!candidate) {
								continue;
							}
							const candidateMetadata = getEntryMetadata(candidate);
							if (candidateMetadata.archived === true) {
								continue;
							}
							return candidate;
						}
						return null;
					})();

					if (!entry) {
						return { content: [{ type: "text", text: `No active entry found for: ${topic}` }] };
					}

					return { content: [{ type: "text", text: JSON.stringify(entry) }] };
				} catch (error) {
					const errMsg = error instanceof Error ? error.message : String(error);
					return { content: [{ type: "text", text: JSON.stringify({ error: `Unexpected: ${errMsg}` }) }] };
				}
			}
		);

		// Tool: get_deep
		this.server.tool(
			"get_deep",
			"Get the full entry including all evidence and evolution history. Use when you need detailed provenance.",
			{ id: z.string().describe("Entry ID (ke_xxx for knowledge, pe_xxx for project)") },
			async ({ id }) => {
				const redis = this.getRedis(this.env);
				const type = id.startsWith("pe_") ? "project" : "knowledge";
				const entry = normalizeEntry(await redis.get(`${type}:${id}`), type);
				return { content: [{ type: "text", text: JSON.stringify(entry || { error: "Not found" }) }] };
			}
		);

		// Tool: search
		this.server.tool(
			"search",
			"Tier-aware semantic search across all knowledge and project entries. Archived entries are excluded by default, and results are reranked by semantic match, salience, recency, and retrieval tier.",
			{
				query: z.string().describe("Search query"),
				limit: z.number().optional().describe("Max results (default 5)"),
				tier_filter: z.union([z.literal(1), z.literal(2), z.literal(3)]).optional()
					.describe("Optional tier filter: 1, 2, or 3"),
			},
			async ({ query, limit, tier_filter }) => {
				try {
					const redis = this.getRedis(this.env);
					const vector = this.getVector(this.env);
					const queryEmbedding = await this.getEmbedding(this.env, query);

					const requestedLimit = Math.max(1, Math.min(limit || 5, 20));
					const fetchLimit = Math.min(requestedLimit * 8, 60);
					const results = await vector.query({
						vector: queryEmbedding,
						topK: fetchLimit,
						includeMetadata: true,
					});

					const rankedResults = await Promise.all(results.map(async (result) => {
						const vectorMetadata = parseStoredObject(result.metadata) ?? {};
						const entryType = vectorMetadata.type === "project" ? "project" : "knowledge";
						const entry = normalizeEntry(await redis.get(`${entryType}:${result.id}`), entryType);
						if (!entry) return null;

						const entryMetadata = getEntryMetadata(entry);
						if (entryMetadata.archived === true) {
							return null;
						}

						const salienceScore = computeSalience(entry);
						entryMetadata.salience_score = salienceScore;
						entryMetadata.injection_tier = resolveStoredInjectionTier(entryMetadata);

						const effectiveTier = deriveSearchTier(entry, result.score);
						if (tier_filter && effectiveTier !== tier_filter) {
							return null;
						}

						const updatedAt = getEntryUpdatedAt(entry);
						const recencyScore = calculateRecencyScore(updatedAt);
						const sourceWeight = getSourceWeightFromMetadata({
							...vectorMetadata,
							...entryMetadata,
						});
						const finalScore = computeSearchScore({
							similarity: result.score,
							recency: recencyScore,
							salience: salienceScore,
							tier: effectiveTier,
							sourceWeight,
						});

						return {
							id: result.id,
							type: entryType,
							label: getEntryLabel(entry),
							summary: getEntrySummary(entry),
							state: getEntryState(entry),
							context_type: typeof entryMetadata.context_type === "string" ? entryMetadata.context_type : null,
							injection_tier: effectiveTier,
							stored_injection_tier: resolveStoredInjectionTier(entryMetadata),
							salience_score: salienceScore,
							mention_count: typeof entryMetadata.mention_count === "number" ? entryMetadata.mention_count : null,
							similarity_score: result.score,
							recency_score: recencyScore,
							source_weight: sourceWeight,
							final_score: finalScore,
							updated: updatedAt ?? null,
							metadata: {
								classification_status: entryMetadata.classification_status,
								context_type: entryMetadata.context_type,
								injection_tier: effectiveTier,
								salience_score: salienceScore,
								mention_count: entryMetadata.mention_count,
								archived: false,
							},
						};
					}));

					const filteredResults = rankedResults.filter((result): result is NonNullable<typeof result> => result !== null);
					filteredResults.sort((a, b) => {
						if (a.injection_tier !== b.injection_tier) return a.injection_tier - b.injection_tier;
						if (a.final_score !== b.final_score) return b.final_score - a.final_score;
						return b.similarity_score - a.similarity_score;
					});
					const topResults = filteredResults.slice(0, requestedLimit);

					return {
						content: [{
							type: "text",
							text: JSON.stringify({
								results: topResults,
								query,
								tier_filter: tier_filter ?? null,
								scoring: "ranked by retrieval tier, then a weighted score of semantic similarity, recency, salience, and source weight; archived entries excluded by default"
							})
						}],
					};
				} catch (error) {
					const errMsg = error instanceof Error ? error.message : String(error);
					return { content: [{ type: "text", text: JSON.stringify({ error: errMsg }) }] };
				}
			}
		);

		// Tool: github - Dynamic GitHub repository queries
		this.server.tool(
			"github",
			"Query GitHub repositories dynamically. Fetches LIVE data from both arjun-via and ArjunDivecha accounts. Use to find code, read files, list repos, or get commit history.",
			{
				operation: z.enum(['list_repos', 'search_code', 'get_file', 'get_repo', 'get_commits'])
					.describe("Operation: list_repos, search_code, get_file, get_repo, get_commits"),
				query: z.string().optional().describe("Search query (for search_code)"),
				repo: z.string().optional().describe("Repository name (for get_file, get_repo, get_commits)"),
				path: z.string().optional().describe("File path (for get_file)"),
				language: z.string().optional().describe("Filter by language (for search_code)"),
				limit: z.number().optional().describe("Max results (default 20)"),
			},
			async ({ operation, query, repo, path, language, limit }) => {
				const token = this.env.GITHUB_TOKEN;
				if (!token) {
					return { content: [{ type: "text", text: JSON.stringify({ error: "GITHUB_TOKEN not configured" }) }] };
				}

				try {
					switch (operation) {
						case 'list_repos': {
							const allRepos: any[] = [];
							for (const account of GITHUB_ACCOUNTS) {
								let page = 1;
								let hasMore = true;
								while (hasMore) {
									const repos = await githubRequest(
										`/users/${account}/repos`,
										token,
										{ per_page: '100', page: page.toString(), sort: 'updated' }
									);
									if (!repos || repos.length === 0) { hasMore = false; continue; }
									for (const r of repos) {
										allRepos.push({
											name: r.name,
											owner: account,
											description: r.description,
											language: r.language,
											stars: r.stargazers_count || 0,
											updated: r.updated_at,
											private: r.private || false,
										});
									}
									if (repos.length < 100) hasMore = false;
									else page++;
								}
							}
							allRepos.sort((a, b) => new Date(b.updated).getTime() - new Date(a.updated).getTime());
							return { content: [{ type: "text", text: JSON.stringify({ total: allRepos.length, accounts: GITHUB_ACCOUNTS, repos: allRepos }) }] };
						}

						case 'search_code': {
							if (!query) return { content: [{ type: "text", text: JSON.stringify({ error: "query required for search_code" }) }] };
							const userFilter = GITHUB_ACCOUNTS.map(u => `user:${u}`).join(' ');
							let searchQuery = `${query} ${userFilter}`;
							if (language) searchQuery += ` language:${language}`;
							const result = await githubRequest('/search/code', token, { q: searchQuery, per_page: '30' });
							const results = (result?.items || []).map((item: any) => ({
								repo: item.repository.full_name,
								path: item.path,
								url: item.html_url,
							}));
							return { content: [{ type: "text", text: JSON.stringify({ query, results }) }] };
						}

						case 'get_file': {
							if (!repo || !path) return { content: [{ type: "text", text: JSON.stringify({ error: "repo and path required" }) }] };
							let owner = '';
							let repoName = repo;
							if (repo.includes('/')) {
								owner = repo.split('/')[0];
								repoName = repo.split('/')[1];
							} else {
								for (const account of GITHUB_ACCOUNTS) {
									const r = await githubRequest(`/repos/${account}/${repo}`, token);
									if (r) { owner = account; break; }
								}
							}
							if (!owner) return { content: [{ type: "text", text: JSON.stringify({ error: "Repository not found" }) }] };
							const data = await githubRequest(`/repos/${owner}/${repoName}/contents/${path}`, token);
							if (!data || !data.content) return { content: [{ type: "text", text: JSON.stringify({ error: "File not found" }) }] };
							const content = atob(data.content.replace(/\n/g, ''));
							return { content: [{ type: "text", text: JSON.stringify({ path: data.path, repo: `${owner}/${repoName}`, content }) }] };
						}

						case 'get_repo': {
							if (!repo) return { content: [{ type: "text", text: JSON.stringify({ error: "repo required" }) }] };
							let owner = '';
							let repoName = repo;
							let repoData: any = null;
							if (repo.includes('/')) {
								const [o, r] = repo.split('/');
								repoData = await githubRequest(`/repos/${o}/${r}`, token);
								if (repoData) { owner = o; repoName = r; }
							} else {
								for (const account of GITHUB_ACCOUNTS) {
									repoData = await githubRequest(`/repos/${account}/${repo}`, token);
									if (repoData) { owner = account; break; }
								}
							}
							if (!repoData) return { content: [{ type: "text", text: JSON.stringify({ error: "Repository not found" }) }] };

							let readme: string | null = null;
							const readmeData = await githubRequest(`/repos/${owner}/${repoName}/readme`, token);
							if (readmeData?.content) {
								readme = atob(readmeData.content.replace(/\n/g, ''));
							}

							const treeData = await githubRequest(`/repos/${owner}/${repoName}/git/trees/${repoData.default_branch}`, token);
							const files = treeData?.tree?.filter((f: any) => f.type === 'blob')?.slice(0, 30)?.map((f: any) => f.path) || [];

							return {
								content: [{
									type: "text", text: JSON.stringify({
										info: {
											name: repoData.name,
											full_name: repoData.full_name,
											description: repoData.description,
											language: repoData.language,
											stars: repoData.stargazers_count,
											updated: repoData.updated_at,
										},
										readme: readme ? readme.substring(0, 5000) : null,
										files,
									})
								}]
							};
						}

						case 'get_commits': {
							if (!repo) return { content: [{ type: "text", text: JSON.stringify({ error: "repo required" }) }] };
							let owner = '';
							let repoName = repo;
							if (repo.includes('/')) {
								owner = repo.split('/')[0];
								repoName = repo.split('/')[1];
							} else {
								for (const account of GITHUB_ACCOUNTS) {
									const r = await githubRequest(`/repos/${account}/${repo}`, token);
									if (r) { owner = account; break; }
								}
							}
							if (!owner) return { content: [{ type: "text", text: JSON.stringify({ error: "Repository not found" }) }] };
							const commits = await githubRequest(`/repos/${owner}/${repoName}/commits`, token, { per_page: (limit || 20).toString() });
							const result = (commits || []).map((c: any) => ({
								sha: c.sha?.slice(0, 7),
								message: c.commit?.message,
								date: c.commit?.author?.date,
								author: c.commit?.author?.name,
							}));
							return { content: [{ type: "text", text: JSON.stringify({ repo: `${owner}/${repoName}`, commits: result }) }] };
						}

						default:
							return { content: [{ type: "text", text: JSON.stringify({ error: `Unknown operation: ${operation}` }) }] };
					}
				} catch (error) {
					const errMsg = error instanceof Error ? error.message : String(error);
					return { content: [{ type: "text", text: JSON.stringify({ error: errMsg }) }] };
				}
			}
		);
	}
}

// Default handler for non-API routes - must be an object with fetch method
const defaultHandler = {
	async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
		const url = new URL(request.url);

		// Handle OAuth authorization - auto-approve for personal single-user system
		if (url.pathname === "/authorize") {
			try {
				// Parse the OAuth authorization request
				const authRequest = await env.OAUTH_PROVIDER.parseAuthRequest(request);

				if (!authRequest.clientId) {
					return new Response("Missing client_id", { status: 400 });
				}

				// Auto-approve: complete authorization immediately without login
				// This is safe for a personal single-user system
				const { redirectTo } = await env.OAUTH_PROVIDER.completeAuthorization({
					request: authRequest,
					userId: "arjun",
					metadata: {
						label: "Personal Knowledge MCP"
					},
					scope: authRequest.scope,
					props: {
						userId: "arjun",
					},
				});

				return Response.redirect(redirectTo, 302);
			} catch (error) {
				const msg = error instanceof Error ? error.message : String(error);
				return new Response(`Authorization error: ${msg}`, { status: 500 });
			}
		}

		if (url.pathname === "/health" || url.pathname === "/status") {
			try {
				return Response.json(await buildHealthPayload(env), {
					headers: { "Content-Type": "application/json" },
				});
			} catch (error) {
				const msg = error instanceof Error ? error.message : String(error);
				return Response.json({ status: "error", error: msg }, { status: 500 });
			}
		}

		// OAuth discovery endpoint for iOS Claude
		if (url.pathname === "/.well-known/oauth-authorization-server") {
			const baseUrl = `${url.protocol}//${url.host}`;
			return new Response(JSON.stringify({
				issuer: baseUrl,
				authorization_endpoint: `${baseUrl}/authorize`,
				token_endpoint: `${baseUrl}/token`,
				registration_endpoint: `${baseUrl}/register`,
				scopes_supported: ["mcp:read", "mcp:write"],
				response_types_supported: ["code"],
				grant_types_supported: ["authorization_code", "refresh_token"],
				token_endpoint_auth_methods_supported: ["client_secret_post", "client_secret_basic"],
			}), {
				headers: { "Content-Type": "application/json" }
			});
		}

		// Home page
		return new Response(`
			<html>
				<head><title>Personal Knowledge MCP</title></head>
				<body style="font-family: system-ui; padding: 2rem; max-width: 600px; margin: 0 auto;">
					<h1>Personal Knowledge MCP Server</h1>
					<p>This is Arjun's personal knowledge system with OAuth support.</p>
					<h2>Endpoints</h2>
					<ul>
						<li><code>/sse</code> - MCP over SSE (for Claude)</li>
						<li><code>/mcp</code> - MCP over HTTP</li>
						<li><code>/authorize</code> - OAuth authorization</li>
						<li><code>/token</code> - OAuth token endpoint</li>
						<li><code>/register</code> - Dynamic client registration</li>
						<li><code>/health</code> - Rollout and migration status</li>
					</ul>
				</body>
			</html>
		`, {
			headers: { "Content-Type": "text/html" }
		});
	}
};

// Export OAuth-wrapped handler for iOS Claude compatibility
export default new OAuthProvider({
	apiRoute: ["/sse", "/mcp"],
	apiHandler: KnowledgeMCP.mount("/sse") as any,
	defaultHandler: defaultHandler as any,
	authorizeEndpoint: "/authorize",
	tokenEndpoint: "/token",
	clientRegistrationEndpoint: "/register",
	scopesSupported: ["mcp:read", "mcp:write"],
});
