/**
 * =============================================================================
 * REDIS STORAGE CLIENT
 * =============================================================================
 * Version: 1.0.0
 * Last Updated: December 2024
 * 
 * Upstash Redis client for retrieving knowledge entries.
 * =============================================================================
 */

import { Redis } from '@upstash/redis';
import { 
  KnowledgeEntry, 
  ProjectEntry, 
  ThinIndex 
} from '../types';

// Lazy-initialize Redis client for serverless
let redisClient: Redis | null = null;

function getRedis(): Redis {
  if (!redisClient) {
    if (!process.env.UPSTASH_REDIS_REST_URL || !process.env.UPSTASH_REDIS_REST_TOKEN) {
      throw new Error('Missing UPSTASH_REDIS_REST_URL or UPSTASH_REDIS_REST_TOKEN environment variables');
    }
    redisClient = new Redis({
      url: process.env.UPSTASH_REDIS_REST_URL,
      token: process.env.UPSTASH_REDIS_REST_TOKEN,
    });
  }
  return redisClient;
}

/**
 * Get the thin index from Redis.
 */
export async function getThinIndex(): Promise<ThinIndex | null> {
  const data = await getRedis().get<ThinIndex>('index:current');
  return data;
}

/**
 * Get a knowledge entry by ID.
 */
export async function getKnowledgeEntry(id: string): Promise<KnowledgeEntry | null> {
  const data = await getRedis().get<KnowledgeEntry>(`knowledge:${id}`);
  return data;
}

/**
 * Get a project entry by ID.
 */
export async function getProjectEntry(id: string): Promise<ProjectEntry | null> {
  const data = await getRedis().get<ProjectEntry>(`project:${id}`);
  return data;
}

/**
 * Get all knowledge entries.
 */
export async function getAllKnowledgeEntries(): Promise<KnowledgeEntry[]> {
  const entries: KnowledgeEntry[] = [];
  const redis = getRedis();
  
  let cursor: string | number = 0;
  do {
    const [nextCursor, keys] = await redis.scan(cursor, {
      match: 'knowledge:*',
      count: 100,
    });
    cursor = nextCursor as string | number;
    
    for (const key of keys) {
      const data = await redis.get<KnowledgeEntry>(key);
      if (data) {
        entries.push(data);
      }
    }
  } while (cursor !== 0);
  
  return entries;
}

/**
 * Get all project entries.
 */
export async function getAllProjectEntries(): Promise<ProjectEntry[]> {
  const entries: ProjectEntry[] = [];
  const redis = getRedis();
  
  let cursor: string | number = 0;
  do {
    const [nextCursor, keys] = await redis.scan(cursor, {
      match: 'project:*',
      count: 100,
    });
    cursor = nextCursor as string | number;
    
    for (const key of keys) {
      const data = await redis.get<ProjectEntry>(key);
      if (data) {
        entries.push(data);
      }
    }
  } while (cursor !== 0);
  
  return entries;
}

/**
 * Find knowledge entry by domain (case-insensitive partial match).
 */
export async function findKnowledgeByDomain(domain: string): Promise<KnowledgeEntry | null> {
  const entries = await getAllKnowledgeEntries();
  const searchLower = domain.toLowerCase();
  
  // Exact match first
  const exact = entries.find(e => e.domain.toLowerCase() === searchLower);
  if (exact) return exact;
  
  // Partial match
  const partial = entries.find(e => e.domain.toLowerCase().includes(searchLower));
  if (partial) return partial;
  
  // Word match
  const searchWords = searchLower.split(/\s+/);
  const wordMatch = entries.find(e => {
    const domainWords = e.domain.toLowerCase().split(/\s+/);
    return searchWords.every(w => domainWords.some(dw => dw.includes(w)));
  });
  
  return wordMatch || null;
}

/**
 * Find project entry by name (case-insensitive partial match).
 */
export async function findProjectByName(name: string): Promise<ProjectEntry | null> {
  const entries = await getAllProjectEntries();
  const searchLower = name.toLowerCase();
  
  // Exact match first
  const exact = entries.find(e => e.name.toLowerCase() === searchLower);
  if (exact) return exact;
  
  // Partial match
  const partial = entries.find(e => e.name.toLowerCase().includes(searchLower));
  return partial || null;
}

/**
 * Increment access count for an entry.
 */
export async function incrementAccessCount(type: 'knowledge' | 'project', id: string): Promise<void> {
  const redis = getRedis();
  const key = `${type}:${id}`;
  const data = await redis.get<KnowledgeEntry | ProjectEntry>(key);
  
  if (data && data.metadata) {
    const meta = data.metadata as any;
    meta.access_count = (meta.access_count || 0) + 1;
    meta.last_accessed = new Date().toISOString();
    await redis.set(key, data);
  }
}
