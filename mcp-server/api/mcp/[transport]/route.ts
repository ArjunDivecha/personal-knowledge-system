// @ts-nocheck
/**
 * =============================================================================
 * MCP SERVER - PROPER PROTOCOL IMPLEMENTATION
 * =============================================================================
 * Uses mcp-handler for correct MCP transport
 * =============================================================================
 */

import { createMcpHandler } from "mcp-handler";
import { z } from "zod";
import { Redis } from "@upstash/redis";
import { Index } from "@upstash/vector";
import OpenAI from "openai";

// Lazy-initialize clients
let redis = null;
let vector = null;
let openai = null;

function getRedis() {
  if (!redis) {
    redis = new Redis({
      url: process.env.UPSTASH_REDIS_REST_URL,
      token: process.env.UPSTASH_REDIS_REST_TOKEN,
    });
  }
  return redis;
}

function getVector() {
  if (!vector) {
    vector = new Index({
      url: process.env.UPSTASH_VECTOR_REST_URL,
      token: process.env.UPSTASH_VECTOR_REST_TOKEN,
    });
  }
  return vector;
}

function getOpenAI() {
  if (!openai) {
    openai = new OpenAI({
      apiKey: process.env.OPENAI_API_KEY,
    });
  }
  return openai;
}

async function getEmbedding(text) {
  const response = await getOpenAI().embeddings.create({
    model: "text-embedding-3-small",
    input: text,
    dimensions: 1536,
  });
  return response.data[0].embedding;
}

// Create MCP handler with tools
const handler = createMcpHandler(
  (server) => {
    // Tool: get_index
    server.tool(
      "get_index",
      "Get the thin index - a compressed view of all knowledge topics and projects. Call this first to see what knowledge exists.",
      {},
      async () => {
        const index = await getRedis().get("index:current");
        return {
          content: [{ type: "text", text: JSON.stringify(index || { topics: [], projects: [] }) }],
        };
      }
    );

    // Tool: get_context
    server.tool(
      "get_context",
      "Get the current view and key insights for a topic or project.",
      { topic: z.string() },
      async (args) => {
        const topic = args.topic;
        const queryEmbedding = await getEmbedding(topic);
        const results = await getVector().query({
          vector: queryEmbedding,
          topK: 1,
          includeMetadata: true,
        });

        if (results.length === 0) {
          return { content: [{ type: "text", text: `No entry found for: ${topic}` }] };
        }

        const result = results[0];
        const entryType = result.metadata?.type;
        const key = entryType === "project" ? `project:${result.id}` : `knowledge:${result.id}`;
        const entry = await getRedis().get(key);

        return { content: [{ type: "text", text: JSON.stringify(entry || { error: "Not found" }) }] };
      }
    );

    // Tool: get_deep
    server.tool(
      "get_deep",
      "Get the full entry including all evidence and evolution history.",
      { id: z.string() },
      async (args) => {
        const id = args.id;
        const type = id.startsWith("pe_") ? "project" : "knowledge";
        const entry = await getRedis().get(`${type}:${id}`);
        return { content: [{ type: "text", text: JSON.stringify(entry || { error: "Not found" }) }] };
      }
    );

    // Tool: search
    server.tool(
      "search",
      "Semantic search across all knowledge and project entries.",
      { query: z.string(), limit: z.number().optional() },
      async (args) => {
        const query = args.query;
        const limit = args.limit || 5;
        const queryEmbedding = await getEmbedding(query);
        const results = await getVector().query({
          vector: queryEmbedding,
          topK: limit,
          includeMetadata: true,
        });

        return {
          content: [{
            type: "text",
            text: JSON.stringify({ results: results.map((r) => ({ id: r.id, score: r.score, metadata: r.metadata })) })
          }],
        };
      }
    );
  },
  {
    serverInfo: { name: "personal-knowledge-mcp", version: "1.0.0" },
    capabilities: { tools: {} },
  },
  {
    basePath: "/api/mcp",
    verboseLogs: true,
  }
);

export { handler as GET, handler as POST };
