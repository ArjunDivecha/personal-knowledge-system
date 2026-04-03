import { env } from "cloudflare:workers";
import { createExecutionContext, waitOnExecutionContext } from "cloudflare:test";
import { beforeEach, describe, expect, it, vi } from "vitest";

const redisMock = vi.hoisted(() => ({
	get: vi.fn(),
	scard: vi.fn(),
	llen: vi.fn(),
}));

vi.mock("@upstash/redis/cloudflare", () => ({
	Redis: class MockRedis {
		constructor() {
			return redisMock as never;
		}
	},
}));

import worker from "../src/index";

const IncomingRequest = Request<unknown, IncomingRequestCfProperties>;

function getTestEnv(): Env {
	return {
		...env,
		UPSTASH_REDIS_REST_URL: "https://redis.test.local",
		UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
		UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
		UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
		OPENAI_API_KEY: "test-openai-key",
		GITHUB_TOKEN: "test-github-token",
		DREAM_OPERATOR_TOKEN: "test-dream-operator-token",
	};
}

async function dispatch(request: Request): Promise<Response> {
	const ctx = createExecutionContext();
	const response = await worker.fetch(request, getTestEnv(), ctx);
	await waitOnExecutionContext(ctx);
	return response;
}

beforeEach(() => {
	vi.clearAllMocks();
	redisMock.get.mockResolvedValue(null);
	redisMock.scard.mockResolvedValue(0);
	redisMock.llen.mockResolvedValue(0);
});

describe("Worker HTTP routes", () => {
	it("serves health with rollout metadata", async () => {
		const rawIndex = {
			generated_at: "2026-03-28T05:00:00.000Z",
			total_topic_count: 573,
			total_project_count: 36,
			tier_1_count: 500,
			tier_2_count: 24,
			tier_3_count: 85,
			archived_count: 0,
			topics: [{ id: "ke_123", domain: "Quantitative investing" }],
			projects: [{ id: "pe_123", name: "PKS memory upgrade" }],
		};
		const lastDreamRun = {
			run_at: "2026-03-28T03:00:00.000Z",
			status: "completed",
			dry_run: true,
			counts: {
				archive_candidates: 78,
			},
		};

		redisMock.get.mockImplementation(async (key: string) => {
			switch (key) {
				case "index:current":
					return rawIndex;
				case "dream:last_run":
					return lastDreamRun;
				case "migration:backfill_complete":
					return "2026-03-27T05:29:20+00:00";
				default:
					return null;
			}
		});
		redisMock.scard.mockResolvedValue(0);
		redisMock.llen.mockResolvedValue(0);

		const response = await dispatch(new IncomingRequest("https://example.com/health"));

		expect(response.status).toBe(200);
		expect(response.headers.get("content-type")).toContain("application/json");

		const payload = (await response.json()) as Record<string, unknown>;
		expect(payload.status).toBe("ok");
		expect(payload.migration_backfill_complete).toBe("2026-03-27T05:29:20+00:00");
		expect(payload.last_dream_status).toBe("completed");
		expect(payload.last_dream_archive_candidate_count).toBe(78);
		expect(payload.reconsolidation_error_count_today).toBe(0);
		expect(payload.pending_classification_count).toBe(0);
		expect(payload.thin_index).toEqual(
			expect.objectContaining({
				total_topic_count: 573,
				total_project_count: 36,
				tier_1_count: 500,
				tier_2_count: 24,
				tier_3_count: 85,
				archived_count: 0,
			}),
		);
	});

	it("rejects unauthorized operator writes", async () => {
		const response = await dispatch(
			new IncomingRequest("https://example.com/ops/dream/run", {
				method: "POST",
				headers: { "content-type": "application/json" },
				body: JSON.stringify({ dry_run: true }),
			}),
		);

		expect(response.status).toBe(401);
		expect(await response.json()).toEqual({ error: "Unauthorized" });
	});

	it("serves the landing page", async () => {
		const response = await dispatch(new IncomingRequest("https://example.com/"));

		expect(response.status).toBe(200);
		expect(response.headers.get("content-type")).toContain("text/html");
		const html = await response.text();
		expect(html).toContain("Personal Knowledge MCP Server");
		expect(html).toContain("/mcp");
		expect(html).toContain("/health");
	});

	it("handles unauthenticated HEAD probes on MCP routes", async () => {
		const response = await dispatch(
			new IncomingRequest("https://example.com/mcp", {
				method: "HEAD",
			}),
		);

		expect(response.status).toBe(401);
		expect(response.headers.get("www-authenticate")).toContain('resource_metadata="https://example.com/mcp/.well-known/oauth-protected-resource"');
		expect(response.headers.get("access-control-allow-methods")).toContain("HEAD");
		expect(response.headers.get("access-control-allow-origin")).toBe("*");
	});

	it("serves CORS preflight for MCP routes", async () => {
		const response = await dispatch(
			new IncomingRequest("https://example.com/mcp", {
				method: "OPTIONS",
				headers: {
					origin: "https://claude.ai",
					"access-control-request-method": "POST",
				},
			}),
		);

		expect(response.status).toBe(204);
		expect(response.headers.get("access-control-allow-origin")).toBe("https://claude.ai");
		expect(response.headers.get("access-control-allow-methods")).toContain("POST");
		expect(response.headers.get("access-control-allow-headers")).toContain("Authorization");
	});

	it("serves protected resource metadata on the MCP-relative path", async () => {
		const response = await dispatch(
			new IncomingRequest("https://example.com/mcp/.well-known/oauth-protected-resource"),
		);

		expect(response.status).toBe(200);
		const payload = (await response.json()) as Record<string, unknown>;
		expect(payload).toEqual(
			expect.objectContaining({
				resource: "https://example.com/mcp",
				authorization_servers: ["https://example.com"],
				scopes_supported: ["mcp:read", "mcp:write"],
			}),
		);
	});

	it("serves OAuth authorization metadata on Claude-compatible aliases", async () => {
		const openIdResponse = await dispatch(
			new IncomingRequest("https://example.com/.well-known/openid-configuration"),
		);
		expect(openIdResponse.status).toBe(200);
		expect(openIdResponse.headers.get("content-type")).toContain("application/json");
		const openIdPayload = (await openIdResponse.json()) as Record<string, unknown>;
		expect(openIdPayload).toEqual(
			expect.objectContaining({
				issuer: "https://example.com",
				authorization_endpoint: "https://example.com/authorize",
				token_endpoint: "https://example.com/token",
				registration_endpoint: "https://example.com/register",
			}),
		);

		const relativeResponse = await dispatch(
			new IncomingRequest("https://example.com/mcp/.well-known/oauth-authorization-server"),
		);
		expect(relativeResponse.status).toBe(200);
		const relativePayload = (await relativeResponse.json()) as Record<string, unknown>;
		expect(relativePayload).toEqual(
			expect.objectContaining({
				issuer: "https://example.com",
				authorization_endpoint: "https://example.com/authorize",
			}),
		);
	});
});
