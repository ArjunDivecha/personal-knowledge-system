/**
 * =============================================================================
 * MCP TOOL: get_deep
 * =============================================================================
 * Returns the full entry including all evidence and evolution history.
 * Use when you need detailed provenance or to explore how thinking evolved.
 * 
 * Arguments:
 *   - id: string (required) - Entry ID (ke_xxx or pe_xxx)
 * 
 * Returns: Full entry with all fields
 * =============================================================================
 */

import { 
  getKnowledgeEntry, 
  getProjectEntry,
  incrementAccessCount,
} from '../storage/redis';
import { GetDeepResponse, KnowledgeEntry, ProjectEntry } from '../types';

export async function getDeep(args: { id: string }): Promise<GetDeepResponse> {
  const { id } = args;
  
  if (!id) {
    throw new Error('Entry ID is required');
  }
  
  let entry: KnowledgeEntry | ProjectEntry | null = null;
  let type: 'knowledge' | 'project';
  
  // Determine type from ID prefix
  if (id.startsWith('ke_')) {
    entry = await getKnowledgeEntry(id);
    type = 'knowledge';
  } else if (id.startsWith('pe_')) {
    entry = await getProjectEntry(id);
    type = 'project';
  } else {
    // Try both
    entry = await getKnowledgeEntry(id);
    type = 'knowledge';
    
    if (!entry) {
      entry = await getProjectEntry(id);
      type = 'project';
    }
  }
  
  if (!entry) {
    throw new Error(`Entry not found: ${id}`);
  }
  
  // Track access
  await incrementAccessCount(type, entry.id);
  
  return { entry };
}

