import type { OAuthHelpers } from "@cloudflare/workers-oauth-provider";

// Environment variable types for Cloudflare Workers
declare global {
	interface Env {
		UPSTASH_REDIS_REST_URL: string;
		UPSTASH_REDIS_REST_TOKEN: string;
		UPSTASH_VECTOR_REST_URL: string;
		UPSTASH_VECTOR_REST_TOKEN: string;
		OPENAI_API_KEY: string;
		GITHUB_TOKEN: string;
		DREAM_OPERATOR_TOKEN: string;
		// Dream + forgetting design (see docs/dream-and-forgetting-design-2026-05-17.md).
		// All default to "off" if unset — preserves pre-implementation behavior.
		DREAM_AUTO_APPLY_MODE?: "off" | "full";
		DREAM_OPUS_MODE?: "off" | "on";
		RETRIEVAL_POLICY_MODE?: "off" | "on";
		// Optional anomaly-tripwire knobs (have safe defaults).
		DREAM_HARD_DELETE_DAILY_CAP?: string;
		// Optional override for Opus judge model used by Mac-side script.
		DREAM_OPUS_MODEL?: string;
		MCP_OBJECT: DurableObjectNamespace<import("./index").KnowledgeMCP>;
		OPENAI_MCP_OBJECT: DurableObjectNamespace<import("./index").OpenAIKnowledgeMCP>;
		OAUTH_KV: KVNamespace;
		OAUTH_PROVIDER: OAuthHelpers;
	}
}

export {};
