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
		MCP_OBJECT: DurableObjectNamespace<import("./index").KnowledgeMCP>;
		OAUTH_KV: KVNamespace;
		OAUTH_PROVIDER: OAuthHelpers;
	}
}

export {};
