import { env } from "cloudflare:workers";
import { createExecutionContext, waitOnExecutionContext } from "cloudflare:test";
import { beforeEach, describe, expect, it, vi } from "vitest";

const redisMock = vi.hoisted(() => ({
	get: vi.fn(),
	scard: vi.fn(),
	llen: vi.fn(),
	incr: vi.fn(),
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

async function readRpcEnvelope(response: Response): Promise<Record<string, unknown>> {
	const contentType = response.headers.get("content-type") || "";
	const text = await response.text();

	if (!contentType.includes("text/event-stream")) {
		return JSON.parse(text) as Record<string, unknown>;
	}

	const dataLine = text
		.split("\n")
		.find((line) => line.startsWith("data: "));
	if (!dataLine) {
		throw new Error(`No SSE data line found in response: ${text}`);
	}

	return JSON.parse(dataLine.slice("data: ".length)) as Record<string, unknown>;
}

async function authorizeClient(baseUrl: string): Promise<{
	accessToken: string;
	clientId: string;
	clientSecret: string;
}> {
	return authorizeClientWithScope(baseUrl, "mcp:read");
}

async function authorizeClientWithScope(
	baseUrl: string,
	scope: string,
	options?: { operatorToken?: string; resource?: string },
): Promise<{
	accessToken: string;
	clientId: string;
	clientSecret: string;
}> {
	const metadataResponse = await dispatch(
		new IncomingRequest(`${baseUrl}/.well-known/oauth-authorization-server`),
	);
	const metadata = (await metadataResponse.json()) as Record<string, string>;

	const registerResponse = await dispatch(
		new IncomingRequest(metadata.registration_endpoint, {
			method: "POST",
			headers: { "content-type": "application/json" },
			body: JSON.stringify({
				client_name: "worker-runtime-test",
				redirect_uris: ["http://127.0.0.1:9881/callback"],
				grant_types: ["authorization_code", "refresh_token"],
				response_types: ["code"],
				token_endpoint_auth_method: "client_secret_post",
				scope,
			}),
		}),
	);
	expect(registerResponse.status).toBe(201);
	const client = (await registerResponse.json()) as Record<string, string>;

	const authorizeUrl = new URL(metadata.authorization_endpoint);
	authorizeUrl.searchParams.set("response_type", "code");
	authorizeUrl.searchParams.set("client_id", client.client_id);
	authorizeUrl.searchParams.set("redirect_uri", "http://127.0.0.1:9881/callback");
	authorizeUrl.searchParams.set("scope", scope);
	authorizeUrl.searchParams.set("state", "worker-runtime-test");
	if (options?.resource) {
		authorizeUrl.searchParams.set("resource", options.resource);
	}

	const authorizeResponse = await dispatch(
		new IncomingRequest(authorizeUrl.toString(), {
			headers: options?.operatorToken
				? { authorization: `Bearer ${options.operatorToken}` }
				: undefined,
		}),
	);
	expect(authorizeResponse.status).toBe(302);
	const redirectTarget = authorizeResponse.headers.get("location");
	expect(redirectTarget).toBeTruthy();

	const redirectUrl = new URL(redirectTarget!);
	const code = redirectUrl.searchParams.get("code");
	expect(code).toBeTruthy();

	const tokenResponse = await dispatch(
		new IncomingRequest(metadata.token_endpoint, {
			method: "POST",
			headers: { "content-type": "application/x-www-form-urlencoded" },
			body: new URLSearchParams({
				grant_type: "authorization_code",
				code: code!,
				redirect_uri: "http://127.0.0.1:9881/callback",
				client_id: client.client_id,
				client_secret: client.client_secret,
				...(options?.resource ? { resource: options.resource } : {}),
			}),
		}),
	);
	expect(tokenResponse.status).toBe(200);
	const token = (await tokenResponse.json()) as Record<string, string>;

	return {
		accessToken: token.access_token,
		clientId: client.client_id,
		clientSecret: client.client_secret,
	};
}

beforeEach(() => {
	vi.clearAllMocks();
	redisMock.get.mockImplementation(async (key: string) => {
		switch (key) {
			case "index:current":
				return {
					generated_at: "2026-03-28T05:00:00.000Z",
					total_topic_count: 573,
					total_project_count: 36,
					tier_1_count: 500,
					tier_2_count: 24,
					tier_3_count: 85,
					archived_count: 0,
					topics: [
						{
							id: "ke_quant",
							domain: "Quantitative investing background",
							current_view_summary: "Thirty years in quantitative investing.",
							injection_tier: 1,
							context_type: "professional_identity",
							salience_score: 1,
							mention_count: 12,
							last_updated: "2026-03-27T00:00:00.000Z",
						},
					],
					projects: [
						{
							id: "pe_memory",
							name: "PKS memory upgrade",
							goal_summary: "Build a selective AI memory system.",
							status: "active",
							current_phase: "testing",
							last_touched: "2026-03-27T00:00:00.000Z",
							injection_tier: 1,
							context_type: "active_project",
							salience_score: 0.9,
							mention_count: 8,
						},
					],
				};
			case "dream:last_run":
				return {
					run_id: "dr_2026-03-28T03-00-00-000Z",
					run_at: "2026-03-28T03:00:00.000Z",
					status: "completed",
					dry_run: true,
					counts: {
						archive_candidates: 78,
					},
				};
			default:
				return null;
		}
	});
	redisMock.scard.mockResolvedValue(0);
	redisMock.llen.mockResolvedValue(0);
	redisMock.incr.mockResolvedValue(1);
});

describe("OAuth and MCP integration", () => {
	it("serves OAuth protected resource metadata for OpenAI endpoints", async () => {
		const response = await dispatch(
			new IncomingRequest("https://example.com/.well-known/oauth-protected-resource/openai/mcp"),
		);
		expect(response.status).toBe(200);
		const payload = (await response.json()) as Record<string, unknown>;
		expect(payload.resource).toBe("https://example.com");
		expect(payload.authorization_servers).toEqual(["https://example.com"]);
		expect(payload.scopes_supported).toEqual(["mcp:read"]);
	});

	it("completes OAuth and serves MCP tools through the real transport", async () => {
		const baseUrl = "https://example.com";
		const { accessToken } = await authorizeClient(baseUrl);

		const initializeResponse = await dispatch(
			new IncomingRequest(`${baseUrl}/mcp`, {
				method: "POST",
				headers: {
					authorization: `Bearer ${accessToken}`,
					accept: "application/json, text/event-stream",
					"content-type": "application/json",
				},
				body: JSON.stringify({
					jsonrpc: "2.0",
					id: 1,
					method: "initialize",
					params: {
						protocolVersion: "2024-11-05",
						capabilities: {},
						clientInfo: { name: "worker-runtime-test", version: "1.0" },
					},
				}),
			}),
		);
		expect(initializeResponse.status).toBe(200);
		const sessionId = initializeResponse.headers.get("mcp-session-id");
		expect(sessionId).toBeTruthy();
		const initializeEnvelope = await readRpcEnvelope(initializeResponse);
		expect(initializeEnvelope.result).toBeTruthy();

		const toolsListResponse = await dispatch(
			new IncomingRequest(`${baseUrl}/mcp`, {
				method: "POST",
				headers: {
					authorization: `Bearer ${accessToken}`,
					accept: "application/json, text/event-stream",
					"content-type": "application/json",
					"mcp-session-id": sessionId!,
				},
				body: JSON.stringify({
					jsonrpc: "2.0",
					id: 2,
					method: "tools/list",
					params: {},
				}),
			}),
		);
		const toolsEnvelope = await readRpcEnvelope(toolsListResponse);
		const tools = ((toolsEnvelope.result as Record<string, unknown>).tools as Array<Record<string, unknown>>)
			.map((tool) => tool.name);
		expect(tools).toEqual(
			expect.arrayContaining([
				"get_index",
				"get_context",
				"search",
				"get_dream_summary",
				"restore_archived",
				"set_context_type",
			]),
		);

		const getIndexResponse = await dispatch(
			new IncomingRequest(`${baseUrl}/mcp`, {
				method: "POST",
				headers: {
					authorization: `Bearer ${accessToken}`,
					accept: "application/json, text/event-stream",
					"content-type": "application/json",
					"mcp-session-id": sessionId!,
				},
				body: JSON.stringify({
					jsonrpc: "2.0",
					id: 3,
					method: "tools/call",
					params: {
						name: "get_index",
						arguments: {},
					},
				}),
			}),
		);
		const getIndexEnvelope = await readRpcEnvelope(getIndexResponse);
		const getIndexResult = getIndexEnvelope.result as Record<string, unknown>;
		const getIndexText = ((getIndexResult.content as Array<Record<string, unknown>>)[0].text as string);
		const thinIndex = JSON.parse(getIndexText) as Record<string, unknown>;
		expect(thinIndex.total_topics).toBe(573);
		expect(thinIndex.total_projects).toBe(36);
		expect(thinIndex.tier_1_count).toBe(500);

		const dreamSummaryResponse = await dispatch(
			new IncomingRequest(`${baseUrl}/mcp`, {
				method: "POST",
				headers: {
					authorization: `Bearer ${accessToken}`,
					accept: "application/json, text/event-stream",
					"content-type": "application/json",
					"mcp-session-id": sessionId!,
				},
				body: JSON.stringify({
					jsonrpc: "2.0",
					id: 4,
					method: "tools/call",
					params: {
						name: "get_dream_summary",
						arguments: {},
					},
				}),
			}),
		);
		const dreamEnvelope = await readRpcEnvelope(dreamSummaryResponse);
		const dreamResult = dreamEnvelope.result as Record<string, unknown>;
		const dreamText = ((dreamResult.content as Array<Record<string, unknown>>)[0].text as string);
		const dreamSummary = JSON.parse(dreamText) as Record<string, unknown>;
		expect(dreamSummary.status).toBe("completed");
		expect((dreamSummary.counts as Record<string, unknown>).archive_candidates).toBe(78);
	});

	it("downgrades public OAuth write requests to read-only scope", async () => {
		const baseUrl = "https://example.com";
		const { accessToken } = await authorizeClientWithScope(baseUrl, "mcp:read mcp:write");

		const initializeResponse = await dispatch(
			new IncomingRequest(`${baseUrl}/mcp`, {
				method: "POST",
				headers: {
					authorization: `Bearer ${accessToken}`,
					accept: "application/json, text/event-stream",
					"content-type": "application/json",
				},
				body: JSON.stringify({
					jsonrpc: "2.0",
					id: 1,
					method: "initialize",
					params: {
						protocolVersion: "2024-11-05",
						capabilities: {},
						clientInfo: { name: "worker-runtime-test", version: "1.0" },
					},
				}),
			}),
		);
		expect(initializeResponse.status).toBe(200);
		const sessionId = initializeResponse.headers.get("mcp-session-id");
		expect(sessionId).toBeTruthy();

		const writeResponse = await dispatch(
			new IncomingRequest(`${baseUrl}/mcp`, {
				method: "POST",
				headers: {
					authorization: `Bearer ${accessToken}`,
					accept: "application/json, text/event-stream",
					"content-type": "application/json",
					"mcp-session-id": sessionId!,
				},
				body: JSON.stringify({
					jsonrpc: "2.0",
					id: 2,
					method: "tools/call",
					params: {
						name: "set_context_type",
						arguments: {
							id: "ke_quant",
							context_type: "recurring_pattern",
							reason: "worker runtime test",
						},
					},
				}),
			}),
		);
		expect(writeResponse.status).toBe(200);
		const writeEnvelope = await readRpcEnvelope(writeResponse);
		const writeResult = writeEnvelope.result as Record<string, unknown>;
		const writeText = ((writeResult.content as Array<Record<string, unknown>>)[0].text as string);
		const payload = JSON.parse(writeText) as Record<string, unknown>;
		expect(String(payload.error || "")).toContain("mcp:write");
	});

	it("serves a read-only OpenAI tool surface on /openai/mcp", async () => {
		const baseUrl = "https://example.com";
		const { accessToken } = await authorizeClient(baseUrl);

		const initializeResponse = await dispatch(
			new IncomingRequest(`${baseUrl}/openai/mcp`, {
				method: "POST",
				headers: {
					authorization: `Bearer ${accessToken}`,
					accept: "application/json, text/event-stream",
					"content-type": "application/json",
				},
				body: JSON.stringify({
					jsonrpc: "2.0",
					id: 1,
					method: "initialize",
					params: {
						protocolVersion: "2024-11-05",
						capabilities: {},
						clientInfo: { name: "worker-runtime-test", version: "1.0" },
					},
				}),
			}),
		);
		expect(initializeResponse.status).toBe(200);
		const sessionId = initializeResponse.headers.get("mcp-session-id");
		expect(sessionId).toBeTruthy();

		const toolsListResponse = await dispatch(
			new IncomingRequest(`${baseUrl}/openai/mcp`, {
				method: "POST",
				headers: {
					authorization: `Bearer ${accessToken}`,
					accept: "application/json, text/event-stream",
					"content-type": "application/json",
					"mcp-session-id": sessionId!,
				},
				body: JSON.stringify({
					jsonrpc: "2.0",
					id: 2,
					method: "tools/list",
					params: {},
				}),
			}),
		);
		const toolsEnvelope = await readRpcEnvelope(toolsListResponse);
		const tools = ((toolsEnvelope.result as Record<string, unknown>).tools as Array<Record<string, unknown>>)
			.map((tool) => tool.name);
		expect(tools).toEqual(
			expect.arrayContaining([
				"get_index",
				"get_context",
				"search",
				"get_deep",
				"get_dream_summary",
				"github",
			]),
		);
		expect(tools).not.toContain("restore_archived");
		expect(tools).not.toContain("set_context_type");
	});

	it("accepts OpenAI resource-scoped OAuth requests on /openai/mcp", async () => {
		const baseUrl = "https://example.com";
		const { accessToken } = await authorizeClientWithScope(
			baseUrl,
			"mcp:read",
			{ resource: `${baseUrl}/openai/mcp` },
		);

		const initializeResponse = await dispatch(
			new IncomingRequest(`${baseUrl}/openai/mcp`, {
				method: "POST",
				headers: {
					authorization: `Bearer ${accessToken}`,
					accept: "application/json, text/event-stream",
					"content-type": "application/json",
				},
				body: JSON.stringify({
					jsonrpc: "2.0",
					id: 1,
					method: "initialize",
					params: {
						protocolVersion: "2024-11-05",
						capabilities: {},
						clientInfo: { name: "worker-runtime-test", version: "1.0" },
					},
				}),
			}),
		);

		expect(initializeResponse.status).toBe(200);
		expect(initializeResponse.headers.get("mcp-session-id")).toBeTruthy();
	});

	it("grants write scope only when /authorize is operator-authenticated", async () => {
		const baseUrl = "https://example.com";
		const { accessToken } = await authorizeClientWithScope(
			baseUrl,
			"mcp:read mcp:write",
			{ operatorToken: "test-dream-operator-token" },
		);

		const initializeResponse = await dispatch(
			new IncomingRequest(`${baseUrl}/mcp`, {
				method: "POST",
				headers: {
					authorization: `Bearer ${accessToken}`,
					accept: "application/json, text/event-stream",
					"content-type": "application/json",
				},
				body: JSON.stringify({
					jsonrpc: "2.0",
					id: 1,
					method: "initialize",
					params: {
						protocolVersion: "2024-11-05",
						capabilities: {},
						clientInfo: { name: "worker-runtime-test", version: "1.0" },
					},
				}),
			}),
		);
		expect(initializeResponse.status).toBe(200);
		const sessionId = initializeResponse.headers.get("mcp-session-id");
		expect(sessionId).toBeTruthy();

		const writeResponse = await dispatch(
			new IncomingRequest(`${baseUrl}/mcp`, {
				method: "POST",
				headers: {
					authorization: `Bearer ${accessToken}`,
					accept: "application/json, text/event-stream",
					"content-type": "application/json",
					"mcp-session-id": sessionId!,
				},
				body: JSON.stringify({
					jsonrpc: "2.0",
					id: 2,
					method: "tools/call",
					params: {
						name: "set_context_type",
						arguments: {
							id: "ke_quant",
							context_type: "recurring_pattern",
							reason: "worker runtime test",
						},
					},
				}),
			}),
		);
		expect(writeResponse.status).toBe(200);
		const writeEnvelope = await readRpcEnvelope(writeResponse);
		const writeResult = writeEnvelope.result as Record<string, unknown>;
		const writeText = ((writeResult.content as Array<Record<string, unknown>>)[0].text as string);
		const payload = JSON.parse(writeText) as Record<string, unknown>;
		expect(String(payload.error || "")).not.toContain("mcp:write");
		expect(String(payload.error || "")).toContain("Entry not found");
	});
});
