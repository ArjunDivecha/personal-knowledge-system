import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { McpAgent } from "agents/mcp";
import { z } from "zod";
import { Redis } from "@upstash/redis/cloudflare";
import { Index } from "@upstash/vector";
import OpenAI from "openai";

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
				const index = await redis.get("index:current");
				return {
					content: [{ type: "text", text: JSON.stringify(index || { topics: [], projects: [] }) }],
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
			"Semantic search across all knowledge and project entries. Use when looking for relevant past discussions.",
			{
				query: z.string().describe("Search query"),
				limit: z.number().optional().describe("Max results (default 5)"),
			},
			async ({ query, limit }) => {
				try {
					const vector = this.getVector(this.env);
					const queryEmbedding = await this.getEmbedding(this.env, query);
					const results = await vector.query({
						vector: queryEmbedding,
						topK: limit || 5,
						includeMetadata: true,
					});

					return {
						content: [{
							type: "text",
							text: JSON.stringify({
								results: results.map((r) => ({
									id: r.id,
									score: r.score,
									metadata: r.metadata,
								}))
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
