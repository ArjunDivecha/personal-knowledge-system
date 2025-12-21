/**
 * =============================================================================
 * MCP SERVER API ENDPOINT
 * =============================================================================
 * Version: 1.0.0
 * Last Updated: December 2024
 * 
 * Main API endpoint for the MCP server.
 * Handles tool requests and authentication.
 * 
 * Deployed to Vercel at /api/mcp (rewritten to /mcp)
 * =============================================================================
 */

import type { VercelRequest, VercelResponse } from '@vercel/node';
import { getIndex } from '../src/tools/get-index';
import { getContext } from '../src/tools/get-context';
import { getDeep } from '../src/tools/get-deep';
import { search } from '../src/tools/search';
import { TOOL_DEFINITIONS } from '../src/tools';
import { MCPRequest, MCPResponse } from '../src/types';

// -----------------------------------------------------------------------------
// AUTH
// -----------------------------------------------------------------------------

function validateAuth(req: VercelRequest): boolean {
  // If no MCP_AUTH_TOKEN is set, allow all requests (for testing)
  if (!process.env.MCP_AUTH_TOKEN) {
    return true;
  }
  
  const authHeader = req.headers.authorization;
  
  if (!authHeader) {
    return false;
  }
  
  const token = authHeader.replace('Bearer ', '');
  return token === process.env.MCP_AUTH_TOKEN;
}

// -----------------------------------------------------------------------------
// TOOL ROUTER
// -----------------------------------------------------------------------------

async function handleTool(tool: string, args: Record<string, unknown>): Promise<unknown> {
  switch (tool) {
    case 'get_index':
      return await getIndex();
    
    case 'get_context':
      return await getContext({ topic: args.topic as string });
    
    case 'get_deep':
      return await getDeep({ id: args.id as string });
    
    case 'search':
      return await search({
        query: args.query as string,
        limit: args.limit as number | undefined,
        type: args.type as 'knowledge' | 'project' | undefined,
      });
    
    case 'list_tools':
      return { tools: TOOL_DEFINITIONS };
    
    default:
      throw new Error(`Unknown tool: ${tool}`);
  }
}

// -----------------------------------------------------------------------------
// API HANDLER
// -----------------------------------------------------------------------------

export default async function handler(
  req: VercelRequest,
  res: VercelResponse
): Promise<void> {
  const startTime = Date.now();
  
  // CORS headers
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');
  
  // Handle preflight
  if (req.method === 'OPTIONS') {
    res.status(200).end();
    return;
  }
  
  // Only allow POST
  if (req.method !== 'POST') {
    const response: MCPResponse = {
      success: false,
      error: 'Method not allowed',
    };
    res.status(405).json(response);
    return;
  }
  
  // Validate auth
  if (!validateAuth(req)) {
    const response: MCPResponse = {
      success: false,
      error: 'Unauthorized',
    };
    res.status(401).json(response);
    return;
  }
  
  try {
    const body = req.body as MCPRequest;
    
    if (!body.tool) {
      const response: MCPResponse = {
        success: false,
        error: 'Tool name is required',
      };
      res.status(400).json(response);
      return;
    }
    
    const result = await handleTool(body.tool, body.arguments || {});
    
    const response: MCPResponse = {
      success: true,
      data: result,
      latency_ms: Date.now() - startTime,
    };
    
    res.status(200).json(response);
    
  } catch (error) {
    const errorMessage = error instanceof Error ? error.message : 'Unknown error';
    
    const response: MCPResponse = {
      success: false,
      error: errorMessage,
      latency_ms: Date.now() - startTime,
    };
    
    res.status(500).json(response);
  }
}

