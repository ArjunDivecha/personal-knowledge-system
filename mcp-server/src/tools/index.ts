/**
 * =============================================================================
 * MCP TOOLS INDEX
 * =============================================================================
 * Exports all MCP tools and their definitions.
 * =============================================================================
 */

export { getIndex } from './get-index';
export { getContext } from './get-context';
export { getDeep } from './get-deep';
export { search } from './search';

// Tool definitions for MCP registration
export const TOOL_DEFINITIONS = [
  {
    name: 'get_index',
    description: 'Get the thin index - a compressed view of all knowledge topics and projects. Call this first to see what knowledge exists.',
    inputSchema: {
      type: 'object',
      properties: {},
      required: [],
    },
  },
  {
    name: 'get_context',
    description: 'Get the current view and key insights for a topic or project. Use when you need to understand a specific topic quickly.',
    inputSchema: {
      type: 'object',
      properties: {
        topic: {
          type: 'string',
          description: 'Topic domain or project name to look up',
        },
      },
      required: ['topic'],
    },
  },
  {
    name: 'get_deep',
    description: 'Get the full entry including all evidence and evolution history. Use when you need detailed provenance or to explore how thinking evolved.',
    inputSchema: {
      type: 'object',
      properties: {
        id: {
          type: 'string',
          description: 'Entry ID (ke_xxx for knowledge, pe_xxx for project)',
        },
      },
      required: ['id'],
    },
  },
  {
    name: 'search',
    description: 'Semantic search across all knowledge and project entries. Use when looking for relevant past discussions or decisions.',
    inputSchema: {
      type: 'object',
      properties: {
        query: {
          type: 'string',
          description: 'Search query',
        },
        limit: {
          type: 'number',
          description: 'Max results (default 5)',
        },
        type: {
          type: 'string',
          enum: ['knowledge', 'project'],
          description: 'Filter by entry type',
        },
      },
      required: ['query'],
    },
  },
];

