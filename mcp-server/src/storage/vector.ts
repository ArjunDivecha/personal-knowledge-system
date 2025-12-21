/**
 * =============================================================================
 * VECTOR STORAGE CLIENT
 * =============================================================================
 * Version: 1.0.0
 * Last Updated: December 2024
 * 
 * Upstash Vector client for semantic search.
 * =============================================================================
 */

import { Index } from '@upstash/vector';
import OpenAI from 'openai';

// Lazy-initialize clients for serverless
let vectorClient: Index | null = null;
let openaiClient: OpenAI | null = null;

function getVector(): Index {
  if (!vectorClient) {
    if (!process.env.UPSTASH_VECTOR_REST_URL || !process.env.UPSTASH_VECTOR_REST_TOKEN) {
      throw new Error('Missing UPSTASH_VECTOR_REST_URL or UPSTASH_VECTOR_REST_TOKEN environment variables');
    }
    vectorClient = new Index({
      url: process.env.UPSTASH_VECTOR_REST_URL,
      token: process.env.UPSTASH_VECTOR_REST_TOKEN,
    });
  }
  return vectorClient;
}

function getOpenAI(): OpenAI {
  if (!openaiClient) {
    if (!process.env.OPENAI_API_KEY) {
      throw new Error('Missing OPENAI_API_KEY environment variable');
    }
    openaiClient = new OpenAI({
      apiKey: process.env.OPENAI_API_KEY,
    });
  }
  return openaiClient;
}

/**
 * Get embedding for a query.
 */
async function getEmbedding(text: string): Promise<number[]> {
  const response = await getOpenAI().embeddings.create({
    model: 'text-embedding-3-small',
    input: text,
    dimensions: 1536,
  });
  
  return response.data[0].embedding;
}

/**
 * Search for similar entries.
 */
export async function searchEntries(
  query: string,
  topK: number = 5,
  filter?: { type?: 'knowledge' | 'project' }
): Promise<{
  id: string;
  score: number;
  metadata?: {
    type: string;
    domain: string;
    state: string;
    updated_at: string;
  };
}[]> {
  const queryEmbedding = await getEmbedding(query);
  
  const results = await getVector().query({
    vector: queryEmbedding,
    topK,
    includeMetadata: true,
    filter: filter?.type ? `type = '${filter.type}'` : undefined,
  });
  
  return results.map(r => ({
    id: r.id as string,
    score: r.score,
    metadata: r.metadata as {
      type: string;
      domain: string;
      state: string;
      updated_at: string;
    } | undefined,
  }));
}
