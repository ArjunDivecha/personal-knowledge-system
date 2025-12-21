/**
 * =============================================================================
 * MCP TOOL: get_index
 * =============================================================================
 * Returns the thin index - a compressed view of all knowledge and projects.
 * This is typically the first tool called to understand what's available.
 * 
 * Arguments: none
 * Returns: ThinIndex with topics, projects, and recent evolutions
 * =============================================================================
 */

import { getThinIndex } from '../storage/redis';
import { GetIndexResponse, ThinIndex } from '../types';

export async function getIndex(): Promise<GetIndexResponse> {
  const index = await getThinIndex();
  
  if (!index) {
    // Return empty index if none exists
    const emptyIndex: ThinIndex = {
      generated_at: new Date().toISOString(),
      token_count: 0,
      topics: [],
      projects: [],
      recent_evolutions: [],
      contested_count: 0,
    };
    return { index: emptyIndex };
  }
  
  return { index };
}

