/**
 * =============================================================================
 * MCP SERVER TYPE DEFINITIONS
 * =============================================================================
 * Version: 1.0.0
 * Last Updated: December 2024
 * 
 * Types for MCP tools and knowledge entries.
 * =============================================================================
 */

// -----------------------------------------------------------------------------
// MCP Tool Types
// -----------------------------------------------------------------------------

export interface MCPRequest {
  tool: string;
  arguments: Record<string, unknown>;
}

export interface MCPResponse {
  success: boolean;
  data?: unknown;
  error?: string;
  latency_ms?: number;
}

// -----------------------------------------------------------------------------
// Knowledge Entry Types
// -----------------------------------------------------------------------------

export interface Evidence {
  conversation_id: string;
  message_ids: string[];
  snippet: string;
}

export interface Insight {
  insight: string;
  evidence: Evidence;
}

export interface Capability {
  capability: string;
  evidence: Evidence;
}

export interface OpenQuestion {
  question: string;
  context?: string;
  evidence?: Evidence;
}

export interface Position {
  view: string;
  confidence: 'high' | 'medium' | 'low';
  as_of: string;
  evidence: Evidence;
}

export interface Evolution {
  delta: string;
  trigger: string;
  from_view: string;
  to_view: string;
  date: string;
  evidence: Evidence;
}

export interface RepoLink {
  repo: string;
  path?: string;
  link_type: 'explicit' | 'semantic';
  confidence: number;
  evidence?: string;
}

export interface KnowledgeMetadata {
  created_at: string;
  updated_at: string;
  source_conversations: string[];
  source_messages: string[];
  access_count: number;
  last_accessed?: string;
}

export interface KnowledgeEntry {
  id: string;
  type: 'knowledge';
  domain: string;
  subdomain?: string;
  state: 'active' | 'contested' | 'stale' | 'deprecated';
  detail_level: 'full' | 'compressed';
  current_view: string;
  confidence: 'high' | 'medium' | 'low';
  positions: Position[];
  key_insights: Insight[];
  knows_how_to: Capability[];
  open_questions: OpenQuestion[];
  related_repos: RepoLink[];
  related_knowledge: { knowledge_id: string; relationship: string }[];
  evolution: Evolution[];
  metadata: KnowledgeMetadata;
  full_content_ref?: string;
}

// -----------------------------------------------------------------------------
// Project Entry Types
// -----------------------------------------------------------------------------

export interface Decision {
  decision: string;
  rationale?: string;
  date: string;
  evidence?: Evidence;
}

export interface ProjectMetadata {
  created_at: string;
  updated_at: string;
  source_conversations: string[];
  source_messages: string[];
  last_touched: string;
}

export interface ProjectEntry {
  id: string;
  type: 'project';
  name: string;
  status: 'active' | 'paused' | 'completed' | 'abandoned';
  detail_level: 'full' | 'compressed';
  goal: string;
  current_phase: string;
  blocked_on?: string;
  decisions_made: Decision[];
  tech_stack: string[];
  related_repos: RepoLink[];
  related_knowledge: { knowledge_id: string; relationship: string }[];
  phase_history: { phase: string; entered_at: string; evidence: Record<string, unknown> }[];
  metadata: ProjectMetadata;
  full_content_ref?: string;
}

// -----------------------------------------------------------------------------
// Thin Index Types
// -----------------------------------------------------------------------------

export interface ThinIndexTopic {
  id: string;
  domain: string;
  current_view_summary: string;
  state: 'active' | 'contested' | 'stale';
  confidence: 'high' | 'medium' | 'low';
  last_updated: string;
  top_repo?: string;
}

export interface ThinIndexProject {
  id: string;
  name: string;
  status: 'active' | 'paused' | 'completed' | 'abandoned';
  goal_summary: string;
  current_phase: string;
  blocked_on?: string;
  last_touched: string;
  primary_repo?: string;
}

export interface ThinIndexEvolution {
  entry_id: string;
  entry_type: 'knowledge' | 'project';
  domain_or_name: string;
  delta_summary: string;
  date: string;
}

export interface ThinIndex {
  generated_at: string;
  token_count: number;
  topics: ThinIndexTopic[];
  projects: ThinIndexProject[];
  recent_evolutions: ThinIndexEvolution[];
  contested_count: number;
}

// -----------------------------------------------------------------------------
// Tool Response Types
// -----------------------------------------------------------------------------

export interface GetIndexResponse {
  index: ThinIndex;
}

export interface GetContextResponse {
  entry: {
    id: string;
    domain?: string;
    name?: string;
    type: 'knowledge' | 'project';
    current_view?: string;
    goal?: string;
    confidence?: string;
    status?: string;
    key_insights?: string[];
    decisions?: string[];
    state?: string;
    current_phase?: string;
    related_repos?: string[];
  };
  has_full_content: boolean;
}

export interface GetDeepResponse {
  entry: KnowledgeEntry | ProjectEntry;
}

export interface SearchResponse {
  results: {
    id: string;
    domain?: string;
    name?: string;
    type: 'knowledge' | 'project';
    current_view?: string;
    goal?: string;
    score: number;
  }[];
}

