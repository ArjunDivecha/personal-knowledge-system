import { cloudflareTest } from "@cloudflare/vitest-pool-workers";
import { defineConfig } from "vitest/config";

export default defineConfig({
	plugins: [
		cloudflareTest({
			wrangler: { configPath: "./wrangler.json" },
			miniflare: {
				bindings: {
					UPSTASH_REDIS_REST_URL: "https://redis.test.local",
					UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
					UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
					UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
					OPENAI_API_KEY: "test-openai-key",
					GITHUB_TOKEN: "test-github-token",
					DREAM_OPERATOR_TOKEN: "test-dream-operator-token",
				},
			},
		}),
	],
	test: {
		deps: {
			optimizer: {
				ssr: {
					enabled: true,
					include: ["ajv", "ajv-formats"],
				},
			},
		},
		include: ["test/**/*.test.ts"],
	},
});
