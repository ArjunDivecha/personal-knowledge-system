import { env } from "cloudflare:workers";
import { createExecutionContext, waitOnExecutionContext } from "cloudflare:test";
import { beforeEach, describe, expect, it, vi } from "vitest";

const redisMock = vi.hoisted(() => ({
	get: vi.fn(),
	set: vi.fn(),
	incr: vi.fn(),
	scard: vi.fn(),
	llen: vi.fn(),
	lpush: vi.fn(),
	ltrim: vi.fn(),
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

function getTestEnv(sourceFirstMode: "off" | "on" = "off"): Env {
	return {
		...env,
		UPSTASH_REDIS_REST_URL: "https://redis.test.local",
		UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
		UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
		UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
		OPENAI_API_KEY: "test-openai-key",
		GITHUB_TOKEN: "test-github-token",
		DREAM_OPERATOR_TOKEN: "test-dream-operator-token",
		SOURCE_FIRST_MODE: sourceFirstMode,
	};
}

async function dispatch(request: Request, sourceFirstMode: "off" | "on" = "off"): Promise<Response> {
	const ctx = createExecutionContext();
	const response = await worker.fetch(request, getTestEnv(sourceFirstMode), ctx);
	await waitOnExecutionContext(ctx);
	return response;
}

beforeEach(() => {
	vi.clearAllMocks();
	redisMock.get.mockResolvedValue(null);
	redisMock.set.mockResolvedValue("OK");
	redisMock.incr.mockResolvedValue(1);
	redisMock.scard.mockResolvedValue(0);
	redisMock.llen.mockResolvedValue(0);
	redisMock.lpush.mockResolvedValue(1);
	redisMock.ltrim.mockResolvedValue("OK");
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
		const lastDreamProposal = {
			run_id: "dpr_2026-03-28T07-10-00-000Z",
			created_at: "2026-03-28T07:10:00.000Z",
			status: "proposal_ready",
			actor_id: "scheduled:dream-governance",
			risk_score: "low",
			operations: [
				{ operation_id: "dop_archive_ke_1", type: "archive_entry" },
				{ operation_id: "dop_archive_ke_2", type: "archive_entry" },
			],
			counts: {
				archive_candidates: 83,
			},
		};

		redisMock.get.mockImplementation(async (key: string) => {
			switch (key) {
				case "index:current":
					return rawIndex;
				case "dream:last_run":
					return lastDreamRun;
				case "dream:proposal:last":
					return lastDreamProposal;
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
		expect(payload.last_dream_proposal_run).toBe("dpr_2026-03-28T07-10-00-000Z");
		expect(payload.last_dream_proposal_at).toBe("2026-03-28T07:10:00.000Z");
		expect(payload.last_dream_proposal_status).toBe("proposal_ready");
		expect(payload.last_dream_proposal_actor).toBe("scheduled:dream-governance");
		expect(payload.last_dream_proposal_operation_count).toBe(2);
		expect(payload.last_dream_proposal_risk).toBe("low");
		expect(payload.last_dream_proposal_archive_candidate_count).toBe(83);
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

	it("reports source-first heartbeat freshness in health", async () => {
		const publishedAt = new Date().toISOString();
		redisMock.get.mockImplementation(async (key: string) => {
			switch (key) {
				case "sf:current_generation":
					return "sf_health";
				case "sf:manifest:sf_health":
					return { generation: "sf_health", built_at: publishedAt, published_at: publishedAt, evidence_count: 4, project_count: 1 };
				case "sf:heartbeat":
					return { generation: "sf_health", published_at: publishedAt };
				default:
					return null;
			}
		});
		redisMock.scard.mockResolvedValue(0);
		redisMock.llen.mockResolvedValue(0);

		const response = await dispatch(new IncomingRequest("https://example.com/health"), "on");
		expect(response.status).toBe(200);
		const payload = (await response.json()) as Record<string, any>;
		expect(payload.status).toBe("ok");
		expect(payload.source_first).toMatchObject({
			enabled: true,
			generation: "sf_health",
			freshness: { status: "fresh" },
		});
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

	it("hard-disables legacy Dream mutation routes in source-first mode", async () => {
		const response = await dispatch(
			new IncomingRequest("https://example.com/ops/dream/apply", {
				method: "POST",
				headers: {
					authorization: "Bearer test-dream-operator-token",
					"content-type": "application/json",
				},
				body: JSON.stringify({ proposal_id: "dpr_old", mutation_id: "mut_old", reason: "must be blocked" }),
			}),
			"on",
		);

		expect(response.status).toBe(410);
		expect(await response.json()).toEqual({
			error: "legacy_write_surface_disabled",
			mode: "source_first",
			immutable: true,
		});
	});

	it("rejects unauthorized Dream proposal apply calls", async () => {
		const response = await dispatch(
			new IncomingRequest("https://example.com/ops/dream/apply", {
				method: "POST",
				headers: { "content-type": "application/json" },
				body: JSON.stringify({
					proposal_id: "dpr_missing",
					mutation_id: "mut_missing",
					reason: "test unauthorized apply",
				}),
			}),
		);

		expect(response.status).toBe(401);
		expect(await response.json()).toEqual({ error: "Unauthorized" });
	});

	it("serves authorized Dream proposal apply through the operator endpoint", async () => {
		const response = await dispatch(
			new IncomingRequest("https://example.com/ops/dream/apply", {
				method: "POST",
				headers: {
					authorization: "Bearer test-dream-operator-token",
					"content-type": "application/json",
				},
				body: JSON.stringify({
					proposal_id: "dpr_missing",
					mutation_id: "mut_missing_apply",
					reason: "test authorized apply",
				}),
			}),
		);

		expect(response.status).toBe(200);
		const payload = (await response.json()) as Record<string, unknown>;
		expect(payload).toMatchObject({
			ok: false,
			error: "proposal_not_found",
			proposal_id: "dpr_missing",
			mutation_id: "mut_missing_apply",
		});
		expect(redisMock.set).toHaveBeenCalledWith(
			"mutation_result:mut_missing_apply",
			expect.any(String),
			expect.objectContaining({ ex: expect.any(Number) }),
		);
	});

	it("runs an authorized scheduled-equivalent Dream proposal without live mutation", async () => {
		redisMock.get.mockImplementation(async (key: string) => {
			if (key === "migration:backfill_complete") {
				return "2026-03-27T05:29:20+00:00";
			}
			if (key === "knowledge:ke_candidate") {
				return {
					id: "ke_candidate",
					type: "knowledge",
					domain: "Low salience candidate",
					state: "active",
					current_view: "Tiny one-off memory",
					confidence: "low",
					related_knowledge: [],
					metadata: {
						schema_version: 2,
						context_type: "task_query",
						injection_tier: 3,
						salience_score: 0.01,
						mention_count: 1,
						access_count: 0,
						source_conversations: ["conv_candidate"],
						archived: false,
					},
				};
			}
			return null;
		});
		redisMock.scan = vi.fn()
			.mockResolvedValueOnce(["0", ["knowledge:ke_candidate"]])
			.mockResolvedValueOnce(["0", []])
			.mockResolvedValueOnce(["0", []]);
		redisMock.mget = vi.fn().mockResolvedValue([await redisMock.get("knowledge:ke_candidate")]);

		const response = await dispatch(
			new IncomingRequest("https://example.com/ops/dream/proposal", {
				method: "POST",
				headers: {
					authorization: "Bearer test-dream-operator-token",
					"content-type": "application/json",
				},
				body: JSON.stringify({ scheduled_equivalent: true }),
			}),
		);

		expect(response.status).toBe(200);
		const payload = (await response.json()) as Record<string, any>;
		expect(payload.status).toBe("proposal_ready");
		expect(payload.actor_id).toBe("scheduled:dream-governance");
		expect(payload.counts.archive_limit).toBe(50);
		expect(payload.counts.promotion_limit).toBe(10);
		expect(payload.dry_run).toBe(true);
		expect(redisMock.set).toHaveBeenCalledWith(
			"dream:proposal:last",
			expect.any(String),
		);
		expect(redisMock.set).not.toHaveBeenCalledWith(
			expect.stringMatching(/^knowledge:/),
			expect.anything(),
		);
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
