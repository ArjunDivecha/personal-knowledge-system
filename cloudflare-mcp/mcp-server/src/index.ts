import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { McpAgent } from "agents/mcp";
import { z } from "zod";
import { Redis } from "@upstash/redis/cloudflare";
import { Index } from "@upstash/vector";
import OpenAI from "openai";

// Calculate recency score based on how recently the entry was updated
// Returns a score between 0.2 (very old) and 1.0 (very recent)
function calculateRecencyScore(updatedAt: string | undefined): number {
	if (!updatedAt) return 0.5; // Default for missing dates
	
	try {
		const entryDate = new Date(updatedAt);
		const now = new Date();
		const daysSinceUpdate = (now.getTime() - entryDate.getTime()) / (1000 * 60 * 60 * 24);
		
		// Decay function: recent entries score higher
		if (daysSinceUpdate <= 7) return 1.0;        // Last week: full score
		if (daysSinceUpdate <= 30) return 0.9;       // Last month: 0.9
		if (daysSinceUpdate <= 90) return 0.75;      // Last 3 months: 0.75
		if (daysSinceUpdate <= 180) return 0.6;      // Last 6 months: 0.6
		if (daysSinceUpdate <= 365) return 0.45;     // Last year: 0.45
		if (daysSinceUpdate <= 730) return 0.3;      // Last 2 years: 0.3
		return 0.2;                                   // Older: 0.2
	} catch {
		return 0.5; // Default on parse error
	}
}

// Get source weight multiplier based on data source
// Emails are downweighted since they dominate the corpus
function getSourceWeight(source: string | undefined): number {
	if (!source) return 1.0;
	
	const sourceLower = source.toLowerCase();
	
	// Check if source contains gmail/email indicators
	if (sourceLower.includes('gmail') || sourceLower.includes('email') || sourceLower.includes('mbox')) {
		return 0.6; // Emails get 60% weight
	}
	
	// GitHub entries get slight boost for being high-signal
	if (sourceLower.includes('github') || sourceLower.includes('repo')) {
		return 1.1; // GitHub gets 110% weight
	}
	
	return 1.0; // Chat/other sources: full weight
}

// Combine semantic similarity with recency and source weight
// Base: 70% semantic, 30% recency, then multiplied by source weight
function calculateFinalScore(similarity: number, recency: number, sourceWeight: number = 1.0): number {
	const baseScore = similarity * 0.7 + recency * 0.3;
	return baseScore * sourceWeight;
}

// Define our MCP agent with knowledge tools
export class KnowledgeMCP extends McpAgent {
	server = new McpServer({
		name: "Personal Knowledge System",
		version: "1.0.0",
	});

	// Get Redis client
	private getRedis(env: Env): Redis {
		return new Redis({
			url: env.UPSTASH_REDIS_REST_URL,
			token: env.UPSTASH_REDIS_REST_TOKEN,
		});
	}

	// Get Vector client
	private getVector(env: Env): Index {
		return new Index({
			url: env.UPSTASH_VECTOR_REST_URL,
			token: env.UPSTASH_VECTOR_REST_TOKEN,
		});
	}

	// Get embedding - using text-embedding-3-large (best OpenAI model)
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
				
				// Compress the index to prevent overwhelming Claude
				// Sort topics by recency and take top 100
				const topics = rawIndex.topics || [];
				const projects = rawIndex.projects || [];
				
				// Sort by last_updated descending
				const sortedTopics = [...topics].sort((a, b) => {
					const dateA = a.last_updated ? new Date(a.last_updated).getTime() : 0;
					const dateB = b.last_updated ? new Date(b.last_updated).getTime() : 0;
					return dateB - dateA;
				});
				
				// Take top 100 topics, truncate summaries
				const compactTopics = sortedTopics.slice(0, 100).map(t => ({
					id: t.id,
					domain: t.domain,
					summary: (t.current_view_summary || "").substring(0, 100),
					state: t.state,
					updated: t.last_updated ? t.last_updated.substring(0, 10) : null
				}));
				
				// Sort projects by last_touched descending
				const sortedProjects = [...projects].sort((a, b) => {
					const dateA = a.last_touched ? new Date(a.last_touched).getTime() : 0;
					const dateB = b.last_touched ? new Date(b.last_touched).getTime() : 0;
					return dateB - dateA;
				});
				
				// Take top 50 projects, truncate summaries
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
					
					// Step 1: Get embedding
					let queryEmbedding: number[];
					try {
						queryEmbedding = await this.getEmbedding(this.env, topic);
					} catch (embErr) {
						const msg = embErr instanceof Error ? embErr.message : String(embErr);
						return { content: [{ type: "text", text: JSON.stringify({ error: `Embedding step failed: ${msg}` }) }] };
					}
					
					// Step 2: Query vector
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

					// Step 3: Get from Redis
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
					
					// Fetch more results than needed to allow re-ranking
					const fetchLimit = Math.min((limit || 5) * 3, 20);
					const results = await vector.query({
						vector: queryEmbedding,
						topK: fetchLimit,
						includeMetadata: true,
					});

					// Apply recency and source weighting, then re-rank
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
					
					// Sort by final score and take requested limit
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
	}
}

export default {
	fetch(request: Request, env: Env, ctx: ExecutionContext) {
		const url = new URL(request.url);

		// SSE endpoint for Claude MCP connector
		if (url.pathname === "/sse" || url.pathname === "/sse/message") {
			return KnowledgeMCP.serveSSE("/sse").fetch(request, env, ctx);
		}

		// HTTP endpoint
		if (url.pathname === "/mcp") {
			return KnowledgeMCP.serve("/mcp").fetch(request, env, ctx);
		}

		return new Response("Personal Knowledge MCP Server. Connect via /sse", { status: 200 });
	},
};
