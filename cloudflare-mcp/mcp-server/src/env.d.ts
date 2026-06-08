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
		// Scheduled Dream now always runs the governed live path; these legacy
		// values may still appear in old records or operator endpoints.
		DREAM_AUTO_APPLY_MODE?: "off" | "governed" | "full";
		DREAM_OPUS_MODE?: "off" | "on";
		RETRIEVAL_POLICY_MODE?: "off" | "on";
		DREAM_PHASE9_OUTCOME_GATE_ENABLED?: string;
		DREAM_PHASE9_AUTO_ROLLBACK_ENABLED?: string;
		DREAM_PHASE9_PROBE_SET_KEY?: string;
		DREAM_PHASE9_WRITE_VALIDATION_LEDGER?: string;
		// Optional anomaly-tripwire knobs (have safe defaults).
		DREAM_HARD_DELETE_DAILY_CAP?: string;
		// Optional override for Opus judge model used by Mac-side script.
		DREAM_OPUS_MODEL?: string;
		// Tweets module (src/tweets/) — referenced by readTweet/readThread tool.
		TWEET_READER_TIMEOUT_MS?: string;
		TWEET_READER_CACHE_TTL_SECONDS?: string;
		// Wrangler injects this at build time when configured.
		BUILD_SHA?: string;
		MCP_OBJECT: DurableObjectNamespace<import("./index").KnowledgeMCP>;
		OPENAI_MCP_OBJECT: DurableObjectNamespace<
			import("./index").OpenAIKnowledgeMCP
		>;
		OAUTH_KV: KVNamespace;
		OAUTH_PROVIDER: OAuthHelpers;
	}
}
