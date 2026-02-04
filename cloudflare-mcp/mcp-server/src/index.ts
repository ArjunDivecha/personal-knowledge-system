import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { McpAgent } from "agents/mcp";
import { z } from "zod";
import { Redis } from "@upstash/redis/cloudflare";
import { Index } from "@upstash/vector";
import OpenAI from "openai";
import { OAuthProvider } from "@cloudflare/workers-oauth-provider";

// GitHub accounts to query
const GITHUB_ACCOUNTS = ['arjun-via', 'ArjunDivecha'];

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

// Get source weight multiplier
function getSourceWeight(source: string | undefined): number {
	if (!source) return 1.0;

	const sourceLower = source.toLowerCase();

	if (sourceLower.includes('gmail') || sourceLower.includes('email') || sourceLower.includes('mbox')) {
		return 0.6;
	}

	if (sourceLower.includes('github') || sourceLower.includes('repo')) {
		return 1.1;
	}

	return 1.0;
}

// Combine semantic similarity with recency and source weight
function calculateFinalScore(similarity: number, recency: number, sourceWeight: number = 1.0): number {
	const baseScore = similarity * 0.7 + recency * 0.3;
	return baseScore * sourceWeight;
}

// Define our MCP agent with knowledge tools
export class KnowledgeMCP extends McpAgent<Env, unknown, { userId: string }> {
	server = new McpServer({
		name: "Personal Knowledge System",
		version: "1.0.0",
	});

	private getRedis(env: Env): Redis {
		return new Redis({
			url: env.UPSTASH_REDIS_REST_URL,
			token: env.UPSTASH_REDIS_REST_TOKEN,
		});
	}

	private getVector(env: Env): Index {
		return new Index({
			url: env.UPSTASH_VECTOR_REST_URL,
			token: env.UPSTASH_VECTOR_REST_TOKEN,
		});
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
					topics?: Array<{ id: string; domain: string; current_view_summary?: string; state?: string; confidence?: string; last_updated?: string }>;
					projects?: Array<{ id: string; name: string; goal_summary?: string; status?: string; current_phase?: string; last_touched?: string }>;
					generated_at?: string;
					token_count?: number;
				} | null;

				if (!rawIndex) {
					return { content: [{ type: "text", text: JSON.stringify({ topics: [], projects: [], message: "No index found" }) }] };
				}

				const topics = rawIndex.topics || [];
				const projects = rawIndex.projects || [];

				const sortedTopics = [...topics].sort((a, b) => {
					const dateA = a.last_updated ? new Date(a.last_updated).getTime() : 0;
					const dateB = b.last_updated ? new Date(b.last_updated).getTime() : 0;
					return dateB - dateA;
				});

				const compactTopics = sortedTopics.slice(0, 100).map(t => ({
					id: t.id,
					domain: t.domain,
					summary: (t.current_view_summary || "").substring(0, 100),
					state: t.state,
					updated: t.last_updated ? t.last_updated.substring(0, 10) : null
				}));

				const sortedProjects = [...projects].sort((a, b) => {
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
					touched: p.last_touched ? p.last_touched.substring(0, 10) : null
				}));

				const compactIndex = {
					total_topics: topics.length,
					total_projects: projects.length,
					showing_recent: { topics: compactTopics.length, projects: compactProjects.length },
					topics: compactTopics,
					projects: compactProjects,
					note: "Showing most recent entries. Use 'search' for specific queries or 'get_context' for full details."
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
							topK: 1,
							includeMetadata: true,
						});
					} catch (vecErr) {
						const msg = vecErr instanceof Error ? vecErr.message : String(vecErr);
						return { content: [{ type: "text", text: JSON.stringify({ error: `Vector query failed: ${msg}` }) }] };
					}

					if (results.length === 0) {
						return { content: [{ type: "text", text: `No entry found for: ${topic}` }] };
					}

					const result = results[0];
					const entryType = (result.metadata as Record<string, unknown>)?.type;
					const key = entryType === "project" ? `project:${result.id}` : `knowledge:${result.id}`;
					const entry = await redis.get(key);

					return { content: [{ type: "text", text: JSON.stringify(entry || { error: "Not found in Redis" }) }] };
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
				const entry = await redis.get(`${type}:${id}`);
				return { content: [{ type: "text", text: JSON.stringify(entry || { error: "Not found" }) }] };
			}
		);

		// Tool: search
		this.server.tool(
			"search",
			"Semantic search across all knowledge and project entries. Results are ranked by a combination of relevance (70%) and recency (30%), so recent knowledge is prioritized.",
			{
				query: z.string().describe("Search query"),
				limit: z.number().optional().describe("Max results (default 5)"),
			},
			async ({ query, limit }) => {
				try {
					const vector = this.getVector(this.env);
					const queryEmbedding = await this.getEmbedding(this.env, query);

					const fetchLimit = Math.min((limit || 5) * 3, 20);
					const results = await vector.query({
						vector: queryEmbedding,
						topK: fetchLimit,
						includeMetadata: true,
					});

					const rankedResults = results.map((r) => {
						const metadata = r.metadata as Record<string, unknown> | undefined;
						const updatedAt = metadata?.updated_at as string | undefined;
						const source = metadata?.source as string | undefined;

						const recencyScore = calculateRecencyScore(updatedAt);
						const sourceWeight = getSourceWeight(source);
						const finalScore = calculateFinalScore(r.score, recencyScore, sourceWeight);

						return {
							id: r.id,
							similarity_score: r.score,
							recency_score: recencyScore,
							source_weight: sourceWeight,
							final_score: finalScore,
							metadata: r.metadata,
						};
					});

					rankedResults.sort((a, b) => b.final_score - a.final_score);
					const topResults = rankedResults.slice(0, limit || 5);

					return {
						content: [{
							type: "text",
							text: JSON.stringify({
								results: topResults,
								scoring: "70% semantic + 30% recency, with source weighting (emails 0.6x, github 1.1x)"
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

							return { content: [{ type: "text", text: JSON.stringify({
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
							}) }] };
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
					</ul>
				</body>
			</html>
		`, {
			headers: { "Content-Type": "text/html" }
		});
	}
};

// Export OAuth-wrapped handler
export default new OAuthProvider({
	apiRoute: ["/sse", "/mcp"],
	apiHandler: KnowledgeMCP.mount("/sse") as any,
	defaultHandler: defaultHandler,
	authorizeEndpoint: "/authorize",
	tokenEndpoint: "/token",
	clientRegistrationEndpoint: "/register",
	scopesSupported: ["mcp:read", "mcp:write"],
});
