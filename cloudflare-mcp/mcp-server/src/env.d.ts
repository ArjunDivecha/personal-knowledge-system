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
		// Contract PKS-INJECTION-RANKING-002 Phase B cutover: query-time
		// selection switches from sort-and-slice to greedy MMR diversity
		// selection (mmr.ts). Default off/unset -> byte-identical to today.
		RANKING_V2?: "off" | "on";
		// Simple source-first read path. When on, get_index/get_context/search/get_deep
		// serve immutable evidence generations and never write access signals.
		SOURCE_FIRST_MODE?: "off" | "on";
		// Maximum age of the promoted source-first generation before health reports
		// the serving state as stale/degraded. Defaults to 36 hours.
		SOURCE_FIRST_MAX_AGE_SECONDS?: string;
		// Insight synthesis (CMA-dreaming parity). "on" enables nightly cluster
		// detection + judge enqueue; verdicts apply additively next cycle.
		// See docs/pks-dream-insight-synthesis-prd-2026-07-02.md.
		DREAM_INSIGHT_MODE?: "off" | "on";
		DREAM_PHASE9_OUTCOME_GATE_ENABLED?: string;
		DREAM_PHASE9_AUTO_ROLLBACK_ENABLED?: string;
		DREAM_PHASE9_PROBE_SET_KEY?: string;
		DREAM_PHASE9_WRITE_VALIDATION_LEDGER?: string;
		// Phase 2 async scheduled-governed Dream: live apply is hard-disabled
		// unless this is exactly "1". Kept absent in production through Phase 2.
		// See docs/pks-nightly-orchestrator-phase-2-dream-worker-prd-2026-06-16.md.
		PKS_ORCH_DREAM_LIVE_ENABLED?: string;
		// Durable semantic consolidation queue. Unset/off preserves the legacy
		// trigger path; live mode is fail-closed and only enqueues bounded work.
		DREAM_QUEUE_MODE?: "off" | "shadow" | "live";
		DREAM_MAINTENANCE_QUEUE?: Queue<import("./maintenanceQueue").MaintenanceMessage>;
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
