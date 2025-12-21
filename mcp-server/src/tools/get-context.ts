/**
 * =============================================================================
 * MCP TOOL: get_context
 * =============================================================================
 * Returns the current view and key insights for a topic or project.
 * Uses semantic search first (fast), then direct lookup.
 * 
 * Arguments:
 *   - topic: string (required) - Topic domain or project name to look up
 * 
 * Returns: Summary of the entry with key insights/decisions
 * =============================================================================
 */

import { 
  getKnowledgeEntry,
  getProjectEntry,
  incrementAccessCount,
} from '../storage/redis';
import { searchEntries } from '../storage/vector';
import { GetContextResponse, KnowledgeEntry, ProjectEntry } from '../types';

export async function getContext(args: { topic: string }): Promise<GetContextResponse> {
  const { topic } = args;
  
  if (!topic) {
    throw new Error('Topic is required');
  }
  
  let entry: KnowledgeEntry | ProjectEntry | null = null;
  let type: 'knowledge' | 'project' = 'knowledge';
  
  // Use semantic search first (fast) - get top 3 results
  const searchResults = await searchEntries(topic, 3);
  
  if (searchResults.length > 0) {
    // Find best match from results
    for (const result of searchResults) {
      if (result.metadata?.type === 'knowledge') {
        entry = await getKnowledgeEntry(result.id);
        type = 'knowledge';
        if (entry) break;
      } else if (result.metadata?.type === 'project') {
        entry = await getProjectEntry(result.id);
        type = 'project';
        if (entry) break;
      }
    }
  }
  
  if (!entry) {
    throw new Error(`No entry found for topic: ${topic}`);
  }
  
  // Track access
  await incrementAccessCount(type, entry.id);
  
  // Format response based on entry type
  if (type === 'knowledge') {
    const ke = entry as KnowledgeEntry;
    return {
      entry: {
        id: ke.id,
        domain: ke.domain,
        type: 'knowledge',
        current_view: ke.current_view,
        confidence: ke.confidence,
        state: ke.state,
        key_insights: ke.key_insights.slice(0, 5).map(i => i.insight),
        related_repos: ke.related_repos.map(r => r.repo),
      },
      has_full_content: ke.detail_level === 'full',
    };
  } else {
    const pe = entry as ProjectEntry;
    return {
      entry: {
        id: pe.id,
        name: pe.name,
        type: 'project',
        goal: pe.goal,
        status: pe.status,
        current_phase: pe.current_phase,
        decisions: pe.decisions_made.slice(0, 5).map(d => d.decision),
        related_repos: pe.related_repos.map(r => r.repo),
      },
      has_full_content: pe.detail_level === 'full',
    };
  }
}
