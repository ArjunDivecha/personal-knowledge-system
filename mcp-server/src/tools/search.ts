/**
 * =============================================================================
 * MCP TOOL: search
 * =============================================================================
 * Semantic search across all knowledge and project entries.
 * Returns top matches sorted by relevance.
 * 
 * Arguments:
 *   - query: string (required) - Search query
 *   - limit: number (optional) - Max results (default 5)
 *   - type: 'knowledge' | 'project' (optional) - Filter by entry type
 * 
 * Returns: List of matching entries with scores
 * =============================================================================
 */

import { getKnowledgeEntry, getProjectEntry } from '../storage/redis';
import { searchEntries } from '../storage/vector';
import { SearchResponse, KnowledgeEntry, ProjectEntry } from '../types';

export async function search(args: { 
  query: string; 
  limit?: number;
  type?: 'knowledge' | 'project';
}): Promise<SearchResponse> {
  const { query, limit = 5, type } = args;
  
  if (!query) {
    throw new Error('Query is required');
  }
  
  // Perform semantic search
  const searchResults = await searchEntries(query, limit, { type });
  
  // Fetch entry details for results
  const results: SearchResponse['results'] = [];
  
  for (const result of searchResults) {
    let entry: KnowledgeEntry | ProjectEntry | null = null;
    
    if (result.metadata?.type === 'knowledge' || result.id.startsWith('ke_')) {
      entry = await getKnowledgeEntry(result.id);
      if (entry) {
        results.push({
          id: entry.id,
          domain: (entry as KnowledgeEntry).domain,
          type: 'knowledge',
          current_view: (entry as KnowledgeEntry).current_view,
          score: result.score,
        });
      }
    } else if (result.metadata?.type === 'project' || result.id.startsWith('pe_')) {
      entry = await getProjectEntry(result.id);
      if (entry) {
        results.push({
          id: entry.id,
          name: (entry as ProjectEntry).name,
          type: 'project',
          goal: (entry as ProjectEntry).goal,
          score: result.score,
        });
      }
    }
  }
  
  return { results };
}

