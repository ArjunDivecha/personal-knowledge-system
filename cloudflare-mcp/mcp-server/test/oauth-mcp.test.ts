import { env } from "cloudflare:workers";
import { createExecutionContext, waitOnExecutionContext } from "cloudflare:test";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import fxSimple from "./fixtures/fxtwitter-simple.json";

const redisMock = vi.hoisted(() => ({
	get: vi.fn(),
	set: vi.fn(),
	// setnx: createEntry -> generateEntryId claims ids atomically via SET NX.
	// The mock must implement it faithfully (set-if-absent, returns 1/0) or
	// createEntry throws "redis.setnx is not a function" and returns an error
	// envelope. Declared exactly once — a duplicate key here is a merge artifact.
	setnx: vi.fn(),
	rename: vi.fn(),
	del: vi.fn(),
	scan: vi.fn(),
	mget: vi.fn(),
	sadd: vi.fn(),
	srem: vi.fn(),
	lpush: vi.fn(),
	ltrim: vi.fn(),
	scard: vi.fn(),
	llen: vi.fn(),
	incr: vi.fn(),
}));

const vectorMock = vi.hoisted(() => ({
	query: vi.fn(),
	update: vi.fn(),
	upsert: vi.fn(),
	delete: vi.fn(),
}));

const openaiEmbeddingsCreateMock = vi.hoisted(() => vi.fn());

vi.mock("@upstash/redis/cloudflare", () => ({
	Redis: class MockRedis {
		constructor() {
			return redisMock as never;
		}
	},
}));

vi.mock("@upstash/vector", () => ({
	Index: class MockIndex {
		constructor() {
			return vectorMock as never;
		}
	},
}));

vi.mock("openai", () => ({
	default: class MockOpenAI {
		embeddings = {
			create: openaiEmbeddingsCreateMock,
		};
	},
}));

import worker from "../src/index";

const IncomingRequest = Request<unknown, IncomingRequestCfProperties>;
let redisStore: Record<string, unknown>;

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
		// This suite preserves the archived legacy MCP contract. Source-first has
		// its own focused contract tests and is exercised against production E2E.
		SOURCE_FIRST_MODE: "off",
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
	grantedScope: string;
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
	grantedScope: string;
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
		grantedScope: token.scope,
	};
}

beforeEach(() => {
	vi.clearAllMocks();
	redisStore = {
		"index:current": JSON.stringify({
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
						archived: false,
					},
					{
						id: "ke_quant_old",
						domain: "Quantitative investing background",
						current_view_summary: "Built multiple quantitative research pipelines.",
						injection_tier: 1,
						context_type: "professional_identity",
						salience_score: 0.8,
						mention_count: 4,
						last_updated: "2026-03-26T00:00:00.000Z",
						archived: false,
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
					archived: false,
				},
				// PKS-PROJECT-LIFECYCLE-001 INV4 fixture: a dormant project must
				// never appear in `projects`, only in the distinct `dormant_projects`
				// section — see the get_index assertions below.
				{
					id: "pe_stale_2024",
					name: "SPX MA200 Strategy Backtest",
					goal_summary: "One-shot 2024 backtest session.",
					status: "dormant",
					current_phase: "abandoned",
					last_touched: "2024-08-25T00:00:00.000Z",
					injection_tier: 2,
					context_type: "active_project",
					salience_score: 0.05,
					mention_count: 1,
					archived: false,
				},
			],
		}),
		"dream:last_run": {
			run_id: "dr_2026-03-28T03-00-00-000Z",
			run_at: "2026-03-28T03:00:00.000Z",
			status: "completed",
			dry_run: true,
			counts: {
				archive_candidates: 78,
			},
		},
		"dream:runs:index": ["dr_2026-03-28T03-00-00-000Z"],
		"dream:run:dr_2026-03-28T03-00-00-000Z": {
			run_id: "dr_2026-03-28T03-00-00-000Z",
			run_at: "2026-03-28T03:00:00.000Z",
			completed_at: "2026-03-28T03:01:00.000Z",
			status: "completed",
			trigger: "scheduled",
			dry_run: true,
			counts: {
				archive_candidates: 78,
			},
		},
		"dream:run:dr_2026-03-28T03-00-00-000Z:events": [
			{ phase: "survey", status: "completed" },
		],
		"validation:gate_status": {
			schema_version: 1,
			updated_at: "2026-05-07T18:40:36+00:00",
			overall_status: "green",
			overall_passed: true,
			gates: {
				verify_memory_full_strict: {
					status: "pass",
					passed: true,
					report_path: "/tmp/verify.json",
				},
			},
		},
		"validation:last": {
			schema_version: 1,
			gate: "verify_memory_full_strict",
			status: "pass",
			passed: true,
			report_path: "/tmp/verify.json",
		},
			"knowledge:ke_quant": {
				id: "ke_quant",
				type: "knowledge",
				domain: "Quantitative investing background",
				current_view: "Thirty years in quantitative investing.",
				state: "active",
				confidence: "medium",
				positions: [],
					key_insights: [],
					knows_how_to: [],
					open_questions: [],
					related_repos: [],
					related_knowledge: [
						{ knowledge_id: "ke_quant_old", relationship: "contradicts" },
					],
					evolution: [],
				metadata: {
					created_at: "2026-03-20T00:00:00.000Z",
					updated_at: "2026-03-27T00:00:00.000Z",
					source_conversations: ["conv_quant"],
				source_messages: [],
				context_type: "professional_identity",
				injection_tier: 1,
				salience_score: 1,
				mention_count: 12,
				access_count: 0,
				revision: 0,
				archived: false,
			},
			},
			"knowledge:ke_quant_old": {
				id: "ke_quant_old",
				type: "knowledge",
				domain: "Quantitative investing background",
				current_view: "Built multiple quantitative research pipelines across market regimes.",
				state: "contested",
				confidence: "low",
				positions: [],
				key_insights: [
					{
						insight: "Has built multiple quant research pipelines.",
						evidence: {
							conversation_id: "conv_quant_old",
							message_ids: ["msg_quant_old_1"],
							snippet: "Built multiple research pipelines over many years.",
						},
					},
				],
				knows_how_to: [],
				open_questions: [],
				related_repos: [],
				related_knowledge: [
					{ knowledge_id: "ke_quant", relationship: "contradicts" },
				],
				evolution: [],
				metadata: {
					created_at: "2026-03-18T00:00:00.000Z",
					updated_at: "2026-03-26T00:00:00.000Z",
					source_conversations: ["conv_quant_old"],
					source_messages: ["msg_quant_old_1"],
					context_type: "professional_identity",
					injection_tier: 1,
					salience_score: 0.8,
					mention_count: 4,
					access_count: 0,
					revision: 0,
					archived: false,
				},
			},
			"project:pe_memory": {
				id: "pe_memory",
				type: "project",
				name: "PKS memory upgrade",
				status: "active",
				detail_level: "full",
				goal: "Build a selective AI memory system.",
				current_phase: "testing",
				blocked_on: null,
				decisions_made: [],
				tech_stack: [],
				related_repos: [],
				related_knowledge: [],
				phase_history: [],
				metadata: {
					created_at: "2026-03-20T00:00:00.000Z",
					updated_at: "2026-03-27T00:00:00.000Z",
					source_conversations: ["conv_memory"],
					source_messages: [],
					context_type: "active_project",
					injection_tier: 1,
					salience_score: 0.9,
					mention_count: 8,
					access_count: 0,
					revision: 0,
					archived: false,
				},
			},
		};
	redisMock.get.mockImplementation(async (key: string) => {
		if (!(key in redisStore)) {
			return null;
		}
		const value = redisStore[key];
		if (value && typeof value === "object") {
			return JSON.parse(JSON.stringify(value));
		}
		return value;
	});
	redisMock.set.mockImplementation(async (key: string, value: unknown) => {
		redisStore[key] = value;
		return "OK";
	});
	// SET NX semantics (4.1 atomic entry-id allocation): 1 if newly set, 0 if the key exists.
	redisMock.setnx.mockImplementation(async (key: string, value: unknown) => {
		if (key in redisStore) return 0;
		redisStore[key] = value;
		return 1;
	});
	redisMock.rename.mockImplementation(async (source: string, destination: string) => {
		if (!(source in redisStore)) {
			throw new Error(`ERR no such key: ${source}`);
		}
		redisStore[destination] = redisStore[source];
		delete redisStore[source];
		return "OK";
	});
	redisMock.del.mockImplementation(async (...keys: string[]) => {
		let deleted = 0;
		for (const key of keys) {
			if (key in redisStore) {
				delete redisStore[key];
				deleted += 1;
			}
		}
		return deleted;
	});
	redisMock.scan.mockImplementation(async (cursor: string, options: { match?: string }) => {
		if (cursor !== "0") return ["0", []];
		const match = options?.match ?? "*";
		const prefix = match.endsWith("*") ? match.slice(0, -1) : match;
		const keys = Object.keys(redisStore).filter((key) => key.startsWith(prefix));
		return ["0", keys];
	});
	redisMock.mget.mockImplementation(async (keys: string[] | string) => {
		const keyList = Array.isArray(keys) ? keys : [keys];
		return keyList.map((key) => {
			if (!(key in redisStore)) return null;
			const value = redisStore[key];
			if (value && typeof value === "object") {
				return JSON.parse(JSON.stringify(value));
			}
			return value;
		});
	});
	redisMock.sadd.mockResolvedValue(1);
	redisMock.srem.mockResolvedValue(1);
	redisMock.lpush.mockImplementation(async (key: string, value: unknown) => {
		const current = Array.isArray(redisStore[key]) ? [...(redisStore[key] as unknown[])] : [];
		current.unshift(value);
		redisStore[key] = current;
		return current.length;
	});
	redisMock.ltrim.mockImplementation(async (key: string, start: number, stop: number) => {
		if (!Array.isArray(redisStore[key])) {
			return "OK";
		}
		const current = redisStore[key] as unknown[];
		redisStore[key] = current.slice(start, stop + 1);
		return "OK";
	});
	redisMock.scard.mockResolvedValue(0);
	redisMock.llen.mockResolvedValue(0);
	redisMock.incr.mockResolvedValue(1);
	redisMock.setnx.mockImplementation(async (key: string, value: unknown) => {
		if (key in redisStore) {
			return 0;
		}
		redisStore[key] = value;
		return 1;
	});
	vectorMock.update.mockResolvedValue(undefined);
	vectorMock.upsert.mockResolvedValue(undefined);
	vectorMock.delete.mockResolvedValue(undefined);
	vectorMock.query.mockResolvedValue([
		{
			id: "ke_quant",
			score: 0.92,
			metadata: {
				type: "knowledge",
				archived: false,
				source: "fixture",
			},
		},
		{
			id: "ke_quant_old",
			score: 0.88,
			metadata: {
				type: "knowledge",
				archived: false,
				source: "fixture",
			},
		},
	]);
	openaiEmbeddingsCreateMock.mockResolvedValue({
		data: [{ embedding: [0.1, 0.2, 0.3] }],
	});
});

afterEach(() => {
	vi.unstubAllGlobals();
});

describe("OAuth and MCP integration", () => {
	it("serves OAuth protected resource metadata for OpenAI endpoints", async () => {
		const response = await dispatch(
			new IncomingRequest("https://example.com/.well-known/oauth-protected-resource/openai/mcp"),
		);
		expect(response.status).toBe(200);
		const payload = (await response.json()) as Record<string, unknown>;
		expect(payload.resource).toBe("https://example.com/openai/mcp");
		expect(payload.authorization_servers).toEqual(["https://example.com"]);
		expect(payload.scopes_supported).toEqual(["mcp:read"]);
	});

	it("returns a standards-friendly dynamic client registration payload", async () => {
		const metadataResponse = await dispatch(
			new IncomingRequest("https://example.com/.well-known/oauth-authorization-server"),
		);
		const metadata = (await metadataResponse.json()) as Record<string, string>;

		const registerResponse = await dispatch(
			new IncomingRequest(metadata.registration_endpoint, {
				method: "POST",
				headers: { "content-type": "application/json" },
				body: JSON.stringify({
					client_name: "claude-test",
					redirect_uris: ["https://claude.ai/api/mcp/auth_callback"],
					grant_types: ["authorization_code", "refresh_token"],
					response_types: ["code"],
					token_endpoint_auth_method: "client_secret_post",
				}),
			}),
		);

		expect(registerResponse.status).toBe(201);
		const client = (await registerResponse.json()) as Record<string, unknown>;
		expect(client.registration_client_uri).toBeUndefined();
		expect(client.client_secret).toEqual(expect.any(String));
		expect(client.client_secret_expires_at).toBe(0);
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
					"create_entry",
					"get_dream_summary",
					"get_validation_status",
					"list_dream_runs",
					"get_dream_run",
					"get_dream_events",
					"read_tweet",
					"read_thread",
					"health",
					"run_dream_proposal",
					"grade_dream_proposal",
					"apply_dream_proposal",
					"rollback_dream_apply",
					"add_insight",
					"archive_entry",
					"consolidate_entries",
					"restore_entry",
					"restore_archived",
					"set_context_type",
					"update_entry",
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
		// PKS-PROJECT-LIFECYCLE-001 INV4: the dormant fixture project must be
		// distinctly separated, never mixed into the default `projects` list.
		const indexProjects = thinIndex.projects as Array<Record<string, unknown>>;
		const indexDormantProjects = thinIndex.dormant_projects as Array<Record<string, unknown>>;
		expect(indexProjects.some((p) => p.id === "pe_stale_2024")).toBe(false);
		expect(indexProjects.some((p) => p.id === "pe_memory")).toBe(true);
		expect(indexDormantProjects.some((p) => p.id === "pe_stale_2024")).toBe(true);
		expect(indexDormantProjects.find((p) => p.id === "pe_stale_2024")?.status).toBe("dormant");
		expect(thinIndex.dormant_project_count).toBe(1);

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

		const validationResponse = await dispatch(
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
					id: 5,
					method: "tools/call",
					params: {
						name: "get_validation_status",
						arguments: {},
					},
				}),
			}),
		);
		const validationEnvelope = await readRpcEnvelope(validationResponse);
		const validationResult = validationEnvelope.result as Record<string, unknown>;
		const validationText = ((validationResult.content as Array<Record<string, unknown>>)[0].text as string);
		const validationStatus = JSON.parse(validationText) as Record<string, any>;
		expect(validationStatus.gate_status.overall_status).toBe("green");

		const listRunsResponse = await dispatch(
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
					id: 6,
					method: "tools/call",
					params: {
						name: "list_dream_runs",
						arguments: { limit: 5 },
					},
				}),
			}),
		);
		const listRunsEnvelope = await readRpcEnvelope(listRunsResponse);
		const listRunsText = (((listRunsEnvelope.result as Record<string, unknown>).content as Array<Record<string, unknown>>)[0].text as string);
		const listRuns = JSON.parse(listRunsText) as Record<string, any>;
		expect(listRuns.runs[0].run_id).toBe("dr_2026-03-28T03-00-00-000Z");

		const fetchMock = vi.fn(async () => Response.json(fxSimple));
		vi.stubGlobal("fetch", fetchMock);
		const readTweetResponse = await dispatch(
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
					id: 7,
					method: "tools/call",
					params: {
						name: "read_tweet",
						arguments: { url: "https://x.com/jack/status/20?s=20" },
					},
				}),
			}),
		);
		const readTweetEnvelope = await readRpcEnvelope(readTweetResponse);
		const readTweetText = (((readTweetEnvelope.result as Record<string, unknown>).content as Array<Record<string, unknown>>)[0].text as string);
		const tweet = JSON.parse(readTweetText) as Record<string, any>;
		expect(tweet).toMatchObject({
			id: "20",
			text: "just setting up my twttr",
			source_api: "fxtwitter",
			author: { username: "jack" },
		});
		expect(fetchMock).toHaveBeenCalledWith(
			"https://api.fxtwitter.com/2/status/20?about_account=1",
			expect.objectContaining({
				headers: expect.objectContaining({
					accept: "application/json",
					"user-agent": expect.stringContaining("Mozilla/5.0"),
				}),
			}),
		);
		expect(redisStore["tweet_reader:queue"]).toBeTruthy();
	});

	it("grants write scope for standard /mcp OAuth requests", async () => {
		const baseUrl = "https://example.com";
		const { grantedScope } = await authorizeClientWithScope(baseUrl, "mcp:read mcp:write");
		expect(grantedScope).toBe("mcp:read mcp:write");
	});

	it("accepts Claude resource-scoped OAuth requests on /mcp", async () => {
		const baseUrl = "https://example.com";
		const { accessToken, grantedScope } = await authorizeClientWithScope(
			baseUrl,
			"mcp:read mcp:write",
			{ resource: `${baseUrl}/mcp` },
		);
		expect(grantedScope).toBe("mcp:read mcp:write");

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
		expect(initializeResponse.headers.get("mcp-session-id")).toBeTruthy();
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
				"get_validation_status",
				"list_dream_runs",
				"get_dream_run",
				"get_dream_events",
				"read_tweet",
				"read_thread",
				"health",
				"github",
			]),
			);
			expect(tools).not.toContain("add_insight");
			expect(tools).not.toContain("archive_entry");
			expect(tools).not.toContain("consolidate_entries");
			expect(tools).not.toContain("create_entry");
			expect(tools).not.toContain("restore_entry");
			expect(tools).not.toContain("restore_archived");
			expect(tools).not.toContain("run_dream_proposal");
			expect(tools).not.toContain("grade_dream_proposal");
			expect(tools).not.toContain("rollback_dream_apply");
			expect(tools).not.toContain("set_context_type");
			expect(tools).not.toContain("update_entry");
			expect(tools).not.toContain("apply_dream_proposal");
			expect(tools).not.toContain("rollback_dream_run");
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

	it("returns Phase 8 retrieval diagnostics from the live search tool", async () => {
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

		const searchResponse = await dispatch(
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
					method: "tools/call",
					params: {
						name: "search",
						arguments: {
							query: "what is the quantitative investing background now?",
							limit: 2,
						},
					},
				}),
			}),
		);

		expect(searchResponse.status).toBe(200);
		const searchEnvelope = await readRpcEnvelope(searchResponse);
		const searchText = (((searchEnvelope.result as Record<string, unknown>).content as Array<Record<string, unknown>>)[0].text as string);
		const payload = JSON.parse(searchText) as Record<string, any>;
		expect(payload.phase8_retrieval).toMatchObject({
			intent: "current_answer",
			temporal_mode: "current",
		});
		expect(payload.results[0].id).toBe("ke_quant");
		expect(payload.results[0].phase8_multiplier).toEqual(expect.any(Number));
		expect(payload.results[0].phase8_retrieval).toMatchObject({
			final_score: expect.any(Number),
			lexical_score: expect.any(Number),
			vector_score: expect.any(Number),
			reasons: expect.arrayContaining(["current_surface_preferred"]),
		});
		expect(payload.scoring).toContain("Phase 8");
		expect(vectorMock.query).toHaveBeenCalled();
	});

	it("downgrades OpenAI resource write requests to read-only scope", async () => {
		const baseUrl = "https://example.com";
		const { accessToken, grantedScope } = await authorizeClientWithScope(
			baseUrl,
			"mcp:read mcp:write",
			{ resource: `${baseUrl}/openai/mcp` },
		);
		expect(grantedScope).toBe("mcp:read");

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
		expect(tools).not.toContain("add_insight");
		expect(tools).not.toContain("archive_entry");
		expect(tools).not.toContain("create_entry");
		expect(tools).not.toContain("update_entry");
	});

	it("grants write scope on /mcp tool calls", async () => {
		const baseUrl = "https://example.com";
		const { accessToken } = await authorizeClientWithScope(
			baseUrl,
			"mcp:read mcp:write",
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
								id: "ke_missing",
								expected_revision: 0,
								mutation_id: "mut_missing_set_context_operator",
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
			expect(String(payload.error || "")).toContain("entry_not_found");
		});

		it("creates a knowledge entry through the write-scoped MCP tool", async () => {
			const baseUrl = "https://example.com";
			const { accessToken } = await authorizeClientWithScope(
				baseUrl,
				"mcp:read mcp:write",
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
			const sessionId = initializeResponse.headers.get("mcp-session-id");
			expect(sessionId).toBeTruthy();

			const createResponse = await dispatch(
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
							name: "create_entry",
							arguments: {
								mutation_id: "mut_create_ke_loopilot",
								reason: "Persist a new durable summary from the current chat.",
								domain: "LoopPilot MCP write API smoke test",
								current_view: "LoopPilot now supports direct MCP write operations for durable memory creation.",
								context_type: "explicit_save",
								key_insights: [
									"Direct MCP writes can now create brand-new knowledge entries.",
								],
								source_conversation_id: "conv_runtime_create",
								source_message_ids: ["msg_runtime_create_1"],
								evidence_snippet: "Added the missing create_entry tool to save new chat memories.",
							},
						},
					}),
				}),
			);

			expect(createResponse.status).toBe(200);
			const createEnvelope = await readRpcEnvelope(createResponse);
			const createResult = createEnvelope.result as Record<string, unknown>;
			const createText = ((createResult.content as Array<Record<string, unknown>>)[0].text as string);
			const payload = JSON.parse(createText) as Record<string, unknown>;

			expect(payload.ok).toBe(true);
			expect(payload.created).toBe(true);
			expect(payload.type).toBe("knowledge");
			// 4.1 — entry ids are 16 hex chars (64 bits), allocated atomically via SET NX.
			// (Widened from 12 to avoid birthday collisions across the four ingestion
			// generators; the mock must therefore implement setnx faithfully.)
			expect(String(payload.id)).toMatch(/^ke_[a-f0-9]{16}$/);
			expect(payload.revision).toBe(1);
			expect((((payload.entry as Record<string, unknown>).metadata as Record<string, unknown>).revision)).toBe(1);
			expect((((payload.entry as Record<string, unknown>).metadata as Record<string, unknown>).context_type)).toBe("explicit_save");
			expect(vectorMock.upsert).toHaveBeenCalled();
			expect(redisStore[`knowledge:${String(payload.id)}`]).toBeTruthy();
			const index = JSON.parse(redisStore["index:current"] as string) as Record<string, unknown>;
			expect(index.total_topic_count).toBe(574);
			expect((index.topics as Array<Record<string, unknown>>).map((topic) => topic.id)).toContain(payload.id);
			expect(redisMock.lpush).toHaveBeenCalledWith(
				"mutation_log",
				expect.stringContaining("\"tool\":\"create_entry\""),
			);
		});

		it("updates a knowledge entry through the write-scoped MCP tool", async () => {
		const baseUrl = "https://example.com";
		const { accessToken } = await authorizeClientWithScope(
			baseUrl,
			"mcp:read mcp:write",
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
		const sessionId = initializeResponse.headers.get("mcp-session-id");
		expect(sessionId).toBeTruthy();

		const updateResponse = await dispatch(
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
						name: "update_entry",
						arguments: {
							id: "ke_quant",
							expected_revision: 0,
							mutation_id: "mut_update_ke_quant",
							state: "stale",
							reason: "worker runtime test",
						},
					},
				}),
			}),
		);

		expect(updateResponse.status).toBe(200);
		const updateEnvelope = await readRpcEnvelope(updateResponse);
		const updateResult = updateEnvelope.result as Record<string, unknown>;
		const updateText = ((updateResult.content as Array<Record<string, unknown>>)[0].text as string);
		const payload = JSON.parse(updateText) as Record<string, unknown>;

		expect(payload.ok).toBe(true);
		expect(payload.revision).toBe(1);
		expect((payload.entry as Record<string, unknown>).state).toBe("stale");
		expect(((payload.entry as Record<string, unknown>).metadata as Record<string, unknown>).revision).toBe(1);
		expect((payload.side_effects as Record<string, unknown>).vector).toBe("metadata_updated");
		expect(vectorMock.update).toHaveBeenCalled();
			expect(redisMock.lpush).toHaveBeenCalledWith(
				"mutation_log",
				expect.stringContaining("\"tool\":\"update_entry\""),
			);
		});

		it("adds an insight to a knowledge entry through the write-scoped MCP tool", async () => {
			const baseUrl = "https://example.com";
			const { accessToken } = await authorizeClientWithScope(
				baseUrl,
				"mcp:read mcp:write",
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
			const sessionId = initializeResponse.headers.get("mcp-session-id");
			expect(sessionId).toBeTruthy();

			const addInsightResponse = await dispatch(
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
							name: "add_insight",
							arguments: {
								id: "ke_quant",
								expected_revision: 0,
								mutation_id: "mut_add_insight_ke_quant",
								reason: "worker runtime add insight test",
								insight: "Has deep experience evaluating quantitative investing systems.",
								source_conversation_id: "conv_runtime",
								source_message_ids: ["msg_runtime_1"],
								evidence_snippet: "Spent decades building and evaluating quant strategies.",
							},
						},
					}),
				}),
			);

			expect(addInsightResponse.status).toBe(200);
			const addInsightEnvelope = await readRpcEnvelope(addInsightResponse);
			const addInsightResult = addInsightEnvelope.result as Record<string, unknown>;
			const addInsightText = ((addInsightResult.content as Array<Record<string, unknown>>)[0].text as string);
			const payload = JSON.parse(addInsightText) as Record<string, unknown>;

			expect(payload.ok).toBe(true);
			expect(payload.added).toBe(true);
			expect(payload.revision).toBe(1);
			expect((((payload.entry as Record<string, unknown>).metadata as Record<string, unknown>).revision)).toBe(1);
			expect((((payload.entry as Record<string, unknown>).key_insights as Array<Record<string, unknown>>)[0].insight)).toContain("quantitative investing systems");
			expect((payload.side_effects as Record<string, unknown>).vector).toBe("reembedded");
			expect(vectorMock.upsert).toHaveBeenCalled();
			expect(redisMock.lpush).toHaveBeenCalledWith(
				"mutation_log",
				expect.stringContaining("\"tool\":\"add_insight\""),
			);
		});

		it("archives and restores a knowledge entry through the write-scoped MCP tools", async () => {
		const baseUrl = "https://example.com";
		const { accessToken } = await authorizeClientWithScope(
			baseUrl,
			"mcp:read mcp:write",
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
		const sessionId = initializeResponse.headers.get("mcp-session-id");
		expect(sessionId).toBeTruthy();

		const archiveResponse = await dispatch(
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
						name: "archive_entry",
						arguments: {
							id: "ke_quant",
							expected_revision: 0,
							mutation_id: "mut_archive_ke_quant",
							reason: "worker runtime archive test",
						},
					},
				}),
			}),
		);
		expect(archiveResponse.status).toBe(200);
		const archiveEnvelope = await readRpcEnvelope(archiveResponse);
		const archiveResult = archiveEnvelope.result as Record<string, unknown>;
		const archiveText = ((archiveResult.content as Array<Record<string, unknown>>)[0].text as string);
		const archivePayload = JSON.parse(archiveText) as Record<string, unknown>;
			expect(archivePayload.ok).toBe(true);
			expect(archivePayload.archived).toBe(true);
			expect(archivePayload.revision).toBe(1);
			expect((((archivePayload.entry as Record<string, unknown>).metadata as Record<string, unknown>).archived)).toBe(true);
			expect((archivePayload.side_effects as Record<string, unknown>).vector).toBe("deleted");
			expect(vectorMock.delete).toHaveBeenCalled();
			const archivedIndex = JSON.parse(redisStore["index:current"] as string) as Record<string, unknown>;
			expect(archivedIndex.total_topic_count).toBe(1);
			expect(archivedIndex.total_project_count).toBe(1);
			expect(archivedIndex.archived_count).toBe(1);
			expect((archivedIndex.topics as Array<Record<string, unknown>>).map((topic) => topic.id)).not.toContain("ke_quant");

			const restoreResponse = await dispatch(
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
						name: "restore_entry",
						arguments: {
							id: "ke_quant",
							expected_revision: 1,
							mutation_id: "mut_restore_ke_quant",
							reason: "worker runtime restore test",
						},
					},
				}),
			}),
		);
		expect(restoreResponse.status).toBe(200);
		const restoreEnvelope = await readRpcEnvelope(restoreResponse);
		const restoreResult = restoreEnvelope.result as Record<string, unknown>;
		const restoreText = ((restoreResult.content as Array<Record<string, unknown>>)[0].text as string);
		const restorePayload = JSON.parse(restoreText) as Record<string, unknown>;
			expect(restorePayload.ok).toBe(true);
			expect(restorePayload.archived).toBe(false);
			expect(restorePayload.revision).toBe(2);
			expect((((restorePayload.entry as Record<string, unknown>).metadata as Record<string, unknown>).archived)).toBe(false);
			expect((restorePayload.side_effects as Record<string, unknown>).vector).toBe("recreated");
			expect(vectorMock.upsert).toHaveBeenCalled();
			const restoredIndex = JSON.parse(redisStore["index:current"] as string) as Record<string, unknown>;
			expect(restoredIndex.total_topic_count).toBe(2);
			expect(restoredIndex.total_project_count).toBe(1);
			expect(restoredIndex.archived_count).toBe(0);
			expect((restoredIndex.topics as Array<Record<string, unknown>>).map((topic) => topic.id)).toContain("ke_quant");
			expect(redisMock.lpush).toHaveBeenCalledWith(
				"mutation_log",
				expect.stringContaining("\"tool\":\"restore_entry\""),
			);
		});

		it("consolidates duplicate knowledge entries through the write-scoped MCP tool", async () => {
			const baseUrl = "https://example.com";
			const { accessToken } = await authorizeClientWithScope(
				baseUrl,
				"mcp:read mcp:write",
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
			const sessionId = initializeResponse.headers.get("mcp-session-id");
			expect(sessionId).toBeTruthy();

			const consolidateResponse = await dispatch(
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
							name: "consolidate_entries",
							arguments: {
								keep_id: "ke_quant",
								archive_ids: ["ke_quant_old"],
								expected_revisions: {
									ke_quant: 0,
									ke_quant_old: 0,
								},
								mutation_id: "mut_consolidate_ke_quant",
								reason: "Keep the canonical quant background memory and archive the superseded duplicate.",
								updated_view: "Thirty years building and evaluating quantitative investing systems.",
								confidence: "high",
							},
						},
					}),
				}),
			);

			expect(consolidateResponse.status).toBe(200);
			const consolidateEnvelope = await readRpcEnvelope(consolidateResponse);
			const consolidateResult = consolidateEnvelope.result as Record<string, unknown>;
			const consolidateText = ((consolidateResult.content as Array<Record<string, unknown>>)[0].text as string);
			const payload = JSON.parse(consolidateText) as Record<string, unknown>;

			expect(payload.ok).toBe(true);
			expect(payload.keep_id).toBe("ke_quant");
			expect(payload.archive_ids).toEqual(["ke_quant_old"]);
			expect((((payload.keep_entry as Record<string, unknown>).metadata as Record<string, unknown>).revision)).toBe(1);
			expect((((payload.keep_entry as Record<string, unknown>).related_knowledge as Array<Record<string, unknown>>))).toEqual(
				expect.arrayContaining([
					expect.objectContaining({
						knowledge_id: "ke_quant_old",
						relationship: "supersedes",
					}),
				]),
			);
			expect((((payload.keep_entry as Record<string, unknown>).related_knowledge as Array<Record<string, unknown>>))).not.toEqual(
				expect.arrayContaining([
					expect.objectContaining({
						knowledge_id: "ke_quant_old",
						relationship: "contradicts",
					}),
				]),
			);
			expect((payload.side_effects as Record<string, unknown>).kept_vector).toBe("reembedded");
			expect((payload.side_effects as Record<string, unknown>).archived_vectors).toBe("deleted");
			expect(vectorMock.upsert).toHaveBeenCalled();
			expect(vectorMock.delete).toHaveBeenCalled();
			expect(redisMock.lpush).toHaveBeenCalledWith(
				"mutation_log",
				expect.stringContaining("\"tool\":\"consolidate_entries\""),
			);
		});
	});
