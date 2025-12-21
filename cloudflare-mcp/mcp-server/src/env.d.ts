// Environment variable types for Cloudflare Workers
interface Env {
	UPSTASH_REDIS_REST_URL: string;
	UPSTASH_REDIS_REST_TOKEN: string;
	UPSTASH_VECTOR_REST_URL: string;
	UPSTASH_VECTOR_REST_TOKEN: string;
	OPENAI_API_KEY: string;
	MCP_OBJECT: DurableObjectNamespace<import("./index").KnowledgeMCP>;
}

