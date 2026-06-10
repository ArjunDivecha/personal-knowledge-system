import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { getMcpAuthContext, McpAgent } from "agents/mcp";
import { z } from "zod";
import { Redis } from "@upstash/redis/cloudflare";
import { Index } from "@upstash/vector";
import OpenAI from "openai";
import { OAuthProvider } from "@cloudflare/workers-oauth-provider";
import {
	computeSalience,
	computeSearchScore,
	deriveSearchTier,
	MEMORY_POLICY,
	getSourceWeightFromMetadata,
	resolveStoredInjectionTier,
} from "./salience";
import {
	addInsight,
	applyDreamProposal,
	archiveExistingEntry,
	consolidateEntries,
	createEntry,
	gradeDreamProposal,
	restoreArchivedEntry,
	restoreEntry,
	rollbackDreamApply,
	runDreamCycle,
	runDreamProposal,
	runScheduledRetierCycle,
	updateEntry,
} from "./dream";
import { formatConsolidationNote } from "./consolidation";
import { getCachedTweet, enqueueTweetRead, setCachedTweet } from "./tweets/cache";
import { checkTweetUpstreams, fetchFxThread, fetchTweetWithFallback } from "./tweets/fetchers";
import { TweetReaderError, type ReadThreadOutput, type ReadTweetOutput } from "./tweets/types";
import { normalizeTweetUrl } from "./tweets/url-parser";
import {
	classifyEntryTopic,
	classifyQueryIntent,
	computeCrossContextPenalty,
	computeQuarantinePenalty,
	type TopicBucket,
	type QueryIntent,
} from "./retrievalPolicy";
import {
	classifyPhase8Query,
	scorePhase8Candidate,
	type Phase8QueryIntent,
} from "./phase8Retrieval";
import {
	checkDestructiveTripwire,
	checkRetrievalTripwire,
	clearKillFlag,
	getEffectiveMode,
	readKillFlag,
	recordSearchQuery,
	setKillFlag,
} from "./tripwires";

// GitHub accounts to query
const GITHUB_ACCOUNTS = ['arjun-via', 'ArjunDivecha'];
const MEMORY_SCHEMA_VERSION = 2;
// 3.3 — Only the rank-1 search result counts as "use" for access_count purposes.
// Top-5 triggers were granting every search result permanent archive immunity.
const MAX_RECONSOLIDATION_SEARCH_RESULTS = 1;
const MAX_RECONSOLIDATION_ERROR_LOGS = 100;
const RECONSOLIDATION_PROMOTION_THRESHOLD = 3;
const MAX_OPERATOR_DREAM_ARCHIVE_LIMIT = 10;
// Phase 4 (R4.1): nightly archive cap is policy-driven so it can ramp
// (10 -> 50 -> ...) under the destructive-spike tripwire. Falls back to 10.
const SCHEDULED_DREAM_ARCHIVE_LIMIT =
	typeof (MEMORY_POLICY.dream_thresholds as Record<string, unknown>).scheduled_archive_limit === "number"
		? ((MEMORY_POLICY.dream_thresholds as Record<string, unknown>).scheduled_archive_limit as number)
		: 10;
// 2.5 — Global entries-touched cap: enforced in applyDreamProposal independent of op count,
// so duplicate_merge archive_ids cannot silently exceed the per-op archive limit.  Defaults
// to 4× the archive limit if not policy-driven (conservatively caps a 10-merge night at 40
// entries × 4 archive_ids each = 160 < 200 typical threshold).
const SCHEDULED_DREAM_MAX_ENTRIES_TOUCHED =
	typeof (MEMORY_POLICY.dream_thresholds as Record<string, unknown>).max_entries_touched_per_apply === "number"
		? ((MEMORY_POLICY.dream_thresholds as Record<string, unknown>).max_entries_touched_per_apply as number)
		: SCHEDULED_DREAM_ARCHIVE_LIMIT * 4;
const SCHEDULED_DREAM_PROMOTION_LIMIT = 10;
const SCHEDULED_DREAM_DUPLICATE_MERGE_LIMIT = 10;
const SCHEDULED_DREAM_MARK_CONTESTED_LIMIT = 10;
const RATE_LIMIT_WINDOW_SECONDS = 60 * 60;
const WRITE_TOOL_RATE_LIMIT = 24;
const OPERATOR_WRITE_RATE_LIMIT = 12;
const OPENAI_ROUTE_PREFIX = "/openai/";
const CONTEXT_TYPES = [
	"professional_identity",
	"stated_preference",
	"explicit_save",
	"active_project",
	"recurring_pattern",
	"task_query",
	"passing_reference",
] as const;
const READ_ONLY_TOOL_ANNOTATIONS = {
	readOnlyHint: true,
};
const MUTATING_TOOL_ANNOTATIONS = {
	destructiveHint: true,
};
const OPEN_WORLD_READ_ONLY_TOOL_ANNOTATIONS = {
	readOnlyHint: true,
	openWorldHint: true,
};
const CORS_ALLOW_METHODS = "GET, POST, OPTIONS, HEAD";
const CORS_ALLOW_HEADERS = "Authorization, Content-Type, Accept, MCP-Session-Id, Last-Event-ID";
const CORS_EXPOSE_HEADERS = "WWW-Authenticate, MCP-Session-Id, Location";
const AUTHLESS_PROBE_PATHS = new Set(["/mcp", "/sse", "/openai/mcp", "/openai/sse"]);
const VALIDATION_LAST_KEY = "validation:last";
const VALIDATION_GATE_STATUS_KEY = "validation:gate_status";
const DREAM_LOCK_KEY = "dream:lock";
const DREAM_LOCK_TTL_SECONDS = 30 * 60;
// 2.7 — Stale window raised to TTL − 60 s so a legitimately-running Dream job
// (which can take 15–20 min under semantic dedup load) is never racily preempted.
const DREAM_LOCK_STALE_AFTER_SECONDS = DREAM_LOCK_TTL_SECONDS - 60;
// 2.7 — Lua script for atomic CAS lock reclaim.  Checks that the current lock
// value is exactly what we observed as stale before replacing it; prevents the
// DEL→SET-NX double-acquire race where two workers both see a stale lock, both
// DEL it, and both SET-NX successfully.
const DREAM_LOCK_CAS_SCRIPT = `
local current = redis.call('GET', KEYS[1])
if current == ARGV[1] then
  return redis.call('SET', KEYS[1], ARGV[2], 'EX', tonumber(ARGV[3]))
end
return nil
`;
const DREAM_LAST_RUN_KEY = "dream:last_run";
const DREAM_LAST_ATTEMPT_KEY = "dream:last_attempt";
const DREAM_RUN_PREFIX = "dream:run:";
const DREAM_RUN_INDEX_KEY = "dream:runs:index";
const DREAM_SCHEDULED_BOUNDARY_PREFIX = "dream:scheduled-governed:boundary:";
const DREAM_SCHEDULED_BOUNDARY_TTL_SECONDS = 72 * 60 * 60;
const SCHEDULED_DREAM_ALLOWED_RISKS = new Set(["low", "medium"]);
const SCHEDULED_DREAM_OPERATION_LIMITS: Record<string, number> = {
	archive_entry: SCHEDULED_DREAM_ARCHIVE_LIMIT,
	duplicate_merge: SCHEDULED_DREAM_DUPLICATE_MERGE_LIMIT,
	mark_contested: SCHEDULED_DREAM_MARK_CONTESTED_LIMIT,
	promote_context_type: SCHEDULED_DREAM_PROMOTION_LIMIT,
};
const DEFAULT_TWEET_TIMEOUT_MS = 4000;
const DEFAULT_TWEET_CACHE_TTL_SECONDS = 300;

function isEnabledEnvFlag(value: unknown): boolean {
	return typeof value === "string" && ["1", "true", "on", "yes"].includes(value.toLowerCase());
}

const AUTHORIZATION_SERVER_METADATA_PATHS = new Set([
	"/.well-known/oauth-authorization-server",
	"/.well-known/openid-configuration",
	"/.well-known/oauth-authorization-server/mcp",
	"/.well-known/oauth-authorization-server/sse",
	"/.well-known/oauth-authorization-server/openai/mcp",
	"/.well-known/oauth-authorization-server/openai/sse",
	"/mcp/.well-known/oauth-authorization-server",
	"/sse/.well-known/oauth-authorization-server",
	"/openai/mcp/.well-known/oauth-authorization-server",
	"/openai/sse/.well-known/oauth-authorization-server",
]);

type ProtectedResourceConfig = {
	resourcePath: string;
	scopes: string[];
};

type EntryType = "knowledge" | "project";
type AuthProps = {
	userId?: string;
	scope?: string;
	scopes?: string[];
};

type McpToolResult = {
	content: Array<{ type: "text"; text: string }>;
	structuredContent?: Record<string, unknown>;
	isError?: boolean;
};

function getBaseUrl(url: URL): string {
	return `${url.protocol}//${url.host}`;
}

function isOpenAIResourcePath(pathname: string): boolean {
	return pathname.startsWith(OPENAI_ROUTE_PREFIX);
}

function normalizeOAuthResource(resource: string | null, baseUrl: string): string | null {
	if (!resource) return resource;
	const trimmed = resource.trim();
	if (!trimmed) return resource;
	if (trimmed === baseUrl) return baseUrl;
	if (trimmed.startsWith(`${baseUrl}/`)) {
		return baseUrl;
	}
	return resource;
}

function rewriteAuthorizeRequestResource(request: Request): Request {
	const url = new URL(request.url);
	if (url.pathname !== "/authorize") {
		return request;
	}

	const normalizedResource = normalizeOAuthResource(url.searchParams.get("resource"), getBaseUrl(url));
	if (!normalizedResource || normalizedResource === url.searchParams.get("resource")) {
		return request;
	}

	url.searchParams.set("resource", normalizedResource);
	return new Request(url.toString(), request);
}

async function rewriteTokenRequestResource(request: Request): Promise<Request> {
	const url = new URL(request.url);
	if (url.pathname !== "/token") {
		return request;
	}

	const contentType = request.headers.get("content-type") || "";
	if (!contentType.includes("application/x-www-form-urlencoded")) {
		return request;
	}

	const bodyText = await request.clone().text();
	const params = new URLSearchParams(bodyText);
	const resourceValues = params.getAll("resource");
	if (resourceValues.length === 0) {
		return request;
	}

	const baseUrl = getBaseUrl(url);
	const normalizedValues = resourceValues.map((value) => normalizeOAuthResource(value, baseUrl) ?? value);
	const changed = normalizedValues.some((value, index) => value !== resourceValues[index]);
	if (!changed) {
		return request;
	}

	params.delete("resource");
	for (const value of normalizedValues) {
		params.append("resource", value);
	}

	return new Request(request.url, {
		method: request.method,
		headers: request.headers,
		body: params.toString(),
	});
}

function createRedisClient(env: Env): Redis {
	return new Redis({
		url: env.UPSTASH_REDIS_REST_URL,
		token: env.UPSTASH_REDIS_REST_TOKEN,
		enableAutoPipelining: false,
	});
}

function createVectorClient(env: Env): Index {
	return new Index({
		url: env.UPSTASH_VECTOR_REST_URL,
		token: env.UPSTASH_VECTOR_REST_TOKEN,
	});
}

function parseStoredObject(raw: unknown): Record<string, unknown> | null {
	if (typeof raw === "string") {
		try {
			const parsed = JSON.parse(raw);
			if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
				return parsed as Record<string, unknown>;
			}
		} catch {
			return null;
		}
	}

	if (raw && typeof raw === "object" && !Array.isArray(raw)) {
		return { ...(raw as Record<string, unknown>) };
	}

	return null;
}

function toStringArray(value: unknown): string[] {
	if (!Array.isArray(value)) return [];
	return value.filter((item): item is string => typeof item === "string");
}

function toOptionalNumber(value: unknown): number | null {
	if (typeof value === "number" && Number.isFinite(value)) {
		return value;
	}
	if (typeof value === "string" && value.trim() !== "") {
		const parsed = Number(value);
		return Number.isFinite(parsed) ? parsed : null;
	}
	return null;
}

function toOptionalInteger(value: unknown): number | null {
	const parsed = toOptionalNumber(value);
	return parsed === null ? null : Math.trunc(parsed);
}

function readPositiveIntegerEnv(
	value: string | undefined,
	defaultValue: number,
	options: { min: number; max: number },
): number {
	if (!value) return defaultValue;
	const parsed = Number(value);
	if (!Number.isInteger(parsed)) return defaultValue;
	return Math.min(Math.max(parsed, options.min), options.max);
}

function getTweetTimeoutMs(env: Env): number {
	return readPositiveIntegerEnv(env.TWEET_READER_TIMEOUT_MS, DEFAULT_TWEET_TIMEOUT_MS, {
		min: 500,
		max: 10_000,
	});
}

function getTweetCacheTtlSeconds(env: Env): number {
	return readPositiveIntegerEnv(
		env.TWEET_READER_CACHE_TTL_SECONDS,
		DEFAULT_TWEET_CACHE_TTL_SECONDS,
		{ min: 30, max: 3600 },
	);
}

function toolJson(payload: object): McpToolResult {
	return {
		structuredContent: payload as Record<string, unknown>,
		content: [{ type: "text", text: JSON.stringify(payload) }],
	};
}

function toolError(message: string): McpToolResult {
	return {
		isError: true,
		content: [{ type: "text", text: message }],
	};
}

function tweetReaderErrorMessage(error: unknown): string {
	if (error instanceof TweetReaderError) return error.message;
	return error instanceof Error ? error.message : String(error);
}

function toSourceWeights(value: unknown): Record<string, number> {
	if (!value || typeof value !== "object" || Array.isArray(value)) {
		return {};
	}

	const normalized: Record<string, number> = {};
	for (const [key, rawValue] of Object.entries(value as Record<string, unknown>)) {
		const parsed = toOptionalNumber(rawValue);
		if (parsed !== null) {
			normalized[key] = parsed;
		}
	}
	return normalized;
}

function normalizeEntryMetadata(rawMetadata: unknown, entryType?: string): Record<string, unknown> {
	const metadata = parseStoredObject(rawMetadata) ?? {};
	const sourceConversations = toStringArray(metadata.source_conversations);
	const sourceMessages = toStringArray(metadata.source_messages);
	const updatedAt =
		typeof metadata.updated_at === "string"
			? metadata.updated_at
			: typeof metadata.last_touched === "string"
				? metadata.last_touched
				: typeof metadata.created_at === "string"
					? metadata.created_at
					: "";
	const createdAt = typeof metadata.created_at === "string" ? metadata.created_at : updatedAt;

	const normalized: Record<string, unknown> = {
		...metadata,
		created_at: createdAt,
		updated_at: updatedAt,
		source_conversations: sourceConversations,
		source_messages: sourceMessages,
		access_count: toOptionalInteger(metadata.access_count) ?? 0,
		last_accessed: typeof metadata.last_accessed === "string" ? metadata.last_accessed : null,
		schema_version: toOptionalInteger(metadata.schema_version) ?? MEMORY_SCHEMA_VERSION,
		classification_status:
			typeof metadata.classification_status === "string" && metadata.classification_status.length > 0
				? metadata.classification_status
				: "pending",
		context_type: typeof metadata.context_type === "string" ? metadata.context_type : null,
		mention_count: toOptionalInteger(metadata.mention_count) ?? Math.max(1, sourceConversations.length || 1),
		first_seen: typeof metadata.first_seen === "string" ? metadata.first_seen : null,
		last_seen: typeof metadata.last_seen === "string" ? metadata.last_seen : null,
		auto_inferred: typeof metadata.auto_inferred === "boolean" ? metadata.auto_inferred : null,
		source_weights: toSourceWeights(metadata.source_weights),
		injection_tier: toOptionalInteger(metadata.injection_tier),
		salience_score: toOptionalNumber(metadata.salience_score),
		revision: toOptionalInteger(metadata.revision) ?? 0,
		last_consolidated: typeof metadata.last_consolidated === "string" ? metadata.last_consolidated : null,
		consolidation_notes: toStringArray(metadata.consolidation_notes),
		archived: Boolean(metadata.archived),
	};

	if (entryType === "project") {
		normalized.last_touched =
			typeof metadata.last_touched === "string" ? metadata.last_touched : updatedAt;
	}

	return normalized;
}

function normalizeEntry(raw: unknown, entryTypeHint?: string): Record<string, unknown> | null {
	const entry = parseStoredObject(raw);
	if (!entry) return null;

	const entryType = typeof entry.type === "string" ? entry.type : entryTypeHint;
	const normalized = {
		...entry,
		type: entryType ?? entry.type,
		metadata: normalizeEntryMetadata(entry.metadata, entryType),
	};
	const metadata = normalized.metadata as Record<string, unknown>;
	metadata.injection_tier = resolveStoredInjectionTier(metadata);
	metadata.salience_score = computeSalience(normalized);
	return normalized;
}

function getEntryId(entry: Record<string, unknown> | null): string | null {
	return typeof entry?.id === "string" && entry.id.length > 0 ? entry.id : null;
}

function getEntryKey(entryType: EntryType, entryId: string): string {
	return `${entryType}:${entryId}`;
}

function getEntryAccessKey(entryId: string): string {
	return `entry_access:${entryId}`;
}

function getEntryLastAccessedKey(entryId: string): string {
	return `entry_last_accessed:${entryId}`;
}

function getReconsolidationErrorKey(now: Date = new Date()): string {
	return `reconsolidation:errors:${now.toISOString().slice(0, 10)}`;
}

function getRateLimitBucket(now: number, windowSeconds: number): number {
	return Math.floor(now / 1000 / windowSeconds);
}

function getRateLimitKey(actor: string, action: string, bucket: number): string {
	return `rate_limit:${actor}:${action}:${bucket}`;
}

function normalizeScopes(raw: unknown): string[] {
	if (Array.isArray(raw)) {
		return raw.filter((item): item is string => typeof item === "string" && item.length > 0);
	}
	if (typeof raw === "string") {
		return raw
			.split(/\s+/)
			.map((scope) => scope.trim())
			.filter((scope) => scope.length > 0);
	}
	return [];
}

function getApprovedAuthorizationScopes(
	requestedScopes: string[],
	allowWriteScope: boolean,
): string[] {
	const normalizedScopes = requestedScopes.length > 0 ? requestedScopes : ["mcp:read"];
	return [...new Set(normalizedScopes)].filter((scope) => {
		if (scope === "mcp:read") return true;
		if (scope === "mcp:write") return allowWriteScope;
		return false;
	});
}

function applyCorsHeaders(request: Request, headers: Headers): void {
	const origin = request.headers.get("origin");
	headers.set("Access-Control-Allow-Origin", origin && origin.length > 0 ? origin : "*");
	headers.set("Access-Control-Allow-Methods", CORS_ALLOW_METHODS);
	headers.set("Access-Control-Allow-Headers", CORS_ALLOW_HEADERS);
	headers.set("Access-Control-Expose-Headers", CORS_EXPOSE_HEADERS);
	headers.set("Vary", "Origin");
}

function withCors(request: Request, response: Response): Response {
	const headers = new Headers(response.headers);
	applyCorsHeaders(request, headers);
	return new Response(response.body, {
		status: response.status,
		statusText: response.statusText,
		headers,
	});
}

function getProtectedResourceConfig(pathname: string): ProtectedResourceConfig | null {
	switch (pathname) {
		case "/mcp/.well-known/oauth-protected-resource":
			return { resourcePath: "/mcp", scopes: ["mcp:read", "mcp:write"] };
		case "/sse/.well-known/oauth-protected-resource":
			return { resourcePath: "/sse", scopes: ["mcp:read", "mcp:write"] };
		case "/openai/mcp/.well-known/oauth-protected-resource":
			return { resourcePath: "/openai/mcp", scopes: ["mcp:read"] };
		case "/openai/sse/.well-known/oauth-protected-resource":
			return { resourcePath: "/openai/sse", scopes: ["mcp:read"] };
		default:
			return null;
	}
}

function buildProtectedResourceMetadata(baseUrl: string, config: ProtectedResourceConfig): Record<string, unknown> {
	return {
		resource: `${baseUrl}${config.resourcePath}`,
		authorization_servers: [baseUrl],
		scopes_supported: config.scopes,
		bearer_methods_supported: ["header"],
	};
}

function buildAuthorizationServerMetadata(baseUrl: string): Record<string, unknown> {
	return {
		issuer: baseUrl,
		authorization_endpoint: `${baseUrl}/authorize`,
		token_endpoint: `${baseUrl}/token`,
		registration_endpoint: `${baseUrl}/register`,
		scopes_supported: ["mcp:read", "mcp:write"],
		response_types_supported: ["code"],
		response_modes_supported: ["query"],
		grant_types_supported: ["authorization_code", "refresh_token"],
		token_endpoint_auth_methods_supported: ["client_secret_post", "client_secret_basic", "none"],
		revocation_endpoint: `${baseUrl}/token`,
		code_challenge_methods_supported: ["plain", "S256"],
		client_id_metadata_document_supported: false,
	};
}

function buildUnauthorizedMcpChallenge(baseUrl: string, pathname: string): string {
	const protectedResourceConfig = getProtectedResourceConfig(`${pathname}/.well-known/oauth-protected-resource`);
	const resourceMetadataPath =
		protectedResourceConfig !== null
			? `${protectedResourceConfig.resourcePath}/.well-known/oauth-protected-resource`
			: `${pathname}/.well-known/oauth-protected-resource`;
	return [
		'Bearer realm="OAuth"',
		`resource_metadata="${baseUrl}${resourceMetadataPath}"`,
		'error="invalid_token"',
		'error_description="Missing or invalid access token"',
	].join(", ");
}

function withUnauthorizedMcpChallenge(
	request: Request,
	response: Response,
	baseUrl: string,
	pathname: string,
): Response {
	const headers = new Headers(response.headers);
	headers.set("WWW-Authenticate", buildUnauthorizedMcpChallenge(baseUrl, pathname));
	applyCorsHeaders(request, headers);
	return new Response(response.body, {
		status: response.status,
		statusText: response.statusText,
		headers,
	});
}

function createCorsPreflightResponse(request: Request): Response {
	const headers = new Headers();
	applyCorsHeaders(request, headers);
	return new Response(null, {
		status: 204,
		headers,
	});
}

function createHeadProbeResponse(request: Request, baseUrl: string, pathname: string): Response {
	return withUnauthorizedMcpChallenge(
		request,
		new Response(null, {
			status: 401,
		}),
		baseUrl,
		pathname,
	);
}

async function normalizeClientRegistrationResponse(response: Response): Promise<Response> {
	if (response.status !== 201) {
		return response;
	}

	const contentType = response.headers.get("content-type") ?? "";
	if (!contentType.includes("application/json")) {
		return response;
	}

	let payload: Record<string, unknown>;
	try {
		payload = (await response.clone().json()) as Record<string, unknown>;
	} catch {
		return response;
	}

	delete payload.registration_client_uri;

	if (typeof payload.client_secret === "string" && payload.client_secret_expires_at === undefined) {
		payload.client_secret_expires_at = 0;
	}

	const headers = new Headers(response.headers);
	headers.delete("content-length");
	return new Response(JSON.stringify(payload), {
		status: response.status,
		statusText: response.statusText,
		headers,
	});
}

async function applyFixedWindowRateLimit(
	redis: Redis,
	actor: string,
	action: string,
	limit: number,
	windowSeconds: number = RATE_LIMIT_WINDOW_SECONDS,
	now: number = Date.now(),
): Promise<{ allowed: boolean; count: number; limit: number; bucket: number }> {
	const bucket = getRateLimitBucket(now, windowSeconds);
	const key = getRateLimitKey(actor, action, bucket);
	const count = Number(await redis.incr(key));
	return {
		allowed: count <= limit,
		count,
		limit,
		bucket,
	};
}

function getOperatorBearerToken(request: Request): string | null {
	const authHeader = request.headers.get("authorization");
	if (!authHeader) return null;
	const match = authHeader.match(/^Bearer\s+(.+)$/i);
	return match ? match[1] : null;
}

function isAuthorizedOperatorRequest(request: Request, env: Env): boolean {
	if (!env.DREAM_OPERATOR_TOKEN) return false;
	const bearerToken = getOperatorBearerToken(request);
	return bearerToken !== null && bearerToken === env.DREAM_OPERATOR_TOKEN;
}

function latestIsoTimestamp(...values: Array<string | null | undefined>): string | null {
	let latestValue: string | null = null;
	let latestTime = Number.NEGATIVE_INFINITY;

	for (const value of values) {
		if (!value) continue;
		const timestamp = new Date(value).getTime();
		if (Number.isNaN(timestamp)) continue;
		if (timestamp > latestTime) {
			latestTime = timestamp;
			latestValue = value;
		}
	}

	return latestValue;
}

function appendConsolidationNote(metadata: Record<string, unknown>, note: string): void {
	const existingNotes = toStringArray(metadata.consolidation_notes);
	if (existingNotes[existingNotes.length - 1] === note) {
		metadata.consolidation_notes = existingNotes;
		return;
	}

	existingNotes.push(note);
	metadata.consolidation_notes = existingNotes.slice(-20);
}

function applyAccessSignals(
	entry: Record<string, unknown>,
	accessCountRaw: unknown,
	lastAccessedRaw: unknown,
): Record<string, unknown> {
	const metadata = getEntryMetadata(entry);
	const storedAccessCount = toOptionalInteger(metadata.access_count) ?? 0;
	const sideAccessCount = toOptionalInteger(accessCountRaw);
	const effectiveAccessCount =
		sideAccessCount === null ? storedAccessCount : Math.max(storedAccessCount, sideAccessCount);
	const storedLastAccessed =
		typeof metadata.last_accessed === "string" ? metadata.last_accessed : null;
	const sideLastAccessed =
		typeof lastAccessedRaw === "string" && lastAccessedRaw.length > 0 ? lastAccessedRaw : null;

	metadata.access_count = effectiveAccessCount;
	metadata.last_accessed = latestIsoTimestamp(storedLastAccessed, sideLastAccessed);
	metadata.salience_score = computeSalience(entry);
	return entry;
}

// GitHub API helper
async function githubRequest(
	endpoint: string,
	token: string,
	params: Record<string, string> = {}
): Promise<any> {
	const url = new URL(`https://api.github.com${endpoint}`);
	Object.entries(params).forEach(([key, value]) => {
		url.searchParams.append(key, value);
	});

	const response = await fetch(url.toString(), {
		headers: {
			'Authorization': `token ${token}`,
			'Accept': 'application/vnd.github.v3+json',
			'User-Agent': 'personal-knowledge-mcp',
		},
	});

	if (!response.ok) {
		if (response.status === 404) return null;
		throw new Error(`GitHub API error: ${response.status}`);
	}

	return response.json();
}

// Calculate recency score based on how recently the entry was updated
function calculateRecencyScore(updatedAt: string | undefined): number {
	if (!updatedAt) return 0.5;

	try {
		const entryDate = new Date(updatedAt);
		const now = new Date();
		const daysSinceUpdate = (now.getTime() - entryDate.getTime()) / (1000 * 60 * 60 * 24);

		if (daysSinceUpdate <= 7) return 1.0;
		if (daysSinceUpdate <= 30) return 0.9;
		if (daysSinceUpdate <= 90) return 0.75;
		if (daysSinceUpdate <= 180) return 0.6;
		if (daysSinceUpdate <= 365) return 0.45;
		if (daysSinceUpdate <= 730) return 0.3;
		return 0.2;
	} catch {
		return 0.5;
	}
}

function getEntryMetadata(entry: Record<string, unknown> | null): Record<string, unknown> {
	return (entry?.metadata as Record<string, unknown> | undefined) ?? {};
}

function getEntryUpdatedAt(entry: Record<string, unknown>): string | undefined {
	const metadata = getEntryMetadata(entry);
	return (
		(typeof metadata.last_seen === "string" && metadata.last_seen) ||
		(typeof metadata.updated_at === "string" && metadata.updated_at) ||
		(typeof metadata.last_touched === "string" && metadata.last_touched) ||
		undefined
	);
}

function getEntryState(entry: Record<string, unknown>): string | null {
	if (typeof entry.state === "string") return entry.state;
	if (typeof entry.status === "string") return entry.status;
	return null;
}

function getEntryLabel(entry: Record<string, unknown>): string {
	if (typeof entry.domain === "string") return entry.domain;
	if (typeof entry.name === "string") return entry.name;
	return String(entry.id ?? "unknown");
}

function getEntrySummary(entry: Record<string, unknown>): string {
	if (typeof entry.current_view === "string" && entry.current_view.length > 0) {
		return entry.current_view.slice(0, 160);
	}
	if (typeof entry.goal === "string" && entry.goal.length > 0) {
		return entry.goal.slice(0, 160);
	}
	return "";
}

function buildReconsolidatedVectorMetadata(entry: Record<string, unknown>): Record<string, unknown> {
	const metadata = getEntryMetadata(entry);
	return {
		archived: Boolean(metadata.archived),
		classification_status:
			typeof metadata.classification_status === "string" && metadata.classification_status.length > 0
				? metadata.classification_status
				: "pending",
		context_type: typeof metadata.context_type === "string" ? metadata.context_type : null,
		injection_tier: resolveStoredInjectionTier(metadata),
		salience_score: toOptionalNumber(metadata.salience_score),
		mention_count: toOptionalInteger(metadata.mention_count),
		last_consolidated:
			typeof metadata.last_consolidated === "string" ? metadata.last_consolidated : null,
	};
}

async function buildHealthPayload(env: Env): Promise<Record<string, unknown>> {
	const redis = createRedisClient(env);
	const rawIndex = parseStoredObject(await redis.get("index:current")) ?? {};
	const dreamSummary = parseStoredObject(await redis.get("dream:last_run"));
	const dreamProposal = parseStoredObject(await redis.get("dream:proposal:last"));
	const backfillComplete = await redis.get("migration:backfill_complete");
	const pendingClassificationCount = await redis.scard("classification:pending") as number;
	const reconsolidationErrorCount = await redis.llen(getReconsolidationErrorKey()) as number;
	const topics = Array.isArray(rawIndex.topics) ? rawIndex.topics : [];
	const projects = Array.isArray(rawIndex.projects) ? rawIndex.projects : [];

	return {
		status: "ok",
		retrieved_at: new Date().toISOString(),
		schema_version: MEMORY_SCHEMA_VERSION,
		migration_backfill_complete: backfillComplete,
		pending_classification_count: pendingClassificationCount || 0,
		reconsolidation_error_count_today: reconsolidationErrorCount || 0,
		last_dream_run: typeof dreamSummary?.run_at === "string" ? dreamSummary.run_at : null,
		last_dream_status: typeof dreamSummary?.status === "string" ? dreamSummary.status : null,
		last_dream_dry_run: typeof dreamSummary?.dry_run === "boolean" ? dreamSummary.dry_run : null,
		last_dream_archive_candidate_count:
			typeof dreamSummary?.counts === "object" &&
			dreamSummary.counts &&
			typeof (dreamSummary.counts as Record<string, unknown>).archive_candidates === "number"
				? (dreamSummary.counts as Record<string, number>).archive_candidates
				: null,
		last_dream_proposal_run:
			typeof dreamProposal?.run_id === "string" ? dreamProposal.run_id : null,
		last_dream_proposal_at:
			typeof dreamProposal?.run_at === "string"
				? dreamProposal.run_at
				: typeof dreamProposal?.created_at === "string"
					? dreamProposal.created_at
					: null,
		last_dream_proposal_status:
			typeof dreamProposal?.status === "string" ? dreamProposal.status : null,
		last_dream_proposal_actor:
			typeof dreamProposal?.actor_id === "string" ? dreamProposal.actor_id : null,
		last_dream_proposal_operation_count:
			Array.isArray(dreamProposal?.operations) ? dreamProposal.operations.length : null,
		last_dream_proposal_risk:
			typeof dreamProposal?.risk_score === "string" ? dreamProposal.risk_score : null,
		last_dream_proposal_archive_candidate_count:
			typeof dreamProposal?.counts === "object" &&
			dreamProposal.counts &&
			typeof (dreamProposal.counts as Record<string, unknown>).archive_candidates === "number"
				? (dreamProposal.counts as Record<string, number>).archive_candidates
				: null,
		thin_index: {
			generated_at: typeof rawIndex.generated_at === "string" ? rawIndex.generated_at : null,
			stored_topic_count: topics.length,
			stored_project_count: projects.length,
			total_topic_count:
				typeof rawIndex.total_topic_count === "number" ? rawIndex.total_topic_count : topics.length,
			total_project_count:
				typeof rawIndex.total_project_count === "number" ? rawIndex.total_project_count : projects.length,
			tier_1_count: typeof rawIndex.tier_1_count === "number" ? rawIndex.tier_1_count : null,
			tier_2_count: typeof rawIndex.tier_2_count === "number" ? rawIndex.tier_2_count : null,
			tier_3_count: typeof rawIndex.tier_3_count === "number" ? rawIndex.tier_3_count : null,
			archived_count: typeof rawIndex.archived_count === "number" ? rawIndex.archived_count : 0,
		},
	};
}

function parseStoredArray(raw: unknown): unknown[] {
	if (Array.isArray(raw)) {
		return [...raw];
	}
	if (typeof raw === "string") {
		try {
			const parsed = JSON.parse(raw);
			return Array.isArray(parsed) ? parsed : [];
		} catch {
			return [];
		}
	}
	return [];
}

function toObjectArray(value: unknown): Array<Record<string, unknown>> {
	return parseStoredArray(value).filter(
		(item): item is Record<string, unknown> => item !== null && typeof item === "object" && !Array.isArray(item),
	);
}

async function getValidationStatus(redis: Redis): Promise<Record<string, unknown>> {
	const gateStatus = parseStoredObject(await redis.get(VALIDATION_GATE_STATUS_KEY));
	const last = parseStoredObject(await redis.get(VALIDATION_LAST_KEY));
	return {
		schema_version: 1,
		retrieved_at: new Date().toISOString(),
		gate_status: gateStatus ?? {
			overall_status: "unknown",
			overall_passed: null,
			gates: {},
		},
		last_validation: last,
	};
}

function compactDreamRun(run: Record<string, unknown>): Record<string, unknown> {
	const counts = parseStoredObject(run.counts);
	return {
		run_id: typeof run.run_id === "string" ? run.run_id : null,
		run_at: typeof run.run_at === "string" ? run.run_at : null,
		completed_at: typeof run.completed_at === "string" ? run.completed_at : null,
		status: typeof run.status === "string" ? run.status : null,
		trigger: typeof run.trigger === "string" ? run.trigger : null,
		dry_run: typeof run.dry_run === "boolean" ? run.dry_run : null,
		counts,
	};
}

async function listDreamRuns(redis: Redis, limit: number, statusFilter?: string): Promise<Record<string, unknown>> {
	const index = parseStoredArray(await redis.get(DREAM_RUN_INDEX_KEY))
		.filter((value): value is string => typeof value === "string");
	const candidateIds = index.length > 0
		? index.slice(0, limit)
		: [];
	const runs: Record<string, unknown>[] = [];

	for (const runId of candidateIds) {
		const run = parseStoredObject(await redis.get(`${DREAM_RUN_PREFIX}${runId}`));
		if (!run) continue;
		if (statusFilter && run.status !== statusFilter) continue;
		runs.push(compactDreamRun(run));
		if (runs.length >= limit) break;
	}

	if (runs.length === 0) {
		const lastRun = parseStoredObject(await redis.get(DREAM_LAST_RUN_KEY));
		if (lastRun && (!statusFilter || lastRun.status === statusFilter)) {
			runs.push(compactDreamRun(lastRun));
		}
	}

	return {
		schema_version: 1,
		runs,
		limit,
		status_filter: statusFilter ?? null,
		source: index.length > 0 ? DREAM_RUN_INDEX_KEY : DREAM_LAST_RUN_KEY,
	};
}

async function getDreamRun(redis: Redis, runId: string): Promise<Record<string, unknown>> {
	const directRun = parseStoredObject(await redis.get(`${DREAM_RUN_PREFIX}${runId}`));
	if (directRun) {
		return directRun;
	}

	const lastRun = parseStoredObject(await redis.get(DREAM_LAST_RUN_KEY));
	if (lastRun?.run_id === runId) {
		return lastRun;
	}

	const lastAttempt = parseStoredObject(await redis.get(DREAM_LAST_ATTEMPT_KEY));
	if (lastAttempt?.run_id === runId) {
		return lastAttempt;
	}

	return { error: "dream_run_not_found", run_id: runId };
}

function getScheduledGovernedBoundaryKey(controller: ScheduledController): string {
	const scheduledTime = typeof controller.scheduledTime === "number" ? controller.scheduledTime : Date.now();
	const boundaryDate = new Date(scheduledTime).toISOString().slice(0, 10);
	return `${DREAM_SCHEDULED_BOUNDARY_PREFIX}${boundaryDate}`;
}

function isScheduledGovernedRecord(run: Record<string, unknown> | null): run is Record<string, unknown> {
	// 2.4 — Only completed runs count as a satisfied boundary; failed/held/skipped runs
	// must not block same-day repair attempts.
	return Boolean(
		run &&
			run.trigger === "scheduled" &&
			run.dry_run === false &&
			run.auto_apply_mode === "governed" &&
			typeof run.run_id === "string" &&
			typeof run.status === "string" &&
			(run.status === "completed" || run.status === "completed_with_holds"),
	);
}

async function getScheduledGovernedBoundaryRun(
	redis: Redis,
	controller: ScheduledController,
): Promise<Record<string, unknown> | null> {
	const runId = await redis.get(getScheduledGovernedBoundaryKey(controller));
	if (typeof runId !== "string" || !runId) return null;
	const run = await getDreamRun(redis, runId);
	if (!isScheduledGovernedRecord(run)) return null;
	return {
		...run,
		boundary_deduped: true,
		next_action: "Scheduled-governed Dream already ran for this UTC boundary; no duplicate apply attempted.",
	};
}

async function storeScheduledGovernedBoundary(
	redis: Redis,
	controller: ScheduledController,
	runId: string,
): Promise<void> {
	await redis.set(getScheduledGovernedBoundaryKey(controller), runId, {
		ex: DREAM_SCHEDULED_BOUNDARY_TTL_SECONDS,
	});
}

async function storeScheduledGovernedBoundaryBestEffort(
	redis: Redis,
	controller: ScheduledController,
	runId: string,
): Promise<void> {
	try {
		await storeScheduledGovernedBoundary(redis, controller, runId);
	} catch (error) {
		console.error("[scheduled-governed] could not store boundary dedupe key", error);
	}
}

async function getDreamEvents(redis: Redis, runId: string): Promise<Record<string, unknown>> {
	const events = parseStoredArray(await redis.get(`${DREAM_RUN_PREFIX}${runId}:events`));
	return {
		schema_version: 1,
		run_id: runId,
		events,
		event_count: events.length,
	};
}

async function updateDreamRunIndex(redis: Redis, runId: string, maxRuns = 50): Promise<void> {
	const existing = parseStoredArray(await redis.get(DREAM_RUN_INDEX_KEY))
		.filter((value): value is string => typeof value === "string");
	const nextIndex = [
		runId,
		...existing.filter((value) => value !== runId),
	].slice(0, maxRuns);
	await redis.set(DREAM_RUN_INDEX_KEY, JSON.stringify(nextIndex));
}

async function storeScheduledGovernedRunRecord(
	redis: Redis,
	runRecord: Record<string, unknown>,
	setAsLatest = true,
): Promise<void> {
	const runId = String(runRecord.run_id);
	const serialized = JSON.stringify(runRecord);
	await redis.set(`${DREAM_RUN_PREFIX}${runId}`, serialized);
	await redis.set(DREAM_LAST_ATTEMPT_KEY, serialized);
	if (setAsLatest) {
		await redis.set(DREAM_LAST_RUN_KEY, serialized);
	}
	await updateDreamRunIndex(redis, runId);
}

async function acquireScheduledGovernedDreamLock(
	redis: Redis,
	runId: string,
	startedAt: string,
): Promise<{ acquired: boolean; existingLock: Record<string, unknown> | null }> {
	const lockPayload = JSON.stringify({
		run_id: runId,
		run_at: startedAt,
		trigger: "scheduled",
		auto_apply_mode: "governed",
	});
	const initialAttempt = await redis.set(DREAM_LOCK_KEY, lockPayload, {
		nx: true,
		ex: DREAM_LOCK_TTL_SECONDS,
	});
	if (initialAttempt) {
		return { acquired: true, existingLock: null };
	}

	const existingLockRaw = await redis.get(DREAM_LOCK_KEY);
	const existingLock = parseStoredObject(existingLockRaw);
	const runAt = typeof existingLock?.run_at === "string" ? Date.parse(existingLock.run_at) : Number.NaN;
	const stale = !Number.isFinite(runAt) || Date.now() - runAt >= DREAM_LOCK_STALE_AFTER_SECONDS * 1000;
	if (!stale) {
		return { acquired: false, existingLock };
	}

	// 2.7 — Atomic CAS: replace the stale lock only if its value is still what we
	// observed.  Prevents the DEL→SET-NX double-acquire race where two concurrent
	// workers both delete the stale key and both succeed on SET NX.
	const staleValue = typeof existingLockRaw === "string" ? existingLockRaw : JSON.stringify(existingLockRaw);
	const casResult = await redis.eval(
		DREAM_LOCK_CAS_SCRIPT,
		[DREAM_LOCK_KEY],
		[staleValue, lockPayload, String(DREAM_LOCK_TTL_SECONDS)],
	);
	if (casResult) {
		return { acquired: true, existingLock: null };
	}
	// CAS failed — another worker already replaced the stale lock.  Report what's there now.
	const newLock = parseStoredObject(await redis.get(DREAM_LOCK_KEY));
	return { acquired: false, existingLock: newLock ?? existingLock };
}

async function releaseScheduledGovernedDreamLock(redis: Redis, runId: string): Promise<void> {
	const currentLock = parseStoredObject(await redis.get(DREAM_LOCK_KEY));
	if (currentLock?.run_id === runId) {
		await (redis as unknown as { del: (key: string) => Promise<unknown> }).del(DREAM_LOCK_KEY);
	}
}

type ScheduledGovernedDecision = {
	selectedOperationIds: string[];
	heldOperations: Array<{ operation_id: string | null; type: string | null; reason: string }>;
	operationCounts: Record<string, number>;
	selectedCounts: Record<string, number>;
};

function holdAllScheduledGovernedOperations(
	operations: Array<Record<string, unknown>>,
	reason: string,
): ScheduledGovernedDecision {
	const operationCounts: Record<string, number> = {};
	const heldOperations = operations.map((operation) => {
		const type = typeof operation.type === "string" ? operation.type : null;
		if (type) {
			operationCounts[type] = (operationCounts[type] ?? 0) + 1;
		}
		return {
			operation_id: typeof operation.operation_id === "string" ? operation.operation_id : null,
			type,
			reason,
		};
	});
	return {
		selectedOperationIds: [],
		heldOperations,
		operationCounts,
		selectedCounts: {},
	};
}

function buildScheduledGovernedDecision(
	proposal: Record<string, unknown>,
	grade: Record<string, unknown> | null,
): ScheduledGovernedDecision {
	const operations = toObjectArray(proposal.operations);
	const riskScore = typeof proposal.risk_score === "string" ? proposal.risk_score : "unknown";
	if (!SCHEDULED_DREAM_ALLOWED_RISKS.has(riskScore)) {
		return holdAllScheduledGovernedOperations(operations, `risk_score_not_auto_applicable:${riskScore}`);
	}
	// 2.6 — Enforce the judge gate: requires_judge was set by the proposal builder but
	// never read here, so proposals needing human review were auto-applied anyway.
	if (proposal.requires_judge === true) {
		return holdAllScheduledGovernedOperations(operations, "requires_judge_approval");
	}
	if (!grade || grade.passed !== true || grade.status !== "passed") {
		return holdAllScheduledGovernedOperations(operations, `grade_not_passed:${String(grade?.status ?? "missing")}`);
	}

	const selectedOperationIds: string[] = [];
	const heldOperations: ScheduledGovernedDecision["heldOperations"] = [];
	const operationCounts: Record<string, number> = {};
	const selectedCounts: Record<string, number> = {};

	for (const operation of operations) {
		const operationId = typeof operation.operation_id === "string" ? operation.operation_id : null;
		const type = typeof operation.type === "string" ? operation.type : null;
		if (type) {
			operationCounts[type] = (operationCounts[type] ?? 0) + 1;
		}
		if (!operationId || !type) {
			heldOperations.push({
				operation_id: operationId,
				type,
				reason: "missing_operation_id_or_type",
			});
			continue;
		}
		const limit = SCHEDULED_DREAM_OPERATION_LIMITS[type];
		if (typeof limit !== "number") {
			heldOperations.push({
				operation_id: operationId,
				type,
				reason: `operation_type_not_auto_applicable:${type}`,
			});
			continue;
		}
		const selectedForType = selectedCounts[type] ?? 0;
		if (selectedForType >= limit) {
			heldOperations.push({
				operation_id: operationId,
				type,
				reason: `scheduled_cap_reached:${type}:${limit}`,
			});
			continue;
		}
		selectedOperationIds.push(operationId);
		selectedCounts[type] = selectedForType + 1;
	}

	return {
		selectedOperationIds,
		heldOperations,
		operationCounts,
		selectedCounts,
	};
}

function verifyScheduledGovernedApply(
	applyResult: Record<string, unknown> | null,
	selectedOperationIds: string[],
): Record<string, unknown> {
	if (selectedOperationIds.length === 0) {
		return {
			passed: true,
			checks: [
				{ name: "no_selected_operations", passed: true },
			],
		};
	}
	const operationIds = toStringArray(applyResult?.operation_ids);
	const sideEffects = parseStoredObject(applyResult?.side_effects);
	const checks = [
		{
			name: "apply_result_ok",
			passed: applyResult?.ok === true,
		},
		{
			name: "applied_count_matches_selection",
			passed: applyResult?.applied_count === selectedOperationIds.length,
			expected: selectedOperationIds.length,
			actual: applyResult?.applied_count ?? null,
		},
		{
			name: "operation_ids_match_selection",
			passed: selectedOperationIds.every((operationId) => operationIds.includes(operationId)),
			expected: selectedOperationIds,
			actual: operationIds,
		},
		{
			name: "thin_index_rebuilt",
			passed: sideEffects?.index === "rebuilt",
			actual: sideEffects?.index ?? null,
		},
	];
	return {
		passed: checks.every((check) => check.passed),
		checks,
	};
}

async function runScheduledGovernedDream(
	env: Env,
	controller: ScheduledController,
): Promise<Record<string, unknown>> {
	const redis = createRedisClient(env);
	const startedAt = new Date().toISOString();
	const runId = `dga_${startedAt.replace(/[:.]/g, "-")}`;
	let lockAcquired = false;
	try {
		const lock = await acquireScheduledGovernedDreamLock(redis, runId, startedAt);
		if (!lock.acquired) {
			const skippedRecord = {
				schema_version: 1,
				run_id: runId,
				run_at: startedAt,
				completed_at: new Date().toISOString(),
				status: "skipped_locked",
				trigger: "scheduled",
				dry_run: false,
				auto_apply_mode: "governed",
				blocked_by: typeof lock.existingLock?.run_id === "string" ? lock.existingLock.run_id : null,
				counts: {
					operation_count: 0,
					selected_operation_count: 0,
					held_operation_count: 0,
					applied_count: 0,
				},
				next_action: "Another Dream run holds the single-flight lock; retry on the next scheduled cycle.",
			};
			await storeScheduledGovernedRunRecord(redis, skippedRecord, false);
			return skippedRecord;
		}
		lockAcquired = true;

		const existingBoundaryRun = await getScheduledGovernedBoundaryRun(redis, controller);
		if (existingBoundaryRun) {
			return existingBoundaryRun;
		}

		// 2.10 — Run Layer-2 quarantine + demote, percentile re-tier, and judge verdict
		// consumption BEFORE the proposal so forgetting executes every nightly window,
		// not only on operator-triggered runDreamCycle calls.
		let retierSummary: Awaited<ReturnType<typeof runScheduledRetierCycle>> | null = null;
		try {
			retierSummary = await runScheduledRetierCycle(env, runId, startedAt, false);
			if (retierSummary.skipped_reason) {
				console.warn("[scheduled-governed] retier cycle skipped:", retierSummary.skipped_reason);
			}
		} catch (retierError) {
			// Log but don't abort — the proposal + apply path is the primary deliverable.
			console.error("[scheduled-governed] retier cycle threw, continuing with proposal", retierError);
		}

		const proposal = await runDreamProposal(env, {
			trigger: "manual",
			actorId: "scheduled:dream-governance",
			archiveLimit: SCHEDULED_DREAM_ARCHIVE_LIMIT,
			promotionLimit: SCHEDULED_DREAM_PROMOTION_LIMIT,
			note: `Nightly Dream governed proposal. cron=${controller.cron} scheduled_time=${controller.scheduledTime}`,
		});
		const proposalId = typeof proposal.run_id === "string" ? proposal.run_id : null;
		const proposalStatus = typeof proposal.status === "string" ? proposal.status : null;
		const operations = toObjectArray(proposal.operations);
		if (!proposalId || proposalStatus !== "proposal_ready") {
			const skippedRecord = {
				schema_version: 1,
				run_id: runId,
				run_at: startedAt,
				completed_at: new Date().toISOString(),
				status: proposalStatus ?? "failed",
				trigger: "scheduled",
				dry_run: false,
				auto_apply_mode: "governed",
				proposal_id: proposalId,
				proposal_status: proposalStatus,
				counts: {
					operation_count: operations.length,
					selected_operation_count: 0,
					held_operation_count: operations.length,
					applied_count: 0,
				},
				next_action: "Dream proposal did not reach proposal_ready; no autonomous apply attempted.",
			};
			await storeScheduledGovernedRunRecord(redis, skippedRecord, false);
			await storeScheduledGovernedBoundaryBestEffort(redis, controller, runId);
			return skippedRecord;
		}

		const grade = await gradeDreamProposal(env, {
			proposalId,
			actorId: "scheduled:dream-governance",
			rubricVersion: "scheduled-governed-v1",
		});
		const decision = buildScheduledGovernedDecision(proposal, grade);
		let applyResult: Record<string, unknown> | null = null;
		let verification = verifyScheduledGovernedApply(null, []);

		if (decision.selectedOperationIds.length > 0) {
			applyResult = await applyDreamProposal(env, {
				proposalId,
				mutationId: `scheduled_governed_${proposalId}`,
				reason: "Scheduled governed Dream auto-apply",
				actorId: "scheduled:dream-governance",
				operationIds: decision.selectedOperationIds,
				requireGradePass: true,
				gradeId: typeof grade.grade_id === "string" ? grade.grade_id : null,
				maxEntriesTouched: SCHEDULED_DREAM_MAX_ENTRIES_TOUCHED,
				phase9OutcomeGate: isEnabledEnvFlag(env.DREAM_PHASE9_OUTCOME_GATE_ENABLED),
				phase9AutoRollback: isEnabledEnvFlag(env.DREAM_PHASE9_AUTO_ROLLBACK_ENABLED),
				phase9ProbeSetKey: typeof env.DREAM_PHASE9_PROBE_SET_KEY === "string" && env.DREAM_PHASE9_PROBE_SET_KEY.length > 0
					? env.DREAM_PHASE9_PROBE_SET_KEY
					: null,
				phase9WriteValidationLedger: isEnabledEnvFlag(env.DREAM_PHASE9_WRITE_VALIDATION_LEDGER),
			});
			verification = verifyScheduledGovernedApply(applyResult, decision.selectedOperationIds);
		}

		const appliedCount = typeof applyResult?.applied_count === "number" ? applyResult.applied_count : 0;
		const passedGrade = grade.passed === true && grade.status === "passed";
		const status = verification.passed !== true
			? "failed"
			: !passedGrade || (decision.selectedOperationIds.length === 0 && decision.heldOperations.length > 0)
				? "held"
				: decision.heldOperations.length > 0
					? "completed_with_holds"
					: "completed";
		const runRecord = {
			schema_version: 1,
			run_id: runId,
			run_at: startedAt,
			completed_at: new Date().toISOString(),
			status,
			trigger: "scheduled",
			dry_run: false,
			auto_apply_mode: "governed",
			cron: controller.cron,
			scheduled_time: controller.scheduledTime,
			proposal_id: proposalId,
			proposal_status: proposalStatus,
			risk_score: typeof proposal.risk_score === "string" ? proposal.risk_score : null,
			grade_id: typeof grade.grade_id === "string" ? grade.grade_id : null,
			grade_status: typeof grade.status === "string" ? grade.status : null,
			apply_run_id: typeof applyResult?.apply_run_id === "string" ? applyResult.apply_run_id : null,
			mutation_id: typeof applyResult?.mutation_id === "string" ? applyResult.mutation_id : null,
			counts: {
				operation_count: operations.length,
				selected_operation_count: decision.selectedOperationIds.length,
				held_operation_count: decision.heldOperations.length,
				applied_count: appliedCount,
				archive_limit: SCHEDULED_DREAM_ARCHIVE_LIMIT,
				promotion_limit: SCHEDULED_DREAM_PROMOTION_LIMIT,
				duplicate_merge_limit: SCHEDULED_DREAM_DUPLICATE_MERGE_LIMIT,
				mark_contested_limit: SCHEDULED_DREAM_MARK_CONTESTED_LIMIT,
				operation_counts: decision.operationCounts,
				selected_counts: decision.selectedCounts,
			},
			selected_operation_ids: decision.selectedOperationIds,
			held_operations: decision.heldOperations,
			apply_result: applyResult
				? {
					ok: applyResult.ok === true,
					error: typeof applyResult.error === "string" ? applyResult.error : null,
					applied_count: appliedCount,
					operation_ids: toStringArray(applyResult.operation_ids),
				}
				: null,
			// 2.10 — Include the retier/Layer-2/judge-verdicts summary so nightly
			// forgetting is observable in the run record.
			retier_cycle: retierSummary
				? {
					layer2_quarantined: retierSummary.layer2.quarantined.length,
					layer2_demoted: retierSummary.layer2.demoted.length,
					retier_changed: retierSummary.retier.changed,
					verdicts_applied: retierSummary.verdicts_applied,
					verdicts_skipped: retierSummary.verdicts_skipped,
					skipped_reason: retierSummary.skipped_reason ?? null,
				}
				: null,
			verification,
			// 4.6 — Scheduled governed Dream has no kill switch (the cron always runs
			// the proposal→grade→apply chain).  Removed the misleading "kill-flag
			// state" clause from the failed/held message — held ops are held by
			// policy, not by a flag, and a kill flag doesn't restart a held run.
			next_action: status === "completed" || status === "completed_with_holds"
				? "Scheduled governed Dream auto-apply completed within caps; held operations will be reconsidered by future runs or judge policy."
				: "Scheduled governed Dream held or failed; inspect the grade and held operations. To release held ops, update the governing policy or wait for the judge queue to clear — no kill flag controls the scheduled Dream path.",
		};
		await storeScheduledGovernedRunRecord(redis, runRecord, status !== "held");
		await storeScheduledGovernedBoundaryBestEffort(redis, controller, runId);
		return runRecord;
	} catch (error) {
		const message = error instanceof Error ? error.message : String(error);
		const failedRecord = {
			schema_version: 1,
			run_id: runId,
			run_at: startedAt,
			completed_at: new Date().toISOString(),
			status: "failed",
			trigger: "scheduled",
			dry_run: false,
			auto_apply_mode: "governed",
			error: message,
			counts: {
				operation_count: 0,
				selected_operation_count: 0,
				held_operation_count: 0,
				applied_count: 0,
			},
			// 2.4 — Accurate next_action (no kill-flag is set; the lock is released in finally).
			next_action: "Scheduled governed Dream threw before completion; inspect the error and re-run manually or wait for the next scheduled trigger.",
		};
		try {
			await storeScheduledGovernedRunRecord(redis, failedRecord, true);
			// 2.4 — Do NOT store the boundary key for failed runs; only completed runs
			// satisfy the boundary so that the repair path can run a fresh attempt the
			// same day without being blocked by a failed record.
		} catch (storeError) {
			console.error("[scheduled-governed] could not store failed run record", storeError);
		}
		return failedRecord;
	} finally {
		if (lockAcquired) {
			await releaseScheduledGovernedDreamLock(redis, runId);
		}
	}
}

// Define our MCP agent with knowledge tools
export class KnowledgeMCP extends McpAgent<Env, unknown, AuthProps> {
	server = new McpServer({
		name: "Personal Knowledge System",
		version: "1.0.0",
	});

	protected includeWriteTools(): boolean {
		return true;
	}

	private getRedis(env: Env): Redis {
		return createRedisClient(env);
	}

	private getVector(env: Env): Index {
		return createVectorClient(env);
	}

	private getAuthProps(): AuthProps {
		const contextProps = getMcpAuthContext()?.props ?? {};
		const merged = {
			...contextProps,
			...(this.props ?? {}),
		} as AuthProps;
		return {
			userId: typeof merged.userId === "string" ? merged.userId : undefined,
			scope: typeof merged.scope === "string" ? merged.scope : undefined,
			scopes: normalizeScopes(merged.scopes ?? merged.scope),
		};
	}

	private async readTweet(url: string, includeMediaAlt: boolean): Promise<ReadTweetOutput> {
		const timeoutMs = getTweetTimeoutMs(this.env);
		const normalizedUrl = await normalizeTweetUrl(url, timeoutMs);
		const redis = this.getRedis(this.env);
		try {
			const cached = await getCachedTweet(redis, normalizedUrl, includeMediaAlt);
			if (cached) {
				this.ctx.waitUntil(enqueueTweetRead(redis, cached, normalizedUrl, "hit").catch(() => undefined));
				return cached;
			}
		} catch {
			// Cache must never block public tweet reads.
		}

		const tweet = await fetchTweetWithFallback(normalizedUrl, {
			includeMediaAlt,
			timeoutMs,
		});
		try {
			await setCachedTweet(redis, tweet, includeMediaAlt, getTweetCacheTtlSeconds(this.env));
		} catch {
			// Upstream success is more important than a cache write.
		}
		this.ctx.waitUntil(enqueueTweetRead(redis, tweet, normalizedUrl, "miss").catch(() => undefined));
		return tweet;
	}

	private async readThread(url: string, maxDepth: number): Promise<ReadThreadOutput> {
		const timeoutMs = getTweetTimeoutMs(this.env);
		const normalizedUrl = await normalizeTweetUrl(url, timeoutMs);
		const thread = await fetchFxThread(normalizedUrl, {
			includeMediaAlt: true,
			timeoutMs,
		});
		const limitedTweets = thread.tweets.slice(0, maxDepth);
		const limited = {
			...thread,
			root: limitedTweets[0] ?? thread.root,
			tweets: limitedTweets,
			count: limitedTweets.length,
		};
		const redis = this.getRedis(this.env);
		this.ctx.waitUntil(enqueueTweetRead(redis, limited.root, normalizedUrl, "miss").catch(() => undefined));
		return limited;
	}

	private async requireWriteAccess(action: string, limit: number = WRITE_TOOL_RATE_LIMIT): Promise<string> {
		const authProps = this.getAuthProps();
		const userId = authProps.userId;
		if (!userId) {
			throw new Error(`Authenticated user context missing for ${action}`);
		}

		const scopes = new Set(normalizeScopes(authProps.scopes ?? authProps.scope));
		if (!scopes.has("mcp:write")) {
			throw new Error(`mcp:write scope required for ${action}`);
		}

		const redis = this.getRedis(this.env);
		const rateLimit = await applyFixedWindowRateLimit(redis, `mcp:${userId}`, action, limit);
		if (!rateLimit.allowed) {
			throw new Error(
				`Rate limit exceeded for ${action}. Allowed ${rateLimit.limit} calls per ${RATE_LIMIT_WINDOW_SECONDS} seconds.`,
			);
		}

		return userId;
	}

	private async loadEntry(
		redis: Redis,
		entryType: EntryType,
		entryId: string,
	): Promise<Record<string, unknown> | null> {
		const entry = normalizeEntry(await redis.get(getEntryKey(entryType, entryId)), entryType);
		return this.hydrateEntryAccessSignals(redis, entry);
	}

	private async hydrateEntryAccessSignals(
		redis: Redis,
		entry: Record<string, unknown> | null,
	): Promise<Record<string, unknown> | null> {
		const entryId = getEntryId(entry);
		if (!entry || !entryId) return entry;

		const [accessCountRaw, lastAccessedRaw] = await Promise.all([
			redis.get(getEntryAccessKey(entryId)),
			redis.get(getEntryLastAccessedKey(entryId)),
		]);
		return applyAccessSignals(entry, accessCountRaw, lastAccessedRaw);
	}

	private scheduleReconsolidation(entryType: EntryType, entryId: string): void {
		this.ctx.waitUntil((async () => {
			try {
				await this.reconsolidateEntry(entryType, entryId);
			} catch (error) {
				await this.logReconsolidationError(entryType, entryId, error);
			}
		})());
	}

	private async logReconsolidationError(
		entryType: EntryType,
		entryId: string,
		error: unknown,
	): Promise<void> {
		try {
			const redis = this.getRedis(this.env);
			const timestamp = new Date();
			const message = error instanceof Error ? error.message : String(error);
			const payload = JSON.stringify({
				timestamp: timestamp.toISOString(),
				entry_id: entryId,
				entry_type: entryType,
				error: message,
			});

			await redis.lpush(getReconsolidationErrorKey(timestamp), payload);
			await redis.ltrim(
				getReconsolidationErrorKey(timestamp),
				0,
				MAX_RECONSOLIDATION_ERROR_LOGS - 1,
			);
		} catch {
			// Swallow logging failures so reconsolidation never cascades into user-visible errors.
		}
	}

	private async reconsolidateEntry(entryType: EntryType, entryId: string): Promise<void> {
		const redis = this.getRedis(this.env);
		const vector = this.getVector(this.env);
		const entryKey = getEntryKey(entryType, entryId);
		const accessCountKey = getEntryAccessKey(entryId);
		const lastAccessedKey = getEntryLastAccessedKey(entryId);
		const now = new Date().toISOString();

		const currentEntry = normalizeEntry(await redis.get(entryKey), entryType);
		if (!currentEntry) return;

		const currentMetadata = getEntryMetadata(currentEntry);
		const baselineAccessCount = toOptionalInteger(currentMetadata.access_count) ?? 0;

		await redis.setnx(accessCountKey, baselineAccessCount);
		await redis.incr(accessCountKey);
		await redis.set(lastAccessedKey, now);

		const [latestRawEntry, effectiveAccessCount, effectiveLastAccessed] = await Promise.all([
			redis.get(entryKey),
			redis.get(accessCountKey),
			redis.get(lastAccessedKey),
		]);

		const latestEntry = normalizeEntry(latestRawEntry, entryType) ?? currentEntry;
		// 3.3 — If Dream archived this entry while the reconsolidation was queued,
		// writing back would un-archive it.  Abort and leave the archived state intact.
		if ((getEntryMetadata(latestEntry) as Record<string, unknown>).archived === true) return;
		const updatedEntry = applyAccessSignals(
			latestEntry,
			effectiveAccessCount,
			effectiveLastAccessed,
		);
		const updatedMetadata = getEntryMetadata(updatedEntry);
		const accessCount = toOptionalInteger(updatedMetadata.access_count) ?? baselineAccessCount;

		if (
			updatedMetadata.context_type === "task_query" &&
			accessCount >= RECONSOLIDATION_PROMOTION_THRESHOLD
		) {
			updatedMetadata.context_type = "recurring_pattern";
			updatedMetadata.injection_tier = 2;
			appendConsolidationNote(
				updatedMetadata,
				formatConsolidationNote({
					timestamp: now,
					source: "reconsolidation",
					action: "promote_context_type",
					detail: `task_query -> recurring_pattern (access_count reached ${accessCount})`,
				}),
			);
		}

		updatedMetadata.last_consolidated = now;
		updatedMetadata.salience_score = computeSalience(updatedEntry);

		// Layer 2 quarantine is reversible by retrieval reinforcement: any
		// access (which is what reconsolidate represents) lifts the quarantine
		// flag and resets the streak so the entry can re-earn its tier.
		if (updatedMetadata.injection_quarantine) {
			updatedMetadata.injection_quarantine = false;
			updatedMetadata.quarantined_at = null;
			updatedMetadata.quarantine_streak_nights = 0;
			appendConsolidationNote(
				updatedMetadata,
				formatConsolidationNote({
					timestamp: now,
					source: "reconsolidation",
					action: "lift_quarantine",
					detail: `quarantine cleared after access (access_count=${accessCount})`,
				}),
			);
		}

		await redis.set(entryKey, JSON.stringify(updatedEntry));
		await vector.update({
			id: entryId,
			metadata: buildReconsolidatedVectorMetadata(updatedEntry),
			metadataUpdateMode: "PATCH",
		});
	}

	private async getEmbedding(env: Env, text: string): Promise<number[]> {
		if (!env.OPENAI_API_KEY) {
			throw new Error("OPENAI_API_KEY not configured");
		}
		try {
			const openai = new OpenAI({ apiKey: env.OPENAI_API_KEY });
			const response = await openai.embeddings.create({
				model: "text-embedding-3-large",
				input: text,
				dimensions: 3072,
			});
			return response.data[0].embedding;
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			throw new Error(`OpenAI embedding failed: ${msg}`);
		}
	}

	async init() {
		// Tool: get_index
		this.server.tool(
			"get_index",
			"Get the thin index - a compressed view of all knowledge topics and projects. Call this first to see what knowledge exists.",
			{},
			READ_ONLY_TOOL_ANNOTATIONS,
			async () => {
				const redis = this.getRedis(this.env);
				const rawIndex = parseStoredObject(await redis.get("index:current")) as {
					topics?: Array<{ id: string; domain: string; current_view_summary?: string; state?: string; confidence?: string; last_updated?: string; context_type?: string; injection_tier?: number; salience_score?: number; mention_count?: number; archived?: boolean; top_repo?: string }>;
					projects?: Array<{ id: string; name: string; goal_summary?: string; status?: string; current_phase?: string; last_touched?: string; context_type?: string; injection_tier?: number; salience_score?: number; mention_count?: number; archived?: boolean }>;
					generated_at?: string;
					token_count?: number;
					total_topic_count?: number;
					total_project_count?: number;
					tier_1_count?: number;
					tier_2_count?: number;
					tier_3_count?: number;
					archived_count?: number;
				} | null;
				const dreamSummary = parseStoredObject(await redis.get("dream:last_run"));

				if (!rawIndex) {
					return { content: [{ type: "text", text: JSON.stringify({ topics: [], projects: [], message: "No index found" }) }] };
				}

				const topics = (rawIndex.topics || []).filter((topic) => !topic.archived);
				const projects = (rawIndex.projects || []).filter((project) => !project.archived);

				const sortedTopics = [...topics].sort((a, b) => {
					const tierA = typeof a.injection_tier === "number" ? a.injection_tier : 3;
					const tierB = typeof b.injection_tier === "number" ? b.injection_tier : 3;
					if (tierA !== tierB) return tierA - tierB;
					const salienceA = typeof a.salience_score === "number" ? a.salience_score : 0;
					const salienceB = typeof b.salience_score === "number" ? b.salience_score : 0;
					if (salienceA !== salienceB) return salienceB - salienceA;
					const dateA = a.last_updated ? new Date(a.last_updated).getTime() : 0;
					const dateB = b.last_updated ? new Date(b.last_updated).getTime() : 0;
					return dateB - dateA;
				});

				const compactTopics = sortedTopics.slice(0, 100).map(t => ({
					id: t.id,
					domain: t.domain,
					summary: (t.current_view_summary || "").substring(0, 100),
					state: t.state,
					updated: t.last_updated ? t.last_updated.substring(0, 10) : null,
					injection_tier: typeof t.injection_tier === "number" ? t.injection_tier : 3,
					context_type: t.context_type || null,
					salience_score: typeof t.salience_score === "number" ? t.salience_score : null,
					mention_count: typeof t.mention_count === "number" ? t.mention_count : null,
					top_repo: t.top_repo || null,
				}));

				const sortedProjects = [...projects].sort((a, b) => {
					const tierA = typeof a.injection_tier === "number" ? a.injection_tier : 3;
					const tierB = typeof b.injection_tier === "number" ? b.injection_tier : 3;
					if (tierA !== tierB) return tierA - tierB;
					const salienceA = typeof a.salience_score === "number" ? a.salience_score : 0;
					const salienceB = typeof b.salience_score === "number" ? b.salience_score : 0;
					if (salienceA !== salienceB) return salienceB - salienceA;
					const dateA = a.last_touched ? new Date(a.last_touched).getTime() : 0;
					const dateB = b.last_touched ? new Date(b.last_touched).getTime() : 0;
					return dateB - dateA;
				});

				const compactProjects = sortedProjects.slice(0, 50).map(p => ({
					id: p.id,
					name: p.name,
					goal: (p.goal_summary || "").substring(0, 80),
					status: p.status,
					phase: (p.current_phase || "").substring(0, 60),
					touched: p.last_touched ? p.last_touched.substring(0, 10) : null,
					injection_tier: typeof p.injection_tier === "number" ? p.injection_tier : 3,
					context_type: p.context_type || null,
					salience_score: typeof p.salience_score === "number" ? p.salience_score : null,
					mention_count: typeof p.mention_count === "number" ? p.mention_count : null,
				}));

				const compactIndex = {
					total_topics: typeof rawIndex.total_topic_count === "number" ? rawIndex.total_topic_count : topics.length,
					total_projects: typeof rawIndex.total_project_count === "number" ? rawIndex.total_project_count : projects.length,
					tier_1_count: typeof rawIndex.tier_1_count === "number" ? rawIndex.tier_1_count : null,
					tier_2_count: typeof rawIndex.tier_2_count === "number" ? rawIndex.tier_2_count : null,
					tier_3_count: typeof rawIndex.tier_3_count === "number" ? rawIndex.tier_3_count : null,
					archived_count: typeof rawIndex.archived_count === "number" ? rawIndex.archived_count : 0,
					last_dream_run: typeof dreamSummary?.run_at === "string" ? dreamSummary.run_at : null,
					generated_at: rawIndex.generated_at || null,
					showing_recent: { topics: compactTopics.length, projects: compactProjects.length },
					topics: compactTopics,
					projects: compactProjects,
					note: "Showing the thin-index subset ordered by tier then salience. Use 'search' for query-specific retrieval or 'get_context' for the full entry."
				};

				return {
					content: [{ type: "text", text: JSON.stringify(compactIndex) }],
				};
			}
		);

		// Tool: get_dream_summary
		this.server.tool(
			"get_dream_summary",
			"Get the most recent Dream job audit summary, including dry-run status and archive-candidate counts.",
			{},
			READ_ONLY_TOOL_ANNOTATIONS,
			async () => {
				const redis = this.getRedis(this.env);
				const dreamSummary = parseStoredObject(await redis.get("dream:last_run"));
				if (!dreamSummary) {
					return {
						content: [{ type: "text", text: JSON.stringify({ message: "No Dream runs recorded yet." }) }],
					};
				}

				return {
					content: [{ type: "text", text: JSON.stringify(dreamSummary) }],
				};
			}
		);

		this.server.tool(
			"get_validation_status",
			"Get the latest validation ledger status. Use this to distinguish runtime health from memory correctness.",
			{},
			READ_ONLY_TOOL_ANNOTATIONS,
			async () => {
				const redis = this.getRedis(this.env);
				return {
					content: [{ type: "text", text: JSON.stringify(await getValidationStatus(redis)) }],
				};
			},
		);

		this.server.tool(
			"read_tweet",
			"Read a public X/Twitter post from a URL. Returns full text, author, timestamp, engagement metrics, media URLs, community note, directly quoted post, and X Article body when available. Use when the user pastes any x.com or twitter.com status link, including mobile.x.com, twitter.com, x.com/i/status, fxtwitter, vxtwitter, fixupx, nitter, or t.co links.",
			{
				url: z.string().min(1).max(2048).describe("The X/Twitter status URL to read."),
				include_media_alt: z.boolean().default(true).describe("Include image/video alt text when upstream APIs provide it."),
			},
			OPEN_WORLD_READ_ONLY_TOOL_ANNOTATIONS,
			async ({ url, include_media_alt }) => {
				try {
					const tweet = await this.readTweet(url, include_media_alt);
					return toolJson(tweet);
				} catch (error) {
					return toolError(tweetReaderErrorMessage(error));
				}
			},
		);

		this.server.tool(
			"read_thread",
			"Read a public self-thread from any X/Twitter status URL in the thread. Returns the root post plus same-author continuation replies in order when FxTwitter exposes the unrolled thread. Use when the user asks to read, summarize, or quote a whole Twitter/X thread.",
			{
				url: z.string().min(1).max(2048).describe("Any X/Twitter status URL from the thread."),
				max_depth: z.number().int().min(1).max(100).default(25).describe("Maximum number of same-author thread posts to return."),
			},
			OPEN_WORLD_READ_ONLY_TOOL_ANNOTATIONS,
			async ({ url, max_depth }) => {
				try {
					const thread = await this.readThread(url, max_depth);
					return toolJson(thread);
				} catch (error) {
					return toolError(tweetReaderErrorMessage(error));
				}
			},
		);

		this.server.tool(
			"health",
			"Check the Personal Knowledge MCP and tweet reader upstream health. Returns build metadata and FxTwitter/VxTwitter/ADHX probe statuses.",
			{},
			READ_ONLY_TOOL_ANNOTATIONS,
			async () => {
				const payload = {
					status: "ok",
					build: this.env.BUILD_SHA ?? "unknown",
					tweet_reader: {
						timeout_ms: getTweetTimeoutMs(this.env),
						cache_ttl_seconds: getTweetCacheTtlSeconds(this.env),
						queue_key: "tweet_reader:queue",
						upstream_status: await checkTweetUpstreams(1500),
					},
				};
				return toolJson(payload);
			},
		);

		this.server.tool(
			"list_dream_runs",
			"List recent Dream run summaries from the run ledger.",
			{
				limit: z.number().int().min(1).max(50).default(10),
				status_filter: z.string().min(1).max(80).optional(),
			},
			READ_ONLY_TOOL_ANNOTATIONS,
			async ({ limit, status_filter }) => {
				const redis = this.getRedis(this.env);
				return {
					content: [{ type: "text", text: JSON.stringify(await listDreamRuns(redis, limit, status_filter)) }],
				};
			},
		);

		this.server.tool(
			"get_dream_run",
			"Get a Dream run record by run_id.",
			{
				run_id: z.string().min(1).max(200),
			},
			READ_ONLY_TOOL_ANNOTATIONS,
			async ({ run_id }) => {
				const redis = this.getRedis(this.env);
				return {
					content: [{ type: "text", text: JSON.stringify(await getDreamRun(redis, run_id)) }],
				};
			},
		);

		this.server.tool(
			"get_dream_events",
			"Get retained Dream run events by run_id.",
			{
				run_id: z.string().min(1).max(200),
			},
			READ_ONLY_TOOL_ANNOTATIONS,
			async ({ run_id }) => {
				const redis = this.getRedis(this.env);
				return {
					content: [{ type: "text", text: JSON.stringify(await getDreamEvents(redis, run_id)) }],
				};
			},
		);

		if (this.includeWriteTools()) {
			// Tool: run_dream_proposal
			this.server.tool(
				"run_dream_proposal",
				"Generate a no-write Dream governance proposal. Requires mcp:write scope; does not mutate entries or vectors.",
				{
					candidate_ids: z.array(z.string().min(1).max(200)).max(200).optional().describe("Optional entry IDs to restrict proposal generation"),
					archive_limit: z.number().int().min(0).max(MAX_OPERATOR_DREAM_ARCHIVE_LIMIT).optional().describe("Maximum archive operations to propose"),
					promotion_limit: z.number().int().min(0).max(MAX_OPERATOR_DREAM_ARCHIVE_LIMIT).optional().describe("Maximum promotion operations to propose"),
					note: z.string().min(1).max(500).optional().describe("Operator note for the proposal audit"),
				},
				MUTATING_TOOL_ANNOTATIONS,
				async ({ candidate_ids, archive_limit, promotion_limit, note }) => {
					try {
						const actorId = await this.requireWriteAccess("run_dream_proposal");
						const result = await runDreamProposal(this.env, {
							trigger: "manual",
							actorId,
							candidateIds: candidate_ids,
							archiveLimit: archive_limit,
							promotionLimit: promotion_limit,
							note,
						});
						return {
							content: [{ type: "text", text: JSON.stringify(result) }],
						};
					} catch (error) {
						const errMsg = error instanceof Error ? error.message : String(error);
						return {
							content: [{ type: "text", text: JSON.stringify({ error: errMsg }) }],
						};
					}
				},
			);

			// Tool: grade_dream_proposal
			this.server.tool(
				"grade_dream_proposal",
				"Run deterministic hard-gate grading for a Dream proposal. Requires mcp:write scope; does not mutate entries or vectors.",
				{
					proposal_id: z.string().min(1).max(200).describe("Dream proposal run ID, usually dpr_..."),
					rubric_version: z.string().min(1).max(100).optional().describe("Optional rubric version; defaults to deterministic-v1"),
				},
				MUTATING_TOOL_ANNOTATIONS,
				async ({ proposal_id, rubric_version }) => {
					try {
						const actorId = await this.requireWriteAccess("grade_dream_proposal");
						const result = await gradeDreamProposal(this.env, {
							proposalId: proposal_id,
							actorId,
							rubricVersion: rubric_version,
						});
						return {
							content: [{ type: "text", text: JSON.stringify(result) }],
						};
					} catch (error) {
						const errMsg = error instanceof Error ? error.message : String(error);
						return {
							content: [{ type: "text", text: JSON.stringify({ error: errMsg }) }],
						};
					}
				},
			);

			// Tool: apply_dream_proposal
			this.server.tool(
				"apply_dream_proposal",
				"Apply an approved Dream governance proposal after rechecking expected revisions. Requires mcp:write scope.",
				{
					proposal_id: z.string().min(1).max(200).describe("Dream proposal run ID, usually dpr_..."),
					mutation_id: z.string().min(1).max(200).describe("Client-generated idempotency key for this apply request"),
					reason: z.string().min(1).max(500).describe("Why this proposal is approved for application"),
					require_grade_pass: z.boolean().optional().describe("Require stored deterministic grade pass before mutating; defaults to true"),
					grade_id: z.string().min(1).max(200).optional().describe("Optional specific grade id to require"),
					operation_ids: z.array(z.string().min(1).max(300)).max(100).optional().describe("Optional subset of proposal operation IDs to apply"),
					phase9_outcome_gate: z.boolean().optional().describe("Run Phase 9 pre/post outcome probes around apply; defaults to false"),
					phase9_auto_rollback: z.boolean().optional().describe("Automatically rollback when Phase 9 detects a post-apply regression; defaults to false"),
					phase9_probe_set_key: z.string().min(1).max(300).optional().describe("Redis key containing the bounded Phase 9 outcome probe set"),
					phase9_write_validation_ledger: z.boolean().optional().describe("Write dream_outcome_quality to the validation ledger; defaults to false"),
				},
				MUTATING_TOOL_ANNOTATIONS,
				async ({ proposal_id, mutation_id, reason, require_grade_pass, grade_id, operation_ids, phase9_outcome_gate, phase9_auto_rollback, phase9_probe_set_key, phase9_write_validation_ledger }) => {
					try {
						const actorId = await this.requireWriteAccess("apply_dream_proposal");
						const result = await applyDreamProposal(this.env, {
							proposalId: proposal_id,
							mutationId: mutation_id,
							reason,
							actorId,
							operationIds: operation_ids,
							requireGradePass: require_grade_pass,
							gradeId: grade_id,
							phase9OutcomeGate: phase9_outcome_gate,
							phase9AutoRollback: phase9_auto_rollback,
							phase9ProbeSetKey: phase9_probe_set_key,
							phase9WriteValidationLedger: phase9_write_validation_ledger,
						});
						return {
							content: [{ type: "text", text: JSON.stringify(result) }],
						};
					} catch (error) {
						const errMsg = error instanceof Error ? error.message : String(error);
						return {
							content: [{ type: "text", text: JSON.stringify({ error: errMsg }) }],
						};
					}
				},
			);

			// Tool: rollback_dream_apply
			this.server.tool(
				"rollback_dream_apply",
				"Rollback supported operations from a previously applied Dream proposal. Requires mcp:write scope.",
				{
					proposal_id: z.string().min(1).max(200).describe("Dream proposal run ID, usually dpr_..."),
					apply_mutation_id: z.string().min(1).max(200).describe("Mutation ID used when the proposal was applied"),
					rollback_mutation_id: z.string().min(1).max(200).describe("Client-generated idempotency key for this rollback request"),
					reason: z.string().min(1).max(500).describe("Why this applied proposal should be rolled back"),
					operation_ids: z.array(z.string().min(1).max(300)).max(100).optional().describe("Optional subset of applied operation IDs to roll back"),
				},
				MUTATING_TOOL_ANNOTATIONS,
				async ({ proposal_id, apply_mutation_id, rollback_mutation_id, reason, operation_ids }) => {
					try {
						const actorId = await this.requireWriteAccess("rollback_dream_apply");
						const result = await rollbackDreamApply(this.env, {
							proposalId: proposal_id,
							applyMutationId: apply_mutation_id,
							rollbackMutationId: rollback_mutation_id,
							reason,
							actorId,
							operationIds: operation_ids,
						});
						return {
							content: [{ type: "text", text: JSON.stringify(result) }],
						};
					} catch (error) {
						const errMsg = error instanceof Error ? error.message : String(error);
						return {
							content: [{ type: "text", text: JSON.stringify({ error: errMsg }) }],
						};
					}
				},
			);

			// Tool: restore_archived
			this.server.tool(
				"restore_archived",
				"Restore an archived entry back into active memory. Requires mcp:write scope.",
				{
					id: z.string().describe("Entry ID to restore (ke_xxx or pe_xxx)"),
					reason: z.string().min(1).max(500).describe("Why this archived entry should be restored"),
				},
				MUTATING_TOOL_ANNOTATIONS,
				async ({ id, reason }) => {
					try {
						await this.requireWriteAccess("restore_archived");
						const result = await restoreArchivedEntry(this.env, id, reason);
						return {
							content: [{ type: "text", text: JSON.stringify(result) }],
						};
					} catch (error) {
						const errMsg = error instanceof Error ? error.message : String(error);
						return {
							content: [{ type: "text", text: JSON.stringify({ error: errMsg }) }],
						};
					}
				},
			);

				// Tool: create_entry
				this.server.tool(
					"create_entry",
					"Create a new durable knowledge entry when no existing entry matches. Use this to save a new memory from the current chat. Requires mcp:write scope.",
					{
						mutation_id: z.string().min(1).max(200).describe("Client-generated idempotency key for this mutation"),
						reason: z.string().min(1).max(500).describe("Why this new memory should be created"),
						domain: z.string().min(1).max(240).describe("Short topic/domain label for the new knowledge entry"),
						current_view: z.string().min(1).max(4000).describe("Canonical current summary of the memory"),
						confidence: z.enum(["low", "medium", "high"]).optional().describe("Initial confidence level"),
						state: z.enum(["active", "contested", "stale"]).optional().describe("Initial knowledge-entry state"),
						context_type: z.enum(CONTEXT_TYPES).optional().describe("Initial context type. Defaults to explicit_save."),
						key_insights: z.array(z.string().min(1).max(500)).max(10).optional().describe("Optional durable insights to seed on the new entry"),
						source_conversation_id: z.string().min(1).max(200).optional().describe("Optional source conversation identifier"),
						source_message_ids: z.array(z.string().min(1).max(200)).max(20).optional().describe("Optional source message identifiers"),
						evidence_snippet: z.string().min(1).max(1000).optional().describe("Optional evidence snippet from the source conversation"),
					},
					MUTATING_TOOL_ANNOTATIONS,
					async ({
						mutation_id,
						reason,
						domain,
						current_view,
						confidence,
						state,
						context_type,
						key_insights,
						source_conversation_id,
						source_message_ids,
						evidence_snippet,
					}) => {
						try {
							const actorId = await this.requireWriteAccess("create_entry");
							const result = await createEntry(this.env, {
								mutationId: mutation_id,
								reason,
								actorId,
								domain,
								currentView: current_view,
								confidence,
								state,
								contextType: context_type,
								keyInsights: key_insights,
								sourceConversationId: source_conversation_id,
								sourceMessageIds: source_message_ids,
								evidenceSnippet: evidence_snippet,
							});
							return {
								content: [{ type: "text", text: JSON.stringify(result) }],
							};
						} catch (error) {
							const errMsg = error instanceof Error ? error.message : String(error);
							return {
								content: [{ type: "text", text: JSON.stringify({ error: errMsg }) }],
							};
						}
					},
				);

				// Tool: set_context_type
				this.server.tool(
					"set_context_type",
					"Override an active entry's context type. Requires mcp:write scope.",
					{
						id: z.string().describe("Entry ID to update (ke_xxx or pe_xxx)"),
						expected_revision: z.number().int().min(0).describe("Revision number last observed by the client"),
						mutation_id: z.string().min(1).max(200).describe("Client-generated idempotency key for this mutation"),
						context_type: z.enum(CONTEXT_TYPES).describe("Replacement context type"),
						reason: z.string().min(1).max(500).describe("Why this override is needed"),
					},
					MUTATING_TOOL_ANNOTATIONS,
					async ({ id, expected_revision, mutation_id, context_type, reason }) => {
						try {
							const actorId = await this.requireWriteAccess("set_context_type");
							const result = await updateEntry(this.env, {
								entryId: id,
								expectedRevision: expected_revision,
								mutationId: mutation_id,
								reason,
								actorId,
								contextType: context_type,
							});
							return {
								content: [{ type: "text", text: JSON.stringify(result) }],
							};
						} catch (error) {
							const errMsg = error instanceof Error ? error.message : String(error);
							return {
								content: [{ type: "text", text: JSON.stringify({ error: errMsg }) }],
							};
						}
					},
				);

				// Tool: archive_entry
				this.server.tool(
					"archive_entry",
					"Archive an active entry so it is excluded from normal retrieval surfaces. Requires mcp:write scope.",
					{
						id: z.string().describe("Entry ID to archive (ke_xxx or pe_xxx)"),
						expected_revision: z.number().int().min(0).describe("Revision number last observed by the client"),
						mutation_id: z.string().min(1).max(200).describe("Client-generated idempotency key for this mutation"),
						reason: z.string().min(1).max(500).describe("Why this entry is being archived"),
					},
					MUTATING_TOOL_ANNOTATIONS,
					async ({ id, expected_revision, mutation_id, reason }) => {
						try {
							const actorId = await this.requireWriteAccess("archive_entry");
							const result = await archiveExistingEntry(this.env, {
								entryId: id,
								expectedRevision: expected_revision,
								mutationId: mutation_id,
								reason,
								actorId,
							});
							return {
								content: [{ type: "text", text: JSON.stringify(result) }],
							};
						} catch (error) {
							const errMsg = error instanceof Error ? error.message : String(error);
							return {
								content: [{ type: "text", text: JSON.stringify({ error: errMsg }) }],
							};
						}
					},
				);

				// Tool: restore_entry
				this.server.tool(
					"restore_entry",
					"Restore an archived entry from its latest snapshot. Requires mcp:write scope.",
					{
						id: z.string().describe("Entry ID to restore (ke_xxx or pe_xxx)"),
						expected_revision: z.number().int().min(0).describe("Revision number last observed by the client"),
						mutation_id: z.string().min(1).max(200).describe("Client-generated idempotency key for this mutation"),
						reason: z.string().min(1).max(500).describe("Why this entry should be restored"),
						restore_overrides: z.object({
							current_view: z.string().min(1).optional(),
							confidence: z.enum(["low", "medium", "high"]).optional(),
							state: z.enum(["active", "contested", "stale"]).optional(),
							context_type: z.enum(CONTEXT_TYPES).optional(),
						}).optional().describe("Optional field overrides to apply during restore"),
					},
					MUTATING_TOOL_ANNOTATIONS,
					async ({ id, expected_revision, mutation_id, reason, restore_overrides }) => {
						try {
							const actorId = await this.requireWriteAccess("restore_entry");
							const result = await restoreEntry(this.env, {
								entryId: id,
								expectedRevision: expected_revision,
								mutationId: mutation_id,
								reason,
								actorId,
								restoreOverrides: restore_overrides
									? {
										currentView: restore_overrides.current_view,
										confidence: restore_overrides.confidence,
										state: restore_overrides.state,
										contextType: restore_overrides.context_type,
									}
									: undefined,
							});
							return {
								content: [{ type: "text", text: JSON.stringify(result) }],
							};
						} catch (error) {
							const errMsg = error instanceof Error ? error.message : String(error);
							return {
								content: [{ type: "text", text: JSON.stringify({ error: errMsg }) }],
							};
						}
					},
				);

				// Tool: consolidate_entries
				this.server.tool(
					"consolidate_entries",
					"Keep one entry as canonical and archive the superseded duplicates. Requires mcp:write scope.",
					{
						keep_id: z.string().describe("Canonical entry to retain"),
						archive_ids: z.array(z.string()).min(1).describe("Superseded entries to archive"),
						expected_revisions: z.record(z.string(), z.number().int().min(0)).describe("Map of entry id to expected revision for all touched entries"),
						mutation_id: z.string().min(1).max(200).describe("Client-generated idempotency key for this mutation"),
						reason: z.string().min(1).max(500).describe("Why this consolidation is valid"),
						updated_view: z.string().min(1).optional().describe("Optional replacement canonical view for the kept knowledge entry"),
						confidence: z.enum(["low", "medium", "high"]).optional().describe("Optional confidence override for the kept knowledge entry"),
						context_type: z.enum(CONTEXT_TYPES).optional().describe("Optional context type override for the kept entry"),
					},
					MUTATING_TOOL_ANNOTATIONS,
					async ({ keep_id, archive_ids, expected_revisions, mutation_id, reason, updated_view, confidence, context_type }) => {
						try {
							const actorId = await this.requireWriteAccess("consolidate_entries");
							const result = await consolidateEntries(this.env, {
								keepId: keep_id,
								archiveIds: archive_ids,
								expectedRevisions: expected_revisions,
								mutationId: mutation_id,
								reason,
								actorId,
								updatedView: updated_view,
								confidence,
								contextType: context_type,
							});
							return {
								content: [{ type: "text", text: JSON.stringify(result) }],
							};
						} catch (error) {
							const errMsg = error instanceof Error ? error.message : String(error);
							return {
								content: [{ type: "text", text: JSON.stringify({ error: errMsg }) }],
							};
						}
					},
				);

				// Tool: add_insight
				this.server.tool(
					"add_insight",
					"Append a structured insight to a knowledge entry and refresh semantic search. Requires mcp:write scope.",
					{
						id: z.string().describe("Knowledge entry ID to update (ke_xxx)"),
						expected_revision: z.number().int().min(0).describe("Revision number last observed by the client"),
						mutation_id: z.string().min(1).max(200).describe("Client-generated idempotency key for this mutation"),
						reason: z.string().min(1).max(500).describe("Why this insight should be stored"),
						insight: z.string().min(1).max(500).describe("Durable insight to append to the entry"),
						source_conversation_id: z.string().min(1).max(200).optional().describe("Optional source conversation identifier"),
						source_message_ids: z.array(z.string().min(1).max(200)).max(20).optional().describe("Optional source message identifiers"),
						evidence_snippet: z.string().min(1).max(1000).optional().describe("Optional evidence snippet from the source conversation"),
					},
					MUTATING_TOOL_ANNOTATIONS,
					async ({
						id,
						expected_revision,
						mutation_id,
						reason,
						insight,
						source_conversation_id,
						source_message_ids,
						evidence_snippet,
					}) => {
						try {
							const actorId = await this.requireWriteAccess("add_insight");
							const result = await addInsight(this.env, {
								entryId: id,
								expectedRevision: expected_revision,
								mutationId: mutation_id,
								reason,
								actorId,
								insight,
								sourceConversationId: source_conversation_id,
								sourceMessageIds: source_message_ids,
								evidenceSnippet: evidence_snippet,
							});
							return {
								content: [{ type: "text", text: JSON.stringify(result) }],
							};
						} catch (error) {
							const errMsg = error instanceof Error ? error.message : String(error);
							return {
								content: [{ type: "text", text: JSON.stringify({ error: errMsg }) }],
							};
						}
					},
				);

				// Tool: update_entry
				this.server.tool(
					"update_entry",
					"Update an existing entry's mutable fields. current_view/confidence/state apply to knowledge entries; context_type applies to knowledge and project entries. Requires mcp:write scope.",
					{
						id: z.string().describe("Entry ID to update (ke_xxx or pe_xxx)"),
						expected_revision: z.number().int().min(0).describe("Revision number last observed by the client"),
						mutation_id: z.string().min(1).max(200).describe("Client-generated idempotency key for this mutation"),
						reason: z.string().min(1).max(500).describe("Why this change is being made"),
						current_view: z.string().min(1).optional().describe("Replacement canonical view text for knowledge entries"),
						confidence: z.enum(["low", "medium", "high"]).optional().describe("Updated confidence level for knowledge entries"),
						state: z.enum(["active", "contested", "stale"]).optional().describe("Updated knowledge-entry state"),
						context_type: z.enum(CONTEXT_TYPES).optional().describe("Updated context type for the entry"),
					},
					MUTATING_TOOL_ANNOTATIONS,
					async ({ id, expected_revision, mutation_id, reason, current_view, confidence, state, context_type }) => {
						try {
							const actorId = await this.requireWriteAccess("update_entry");
							const result = await updateEntry(this.env, {
								entryId: id,
								expectedRevision: expected_revision,
								mutationId: mutation_id,
								reason,
								actorId,
								currentView: current_view,
								confidence,
								state,
								contextType: context_type,
							});
							return {
								content: [{ type: "text", text: JSON.stringify(result) }],
							};
						} catch (error) {
							const errMsg = error instanceof Error ? error.message : String(error);
							return {
								content: [{ type: "text", text: JSON.stringify({ error: errMsg }) }],
							};
						}
					},
				);
		}

		// Tool: get_context
		this.server.tool(
			"get_context",
			"Get the current view and key insights for a topic or project. Use when you need to understand a specific topic quickly.",
			{ topic: z.string().describe("Topic domain or project name to look up") },
			READ_ONLY_TOOL_ANNOTATIONS,
			async ({ topic }) => {
				try {
					const redis = this.getRedis(this.env);
					const vector = this.getVector(this.env);

					let queryEmbedding: number[];
					try {
						queryEmbedding = await this.getEmbedding(this.env, topic);
					} catch (embErr) {
						const msg = embErr instanceof Error ? embErr.message : String(embErr);
						return { content: [{ type: "text", text: JSON.stringify({ error: `Embedding step failed: ${msg}` }) }] };
					}

					let results;
					try {
						results = await vector.query({
							vector: queryEmbedding,
							topK: 5,
							includeMetadata: true,
						});
					} catch (vecErr) {
						const msg = vecErr instanceof Error ? vecErr.message : String(vecErr);
						return { content: [{ type: "text", text: JSON.stringify({ error: `Vector query failed: ${msg}` }) }] };
					}

					const entry = await (async () => {
						for (const result of results) {
							const vectorMetadata = parseStoredObject(result.metadata) ?? {};
							if (vectorMetadata.archived === true) {
								continue;
							}
							const entryType: EntryType =
								vectorMetadata.type === "project" ? "project" : "knowledge";
							const candidate = await this.loadEntry(redis, entryType, String(result.id));
							if (!candidate) {
								continue;
							}
							const candidateMetadata = getEntryMetadata(candidate);
							if (candidateMetadata.archived === true) {
								continue;
							}
							return candidate;
						}
						return null;
					})();

					if (!entry) {
						return { content: [{ type: "text", text: `No active entry found for: ${topic}` }] };
					}

					const entryId = getEntryId(entry);
					const entryType: EntryType = entry.type === "project" ? "project" : "knowledge";
					if (entryId) {
						this.scheduleReconsolidation(entryType, entryId);
					}

					return { content: [{ type: "text", text: JSON.stringify(entry) }] };
				} catch (error) {
					const errMsg = error instanceof Error ? error.message : String(error);
					return { content: [{ type: "text", text: JSON.stringify({ error: `Unexpected: ${errMsg}` }) }] };
				}
			}
		);

		// Tool: get_deep
		this.server.tool(
			"get_deep",
			"Get the full entry including all evidence and evolution history. Use when you need detailed provenance.",
			{ id: z.string().describe("Entry ID (ke_xxx for knowledge, pe_xxx for project)") },
			READ_ONLY_TOOL_ANNOTATIONS,
			async ({ id }) => {
				const redis = this.getRedis(this.env);
				const type: EntryType = id.startsWith("pe_") ? "project" : "knowledge";
				const entry = await this.loadEntry(redis, type, id);
				if (entry) {
					this.scheduleReconsolidation(type, id);
				}
				return { content: [{ type: "text", text: JSON.stringify(entry || { error: "Not found" }) }] };
			}
		);

		// Tool: search
		this.server.tool(
			"search",
			"Tier-aware semantic search across all knowledge and project entries. Archived entries are excluded by default. Results are scored by semantic match, salience, recency, source weight, and retrieval-tier multiplier, then ranked by final score.",
			{
				query: z.string().describe("Search query"),
				limit: z.number().optional().describe("Max results (default 5)"),
				tier_filter: z.union([z.literal(1), z.literal(2), z.literal(3)]).optional()
					.describe("Optional tier filter: 1, 2, or 3"),
			},
			READ_ONLY_TOOL_ANNOTATIONS,
			async ({ query, limit, tier_filter }) => {
				try {
					const redis = this.getRedis(this.env);
					const vector = this.getVector(this.env);
					const queryEmbedding = await this.getEmbedding(this.env, query);

					const requestedLimit = Math.max(1, Math.min(limit || 5, 20));
					const fetchLimit = Math.min(requestedLimit * 8, 60);
					const results = await vector.query({
						vector: queryEmbedding,
						topK: fetchLimit,
						includeMetadata: true,
					});

					// Layer 0: classify query intent once for use across all candidates.
					// The effective mode combines operator intent (env var) with any
					// active anomaly-tripwire kill flag: off wins.
					const retrievalEffective = await getEffectiveMode(
						redis,
						this.env.RETRIEVAL_POLICY_MODE,
						"RETRIEVAL_POLICY_MODE",
					);
					const retrievalPolicyMode: "off" | "on" =
						retrievalEffective.effective === "on" ? "on" : "off";
					const queryIntent: QueryIntent = classifyQueryIntent(query);
					const phase8QueryIntent: Phase8QueryIntent = classifyPhase8Query(query);

					const rankedResults = await Promise.all(results.map(async (result) => {
						const vectorMetadata = parseStoredObject(result.metadata) ?? {};
						const entryType: EntryType =
							vectorMetadata.type === "project" ? "project" : "knowledge";
						const entry = await this.loadEntry(redis, entryType, String(result.id));
						if (!entry) return null;

						const entryMetadata = getEntryMetadata(entry);
						if (entryMetadata.archived === true) {
							return null;
						}

						const salienceScore = computeSalience(entry);
						entryMetadata.salience_score = salienceScore;
						entryMetadata.injection_tier = resolveStoredInjectionTier(entryMetadata);

						const effectiveTier = deriveSearchTier(entry, result.score);
						if (tier_filter && effectiveTier !== tier_filter) {
							return null;
						}

						const updatedAt = getEntryUpdatedAt(entry);
						const recencyScore = calculateRecencyScore(updatedAt);
						const sourceWeight = getSourceWeightFromMetadata({
							...vectorMetadata,
							...entryMetadata,
						});
						const baseScore = computeSearchScore({
							similarity: result.score,
							recency: recencyScore,
							salience: salienceScore,
							tier: effectiveTier,
							sourceWeight,
						});

						// Layer 0 penalties (no-op when RETRIEVAL_POLICY_MODE !== "on").
						const entryBucket: TopicBucket = classifyEntryTopic({
							label: getEntryLabel(entry),
							domain:
								typeof (entry as Record<string, unknown>).domain === "string"
									? ((entry as Record<string, unknown>).domain as string)
									: null,
							currentView:
								typeof (entry as Record<string, unknown>).current_view === "string"
									? ((entry as Record<string, unknown>).current_view as string)
									: null,
							source:
								typeof entryMetadata.source === "string"
									? entryMetadata.source
									: typeof entryMetadata.source_type === "string"
										? entryMetadata.source_type
										: null,
							project:
								typeof entryMetadata.project === "string" ? entryMetadata.project : null,
							githubRepo:
								typeof entryMetadata.github_repo === "string"
									? entryMetadata.github_repo
									: null,
						});
						const crossContextPenalty = computeCrossContextPenalty({
							mode: retrievalPolicyMode,
							queryIntent,
							entryBucket,
						});
						const quarantinePenalty = computeQuarantinePenalty({
							mode: retrievalPolicyMode,
							isQuarantined: Boolean(entryMetadata.injection_quarantine),
						});
						const policyMultiplier = crossContextPenalty * quarantinePenalty;
						const label = getEntryLabel(entry);
						const summary = getEntrySummary(entry);
						const phase8Score = scorePhase8Candidate(query, phase8QueryIntent, {
							entry,
							metadata: entryMetadata,
							label,
							summary,
							entryType,
							vectorScore: result.score,
						});
						const finalScore = Math.round(
							baseScore * policyMultiplier * phase8Score.score_multiplier * 10000,
						) / 10000;

						return {
							id: String(result.id),
							type: entryType,
							label,
							summary,
							top_repo: typeof entryMetadata.github_repo === "string" ? entryMetadata.github_repo : null,
							state: getEntryState(entry),
							context_type: typeof entryMetadata.context_type === "string" ? entryMetadata.context_type : null,
							injection_tier: effectiveTier,
							stored_injection_tier: resolveStoredInjectionTier(entryMetadata),
							salience_score: salienceScore,
							mention_count: typeof entryMetadata.mention_count === "number" ? entryMetadata.mention_count : null,
							access_count: typeof entryMetadata.access_count === "number" ? entryMetadata.access_count : 0,
							last_accessed: typeof entryMetadata.last_accessed === "string" ? entryMetadata.last_accessed : null,
							similarity_score: result.score,
							recency_score: recencyScore,
							source_weight: sourceWeight,
							base_score: baseScore,
							policy_multiplier: policyMultiplier,
							phase8_multiplier: phase8Score.score_multiplier,
							final_score: finalScore,
							topic_bucket: entryBucket,
							phase8_retrieval: phase8Score,
							quarantined: Boolean(entryMetadata.injection_quarantine),
							updated: updatedAt ?? null,
							metadata: {
								classification_status: entryMetadata.classification_status,
								context_type: entryMetadata.context_type,
								injection_tier: effectiveTier,
								salience_score: salienceScore,
								mention_count: entryMetadata.mention_count,
								github_repo: typeof entryMetadata.github_repo === "string" ? entryMetadata.github_repo : null,
								artifact_path: typeof entryMetadata.artifact_path === "string" ? entryMetadata.artifact_path : null,
								archived: false,
								injection_quarantine: Boolean(entryMetadata.injection_quarantine),
							},
						};
					}));

					const filteredResults = rankedResults.filter((result): result is NonNullable<typeof result> => result !== null);
					// 3.1 — Rank by score only; tier is already a multiplier in final_score
					// (search_tier_multipliers: T1=1.15, T2=1.0, T3=0.85) so a hard tier
					// sort is redundant and prevents a high-scoring Tier-3 result from
					// surfacing above a weakly-matched Tier-1 result.
					filteredResults.sort((a, b) => {
						if (a.final_score !== b.final_score) return b.final_score - a.final_score;
						return b.similarity_score - a.similarity_score;
					});
					const topResults = filteredResults.slice(0, requestedLimit);
					for (const result of topResults.slice(0, MAX_RECONSOLIDATION_SEARCH_RESULTS)) {
						const entryType: EntryType = result.type === "project" ? "project" : "knowledge";
						this.scheduleReconsolidation(entryType, result.id);
					}

					// Tripwire bookkeeping: record query + hit status for the
					// retrieval-collapse anomaly check. A "hit" means at least
					// one result was returned to the caller (passed all filters).
					const wasHit = topResults.length > 0;
					await recordSearchQuery(redis, wasHit);

					return {
						content: [{
							type: "text",
							text: JSON.stringify({
								results: topResults,
								query,
								tier_filter: tier_filter ?? null,
								retrieval_policy: {
									mode: retrievalPolicyMode,
									env_mode: retrievalEffective.env_value,
									kill_flag_active: retrievalEffective.tripped,
									kill_flag_record: retrievalEffective.trip_record,
									query_intent: queryIntent.bucket,
									query_confidence: queryIntent.confidence,
									matched_keywords: queryIntent.matchedKeywords,
								},
								phase8_retrieval: {
									intent: phase8QueryIntent.intent,
									temporal_mode: phase8QueryIntent.temporal_mode,
									matched_terms: phase8QueryIntent.matched_terms,
									as_of: phase8QueryIntent.as_of,
								},
								scoring: "ranked by retrieval tier, then a weighted score of semantic similarity, recency, salience, source weight, Phase 8 lexical/entity/vector/temporal/lane/source scoring, and (when RETRIEVAL_POLICY_MODE=on) Layer 0 cross-context + quarantine penalties; archived entries excluded by default"
							})
						}],
					};
				} catch (error) {
					const errMsg = error instanceof Error ? error.message : String(error);
					return { content: [{ type: "text", text: JSON.stringify({ error: errMsg }) }] };
				}
			}
		);

		// Tool: github - Dynamic GitHub repository queries
		this.server.tool(
			"github",
			"Query GitHub repositories dynamically. Fetches LIVE data from both arjun-via and ArjunDivecha accounts. Use to find code, read files, list repos, or get commit history.",
			{
				operation: z.enum(['list_repos', 'search_code', 'get_file', 'get_repo', 'get_commits'])
					.describe("Operation: list_repos, search_code, get_file, get_repo, get_commits"),
				query: z.string().optional().describe("Search query (for search_code)"),
				repo: z.string().optional().describe("Repository name (for get_file, get_repo, get_commits)"),
				path: z.string().optional().describe("File path (for get_file)"),
				language: z.string().optional().describe("Filter by language (for search_code)"),
				limit: z.number().optional().describe("Max results (default 20)"),
			},
			OPEN_WORLD_READ_ONLY_TOOL_ANNOTATIONS,
			async ({ operation, query, repo, path, language, limit }) => {
				const token = this.env.GITHUB_TOKEN;
				if (!token) {
					return { content: [{ type: "text", text: JSON.stringify({ error: "GITHUB_TOKEN not configured" }) }] };
				}

				try {
					switch (operation) {
						case 'list_repos': {
							const allRepos: any[] = [];
							for (const account of GITHUB_ACCOUNTS) {
								let page = 1;
								let hasMore = true;
								while (hasMore) {
									const repos = await githubRequest(
										`/users/${account}/repos`,
										token,
										{ per_page: '100', page: page.toString(), sort: 'updated' }
									);
									if (!repos || repos.length === 0) { hasMore = false; continue; }
									for (const r of repos) {
										const pushedAt = typeof r.pushed_at === "string" ? r.pushed_at : null;
										const repoUpdatedAt = typeof r.updated_at === "string" ? r.updated_at : null;
										const activityAt = pushedAt ?? repoUpdatedAt;
										allRepos.push({
											name: r.name,
											owner: account,
											description: r.description,
											language: r.language,
											stars: r.stargazers_count || 0,
											updated: activityAt,
											pushed_at: pushedAt,
											repo_updated_at: repoUpdatedAt,
											private: r.private || false,
										});
									}
									if (repos.length < 100) hasMore = false;
									else page++;
								}
							}
							allRepos.sort((a, b) => {
								const left = typeof a.updated === "string" ? new Date(a.updated).getTime() : 0;
								const right = typeof b.updated === "string" ? new Date(b.updated).getTime() : 0;
								return right - left;
							});
							return { content: [{ type: "text", text: JSON.stringify({ total: allRepos.length, accounts: GITHUB_ACCOUNTS, repos: allRepos }) }] };
						}

						case 'search_code': {
							if (!query) return { content: [{ type: "text", text: JSON.stringify({ error: "query required for search_code" }) }] };
							const userFilter = GITHUB_ACCOUNTS.map(u => `user:${u}`).join(' ');
							let searchQuery = `${query} ${userFilter}`;
							if (language) searchQuery += ` language:${language}`;
							const result = await githubRequest('/search/code', token, { q: searchQuery, per_page: '30' });
							const results = (result?.items || []).map((item: any) => ({
								repo: item.repository.full_name,
								path: item.path,
								url: item.html_url,
							}));
							return { content: [{ type: "text", text: JSON.stringify({ query, results }) }] };
						}

						case 'get_file': {
							if (!repo || !path) return { content: [{ type: "text", text: JSON.stringify({ error: "repo and path required" }) }] };
							let owner = '';
							let repoName = repo;
							if (repo.includes('/')) {
								owner = repo.split('/')[0];
								repoName = repo.split('/')[1];
							} else {
								for (const account of GITHUB_ACCOUNTS) {
									const r = await githubRequest(`/repos/${account}/${repo}`, token);
									if (r) { owner = account; break; }
								}
							}
							if (!owner) return { content: [{ type: "text", text: JSON.stringify({ error: "Repository not found" }) }] };
							const data = await githubRequest(`/repos/${owner}/${repoName}/contents/${path}`, token);
							if (!data || !data.content) return { content: [{ type: "text", text: JSON.stringify({ error: "File not found" }) }] };
							const content = atob(data.content.replace(/\n/g, ''));
							return { content: [{ type: "text", text: JSON.stringify({ path: data.path, repo: `${owner}/${repoName}`, content }) }] };
						}

						case 'get_repo': {
							if (!repo) return { content: [{ type: "text", text: JSON.stringify({ error: "repo required" }) }] };
							let owner = '';
							let repoName = repo;
							let repoData: any = null;
							if (repo.includes('/')) {
								const [o, r] = repo.split('/');
								repoData = await githubRequest(`/repos/${o}/${r}`, token);
								if (repoData) { owner = o; repoName = r; }
							} else {
								for (const account of GITHUB_ACCOUNTS) {
									repoData = await githubRequest(`/repos/${account}/${repo}`, token);
									if (repoData) { owner = account; break; }
								}
							}
							if (!repoData) return { content: [{ type: "text", text: JSON.stringify({ error: "Repository not found" }) }] };

							let readme: string | null = null;
							const readmeData = await githubRequest(`/repos/${owner}/${repoName}/readme`, token);
							if (readmeData?.content) {
								readme = atob(readmeData.content.replace(/\n/g, ''));
							}

							const treeData = await githubRequest(`/repos/${owner}/${repoName}/git/trees/${repoData.default_branch}`, token);
							const files = treeData?.tree?.filter((f: any) => f.type === 'blob')?.slice(0, 30)?.map((f: any) => f.path) || [];

							return {
								content: [{
									type: "text", text: JSON.stringify({
										info: {
											name: repoData.name,
											full_name: repoData.full_name,
											description: repoData.description,
											language: repoData.language,
											stars: repoData.stargazers_count,
											updated: repoData.updated_at,
										},
										readme: readme ? readme.substring(0, 5000) : null,
										files,
									})
								}]
							};
						}

						case 'get_commits': {
							if (!repo) return { content: [{ type: "text", text: JSON.stringify({ error: "repo required" }) }] };
							let owner = '';
							let repoName = repo;
							if (repo.includes('/')) {
								owner = repo.split('/')[0];
								repoName = repo.split('/')[1];
							} else {
								for (const account of GITHUB_ACCOUNTS) {
									const r = await githubRequest(`/repos/${account}/${repo}`, token);
									if (r) { owner = account; break; }
								}
							}
							if (!owner) return { content: [{ type: "text", text: JSON.stringify({ error: "Repository not found" }) }] };
							const commits = await githubRequest(`/repos/${owner}/${repoName}/commits`, token, { per_page: (limit || 20).toString() });
							const result = (commits || []).map((c: any) => ({
								sha: c.sha?.slice(0, 7),
								message: c.commit?.message,
								date: c.commit?.author?.date,
								author: c.commit?.author?.name,
							}));
							return { content: [{ type: "text", text: JSON.stringify({ repo: `${owner}/${repoName}`, commits: result }) }] };
						}

						default:
							return { content: [{ type: "text", text: JSON.stringify({ error: `Unknown operation: ${operation}` }) }] };
					}
				} catch (error) {
					const errMsg = error instanceof Error ? error.message : String(error);
					return { content: [{ type: "text", text: JSON.stringify({ error: errMsg }) }] };
				}
			}
		);
	}
}

export class OpenAIKnowledgeMCP extends KnowledgeMCP {
	protected includeWriteTools(): boolean {
		return false;
	}
}

// Default handler for non-API routes - must be an object with fetch method
const defaultHandler = {
	async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
		const url = new URL(request.url);
		const baseUrl = getBaseUrl(url);

		if (url.pathname === "/.well-known/oauth-protected-resource" || url.pathname.startsWith("/.well-known/oauth-protected-resource/")) {
			const suffix = url.pathname === "/.well-known/oauth-protected-resource"
				? ""
				: url.pathname.slice("/.well-known/oauth-protected-resource".length);
			const isOpenAIResource = isOpenAIResourcePath(suffix);
			const resource = suffix ? `${baseUrl}${suffix}` : baseUrl;
			return Response.json({
				resource,
				authorization_servers: [baseUrl],
				scopes_supported: isOpenAIResource ? ["mcp:read"] : ["mcp:read", "mcp:write"],
				bearer_methods_supported: ["header"],
			});
		}

		// Handle OAuth authorization - auto-approve for personal single-user system
		if (url.pathname === "/authorize") {
			try {
				const normalizedAuthorizeRequest = rewriteAuthorizeRequestResource(request);
				// Parse the OAuth authorization request
				const authRequest = await env.OAUTH_PROVIDER.parseAuthRequest(normalizedAuthorizeRequest);

				if (!authRequest.clientId) {
					return new Response("Missing client_id", { status: 400 });
				}

				const requestedScopes = normalizeScopes(authRequest.scope);
				const requestedResource = new URL(request.url).searchParams.get("resource");
				const requestsOpenAIResource =
					typeof requestedResource === "string" &&
					requestedResource.startsWith(`${baseUrl}${OPENAI_ROUTE_PREFIX}`);
				const approvedScopes = getApprovedAuthorizationScopes(
					requestedScopes,
					!requestsOpenAIResource,
				);
				if (approvedScopes.length === 0) {
					return new Response("No supported scopes requested", { status: 403 });
				}
				const approvedScopeString = approvedScopes.join(" ");
				const { redirectTo } = await env.OAUTH_PROVIDER.completeAuthorization({
					request: authRequest,
					userId: "arjun",
					metadata: {
						label: "Personal Knowledge MCP"
					},
					scope: approvedScopes,
					props: {
						userId: "arjun",
						scope: approvedScopeString,
						scopes: approvedScopes,
					},
				});

				return Response.redirect(redirectTo, 302);
			} catch (error) {
				const msg = error instanceof Error ? error.message : String(error);
				return new Response(`Authorization error: ${msg}`, { status: 500 });
			}
		}

		if (url.pathname === "/health" || url.pathname === "/status") {
			try {
				return Response.json(await buildHealthPayload(env), {
					headers: { "Content-Type": "application/json" },
				});
			} catch (error) {
				const msg = error instanceof Error ? error.message : String(error);
				return Response.json({ status: "error", error: msg }, { status: 500 });
			}
		}

		if (url.pathname === "/ops/dream/run" && request.method === "POST") {
			if (!isAuthorizedOperatorRequest(request, env)) {
				return Response.json({ error: "Unauthorized" }, { status: 401 });
			}

			try {
				const redis = createRedisClient(env);
				const rateLimit = await applyFixedWindowRateLimit(
					redis,
					"operator",
					"ops_dream_run",
					OPERATOR_WRITE_RATE_LIMIT,
				);
				if (!rateLimit.allowed) {
					return Response.json(
						{
							error: `Rate limit exceeded for Dream operator runs. Allowed ${rateLimit.limit} calls per ${RATE_LIMIT_WINDOW_SECONDS} seconds.`,
						},
						{ status: 429 },
					);
				}

				const body = await request.json();
				const parsed = z.object({
					dry_run: z.boolean().default(true),
					candidate_ids: z.array(z.string().min(1)).max(MAX_OPERATOR_DREAM_ARCHIVE_LIMIT).optional(),
					archive_limit: z.number().int().positive().max(MAX_OPERATOR_DREAM_ARCHIVE_LIMIT).optional(),
					promotion_limit: z.number().int().positive().max(MAX_OPERATOR_DREAM_ARCHIVE_LIMIT).optional(),
					set_as_latest: z.boolean().default(false),
					note: z.string().max(500).optional(),
				}).parse(body);

				if (!parsed.dry_run && (!parsed.candidate_ids || parsed.candidate_ids.length === 0)) {
					return Response.json(
						{ error: "Non-dry-run operator Dream calls require candidate_ids." },
						{ status: 400 },
					);
				}

				const archiveLimit =
					parsed.archive_limit ??
					(parsed.candidate_ids && parsed.candidate_ids.length > 0
						? parsed.candidate_ids.length
						: undefined);

				const result = await runDreamCycle(env, {
					dryRun: parsed.dry_run,
					trigger: "manual",
					candidateIds: parsed.candidate_ids ?? null,
					archiveLimit: archiveLimit ?? null,
					promotionLimit: parsed.promotion_limit ?? null,
					setAsLatest: parsed.set_as_latest,
					note: parsed.note ?? "Operator-triggered Dream test run",
				});

				return Response.json(result, { headers: { "Content-Type": "application/json" } });
			} catch (error) {
				const msg = error instanceof Error ? error.message : String(error);
				return Response.json({ error: msg }, { status: 500 });
			}
		}

		if (url.pathname === "/ops/dream/proposal" && request.method === "POST") {
			if (!isAuthorizedOperatorRequest(request, env)) {
				return Response.json({ error: "Unauthorized" }, { status: 401 });
			}

			try {
				const redis = createRedisClient(env);
				const rateLimit = await applyFixedWindowRateLimit(
					redis,
					"operator",
					"ops_dream_proposal",
					OPERATOR_WRITE_RATE_LIMIT,
				);
				if (!rateLimit.allowed) {
					return Response.json(
						{
							error: `Rate limit exceeded for Dream operator proposals. Allowed ${rateLimit.limit} calls per ${RATE_LIMIT_WINDOW_SECONDS} seconds.`,
						},
						{ status: 429 },
					);
				}

				const body = await request.json();
				const parsed = z.object({
					candidate_ids: z.array(z.string().min(1)).max(200).optional(),
					archive_limit: z.number().int().min(0).max(MAX_OPERATOR_DREAM_ARCHIVE_LIMIT).optional(),
					promotion_limit: z.number().int().min(0).max(MAX_OPERATOR_DREAM_ARCHIVE_LIMIT).optional(),
					note: z.string().max(500).optional(),
					scheduled_equivalent: z.boolean().default(false),
				}).parse(body);

				const result = await runDreamProposal(env, {
					trigger: "manual",
					actorId: parsed.scheduled_equivalent ? "scheduled:dream-governance" : "operator",
					candidateIds: parsed.candidate_ids ?? null,
					archiveLimit: parsed.scheduled_equivalent
						? SCHEDULED_DREAM_ARCHIVE_LIMIT
						: parsed.archive_limit ?? null,
					promotionLimit: parsed.scheduled_equivalent
						? SCHEDULED_DREAM_PROMOTION_LIMIT
						: parsed.promotion_limit ?? null,
					note: parsed.note ??
						(parsed.scheduled_equivalent
							? "Operator-triggered scheduled-equivalent Dream governance proposal."
							: "Operator-triggered Dream governance proposal."),
				});

				return Response.json(result, { headers: { "Content-Type": "application/json" } });
			} catch (error) {
				const msg = error instanceof Error ? error.message : String(error);
				return Response.json({ error: msg }, { status: 500 });
			}
		}

		if (url.pathname === "/ops/dream/run_scheduled_governed" && request.method === "POST") {
			if (!isAuthorizedOperatorRequest(request, env)) {
				return Response.json({ error: "Unauthorized" }, { status: 401 });
			}

			try {
				const redis = createRedisClient(env);
				const rateLimit = await applyFixedWindowRateLimit(
					redis,
					"operator",
					"ops_dream_run_scheduled_governed",
					OPERATOR_WRITE_RATE_LIMIT,
				);
				if (!rateLimit.allowed) {
					return Response.json(
						{
							error: `Rate limit exceeded for scheduled-governed Dream repairs. Allowed ${rateLimit.limit} calls per ${RATE_LIMIT_WINDOW_SECONDS} seconds.`,
						},
						{ status: 429 },
					);
				}

				const body = await request.json();
				const parsed = z.object({
					cron: z.string().min(1).max(100).default("operator-repair"),
					scheduled_time: z.number().int().positive().optional(),
				}).parse(body);

				const controller = {
					cron: parsed.cron,
					scheduledTime: parsed.scheduled_time ?? Date.now(),
					noRetry: () => undefined,
				} as ScheduledController;
				const result = await runScheduledGovernedDream(env, controller);

				return Response.json(result, { headers: { "Content-Type": "application/json" } });
			} catch (error) {
				const msg = error instanceof Error ? error.message : String(error);
				return Response.json({ error: msg }, { status: 500 });
			}
		}

		if (url.pathname === "/ops/dream/grade" && request.method === "POST") {
			if (!isAuthorizedOperatorRequest(request, env)) {
				return Response.json({ error: "Unauthorized" }, { status: 401 });
			}

			try {
				const redis = createRedisClient(env);
				const rateLimit = await applyFixedWindowRateLimit(
					redis,
					"operator",
					"ops_dream_grade",
					OPERATOR_WRITE_RATE_LIMIT,
				);
				if (!rateLimit.allowed) {
					return Response.json(
						{
							error: `Rate limit exceeded for Dream operator grade calls. Allowed ${rateLimit.limit} calls per ${RATE_LIMIT_WINDOW_SECONDS} seconds.`,
						},
						{ status: 429 },
					);
				}

				const body = await request.json();
				const parsed = z.object({
					proposal_id: z.string().min(1).max(200),
					rubric_version: z.string().min(1).max(100).optional(),
				}).parse(body);

				const result = await gradeDreamProposal(env, {
					proposalId: parsed.proposal_id,
					actorId: "operator",
					rubricVersion: parsed.rubric_version,
				});
				return Response.json(result, { headers: { "Content-Type": "application/json" } });
			} catch (error) {
				const msg = error instanceof Error ? error.message : String(error);
				return Response.json({ error: msg }, { status: 500 });
			}
		}

		if (url.pathname === "/ops/dream/apply" && request.method === "POST") {
			if (!isAuthorizedOperatorRequest(request, env)) {
				return Response.json({ error: "Unauthorized" }, { status: 401 });
			}

			try {
				const redis = createRedisClient(env);
				const rateLimit = await applyFixedWindowRateLimit(
					redis,
					"operator",
					"ops_dream_apply",
					OPERATOR_WRITE_RATE_LIMIT,
				);
				if (!rateLimit.allowed) {
					return Response.json(
						{
							error: `Rate limit exceeded for Dream operator apply calls. Allowed ${rateLimit.limit} calls per ${RATE_LIMIT_WINDOW_SECONDS} seconds.`,
						},
						{ status: 429 },
					);
				}

				const body = await request.json();
				const parsed = z.object({
					proposal_id: z.string().min(1).max(200),
					mutation_id: z.string().min(1).max(200),
					reason: z.string().min(1).max(500),
					operation_ids: z.array(z.string().min(1).max(300)).max(100).optional(),
					require_grade_pass: z.boolean().optional(),
					grade_id: z.string().min(1).max(200).optional(),
					phase9_outcome_gate: z.boolean().optional(),
					phase9_auto_rollback: z.boolean().optional(),
					phase9_probe_set_key: z.string().min(1).max(300).optional(),
					phase9_write_validation_ledger: z.boolean().optional(),
				}).parse(body);

				const result = await applyDreamProposal(env, {
					proposalId: parsed.proposal_id,
					mutationId: parsed.mutation_id,
					reason: parsed.reason,
					actorId: "operator",
					operationIds: parsed.operation_ids ?? null,
					requireGradePass: parsed.require_grade_pass,
					gradeId: parsed.grade_id,
					phase9OutcomeGate: parsed.phase9_outcome_gate,
					phase9AutoRollback: parsed.phase9_auto_rollback,
					phase9ProbeSetKey: parsed.phase9_probe_set_key,
					phase9WriteValidationLedger: parsed.phase9_write_validation_ledger,
				});
				return Response.json(result, { headers: { "Content-Type": "application/json" } });
			} catch (error) {
				const msg = error instanceof Error ? error.message : String(error);
				return Response.json({ error: msg }, { status: 500 });
			}
		}

		if (url.pathname === "/ops/dream/restore" && request.method === "POST") {
			if (!isAuthorizedOperatorRequest(request, env)) {
				return Response.json({ error: "Unauthorized" }, { status: 401 });
			}

			try {
				const redis = createRedisClient(env);
				const rateLimit = await applyFixedWindowRateLimit(
					redis,
					"operator",
					"ops_dream_restore",
					OPERATOR_WRITE_RATE_LIMIT,
				);
				if (!rateLimit.allowed) {
					return Response.json(
						{
							error: `Rate limit exceeded for Dream operator restores. Allowed ${rateLimit.limit} calls per ${RATE_LIMIT_WINDOW_SECONDS} seconds.`,
						},
						{ status: 429 },
					);
				}

				const body = await request.json();
				const parsed = z.object({
					entry_id: z.string().min(1),
					reason: z.string().min(1).max(500),
				}).parse(body);

				const result = await restoreArchivedEntry(env, parsed.entry_id, parsed.reason);
				return Response.json(result, { headers: { "Content-Type": "application/json" } });
			} catch (error) {
				const msg = error instanceof Error ? error.message : String(error);
				return Response.json({ error: msg }, { status: 500 });
			}
		}

		// ─────────────────────────────────────────────────────────────────
		// Anomaly tripwires (Dream + forgetting design)
		// ─────────────────────────────────────────────────────────────────

		// GET /ops/dream/tripwire_status — inspect kill flags and the recent
		// destructive + retrieval signals. Operator-token authenticated.
		if (url.pathname === "/ops/dream/tripwire_status" && request.method === "GET") {
			if (!isAuthorizedOperatorRequest(request, env)) {
				return Response.json({ error: "Unauthorized" }, { status: 401 });
			}
			try {
				const redis = createRedisClient(env);
				const [destructiveCheck, retrievalCheck, autoApplyMode, retrievalPolicyMode] = await Promise.all([
					checkDestructiveTripwire(redis),
					checkRetrievalTripwire(redis),
					getEffectiveMode(redis, env.DREAM_AUTO_APPLY_MODE, "DREAM_AUTO_APPLY_MODE"),
					getEffectiveMode(redis, env.RETRIEVAL_POLICY_MODE, "RETRIEVAL_POLICY_MODE"),
				]);
				return Response.json({
					modes: {
						DREAM_AUTO_APPLY_MODE: autoApplyMode,
						RETRIEVAL_POLICY_MODE: retrievalPolicyMode,
					},
					tripwires: {
						destructive_action_volume: destructiveCheck,
						retrieval_hit_collapse: retrievalCheck,
					},
				}, { headers: { "Content-Type": "application/json" } });
			} catch (error) {
				const msg = error instanceof Error ? error.message : String(error);
				return Response.json({ error: msg }, { status: 500 });
			}
		}

		// ─────────────────────────────────────────────────────────────────
		// Judge queue (Mac-side judge script consumes these)
		// ─────────────────────────────────────────────────────────────────

		// GET /ops/dream/judge_queue — list pending judge items with payloads.
		if (url.pathname === "/ops/dream/judge_queue" && request.method === "GET") {
			if (!isAuthorizedOperatorRequest(request, env)) {
				return Response.json({ error: "Unauthorized" }, { status: 401 });
			}
			try {
				const redis = createRedisClient(env);
				const { listPendingOpIds, getJudgeItem, getJudgeVerdict } = await import("./judgeQueue");
				const opIds = await listPendingOpIds(redis, 200);
				const items = await Promise.all(
					opIds.map(async (opId) => ({
						op_id: opId,
						item: await getJudgeItem(redis, opId),
						verdict: await getJudgeVerdict(redis, opId),
					})),
				);
				return Response.json({
					pending_count: opIds.length,
					items,
				}, { headers: { "Content-Type": "application/json" } });
			} catch (error) {
				const msg = error instanceof Error ? error.message : String(error);
				return Response.json({ error: msg }, { status: 500 });
			}
		}

		// POST /ops/dream/judge_verdict — Mac script posts a verdict.
		// Body: { op_id, verdict: "apply" | "skip", reason, judge_model, judge_source }
		if (url.pathname === "/ops/dream/judge_verdict" && request.method === "POST") {
			if (!isAuthorizedOperatorRequest(request, env)) {
				return Response.json({ error: "Unauthorized" }, { status: 401 });
			}
			try {
				const body = await request.json();
				const parsed = z.object({
					op_id: z.string().min(1),
					verdict: z.enum(["apply", "skip"]),
					reason: z.string().min(1).max(2000),
					judge_model: z.string().min(1).max(100),
					judge_source: z.enum(["claude_cli", "anthropic_api"]),
				}).parse(body);
				const redis = createRedisClient(env);
				const { judgeVerdictKey } = await import("./judgeQueue");
				const record = {
					...parsed,
					judged_at: new Date().toISOString(),
				};
				await redis.set(judgeVerdictKey(parsed.op_id), JSON.stringify(record));
				return Response.json({
					ok: true,
					op_id: parsed.op_id,
					accepted_at: record.judged_at,
				}, { headers: { "Content-Type": "application/json" } });
			} catch (error) {
				const msg = error instanceof Error ? error.message : String(error);
				return Response.json({ error: msg }, { status: 500 });
			}
		}

		// POST /ops/dream/clear_kill_flag — clear an active kill flag so the
		// affected mode can resume operating per its env-var setting.
		// Body: { mode: "DREAM_AUTO_APPLY_MODE" | "RETRIEVAL_POLICY_MODE" }
		if (url.pathname === "/ops/dream/clear_kill_flag" && request.method === "POST") {
			if (!isAuthorizedOperatorRequest(request, env)) {
				return Response.json({ error: "Unauthorized" }, { status: 401 });
			}
			try {
				const body = await request.json();
				const parsed = z.object({
					mode: z.enum(["DREAM_AUTO_APPLY_MODE", "RETRIEVAL_POLICY_MODE"]),
					reason: z.string().min(1).max(500).optional(),
				}).parse(body);
				const redis = createRedisClient(env);
				const { clearKillFlag } = await import("./tripwires");
				await clearKillFlag(redis, parsed.mode);
				return Response.json({
					ok: true,
					cleared: parsed.mode,
					cleared_at: new Date().toISOString(),
					reason: parsed.reason ?? null,
				}, { headers: { "Content-Type": "application/json" } });
			} catch (error) {
				const msg = error instanceof Error ? error.message : String(error);
				return Response.json({ error: msg }, { status: 500 });
			}
		}

		// OAuth discovery endpoints for MCP clients and OIDC-style probes used by some connector UIs.
		if (AUTHORIZATION_SERVER_METADATA_PATHS.has(url.pathname)) {
			return new Response(JSON.stringify(buildAuthorizationServerMetadata(baseUrl)), {
				headers: { "Content-Type": "application/json" }
			});
		}

		// Home page
		return new Response(`
			<html>
				<head><title>Personal Knowledge MCP</title></head>
				<body style="font-family: system-ui; padding: 2rem; max-width: 600px; margin: 0 auto;">
					<h1>Personal Knowledge MCP Server</h1>
					<p>This is Arjun's personal knowledge system with OAuth support.</p>
					<h2>Endpoints</h2>
					<ul>
						<li><code>/sse</code> - Full MCP over SSE (Claude / full tool surface)</li>
						<li><code>/mcp</code> - Full MCP over HTTP</li>
						<li><code>/openai/sse</code> - Read-only MCP over SSE for Codex / ChatGPT</li>
						<li><code>/openai/mcp</code> - Read-only MCP over HTTP for Codex / ChatGPT</li>
						<li><code>/authorize</code> - OAuth authorization</li>
						<li><code>/token</code> - OAuth token endpoint</li>
						<li><code>/register</code> - Dynamic client registration</li>
						<li><code>/health</code> - Rollout and migration status</li>
					</ul>
				</body>
			</html>
		`, {
			headers: { "Content-Type": "text/html" }
		});
	}
};

// Export OAuth-wrapped handler for iOS Claude compatibility
const oauthProvider = new OAuthProvider({
	apiHandlers: {
		"/mcp": KnowledgeMCP.serve("/mcp", { binding: "MCP_OBJECT" }) as any,
		"/sse": KnowledgeMCP.serveSSE("/sse", { binding: "MCP_OBJECT" }) as any,
		"/openai/mcp": OpenAIKnowledgeMCP.serve("/openai/mcp", { binding: "OPENAI_MCP_OBJECT" }) as any,
		"/openai/sse": OpenAIKnowledgeMCP.serveSSE("/openai/sse", { binding: "OPENAI_MCP_OBJECT" }) as any,
	},
	defaultHandler: defaultHandler as any,
	authorizeEndpoint: "/authorize",
	tokenEndpoint: "/token",
	clientRegistrationEndpoint: "/register",
	scopesSupported: ["mcp:read", "mcp:write"],
});

export default {
	async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
		const url = new URL(request.url);
		const baseUrl = getBaseUrl(url);
		if (AUTHORIZATION_SERVER_METADATA_PATHS.has(url.pathname)) {
			return withCors(
				request,
				Response.json(buildAuthorizationServerMetadata(baseUrl)),
			);
		}
		const protectedResourceConfig = getProtectedResourceConfig(url.pathname);
		if (protectedResourceConfig) {
			return withCors(
				request,
				Response.json(buildProtectedResourceMetadata(baseUrl, protectedResourceConfig)),
			);
		}
		if (request.method === "OPTIONS") {
			return createCorsPreflightResponse(request);
		}
		if (
			request.method === "HEAD" &&
			AUTHLESS_PROBE_PATHS.has(url.pathname) &&
			!request.headers.has("authorization")
		) {
			return createHeadProbeResponse(request, baseUrl, url.pathname);
		}

		let normalizedRequest = request;
		if (url.pathname === "/token") {
			normalizedRequest = await rewriteTokenRequestResource(request);
		}
		let response = await oauthProvider.fetch(normalizedRequest, env, ctx);
		if (url.pathname === "/register") {
			response = await normalizeClientRegistrationResponse(response);
		}
		if (
			response.status === 401 &&
			AUTHLESS_PROBE_PATHS.has(url.pathname) &&
			!request.headers.has("authorization")
		) {
			return withUnauthorizedMcpChallenge(request, response, baseUrl, url.pathname);
		}
		return withCors(request, response);
	},
	async scheduled(controller: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
		// =========================================================================
		// 1. Anomaly tripwires (monitoring only; no Dream off switch).
		// =========================================================================
		const tripwireRedis = createRedisForTripwires(env);
		if (tripwireRedis) {
			try {
				const destructive = await checkDestructiveTripwire(tripwireRedis);
				if (destructive.tripped && destructive.reason) {
					console.warn(
						`[tripwire] destructive spike observed; scheduled Dream remains governed: ${destructive.reason}`,
						JSON.stringify(destructive.day_counts),
					);
				} else {
					const staleDreamFlag = await readKillFlag(tripwireRedis, "DREAM_AUTO_APPLY_MODE");
					if (staleDreamFlag?.source_tripwire === "destructive_spike") {
						await clearKillFlag(tripwireRedis, "DREAM_AUTO_APPLY_MODE");
						console.warn("[tripwire] cleared stale DREAM_AUTO_APPLY_MODE destructive-spike kill flag; scheduled Dream remains governed.");
					}
				}
				const retrieval = await checkRetrievalTripwire(tripwireRedis);
				if (retrieval.tripped && retrieval.reason) {
					await setKillFlag(tripwireRedis, "RETRIEVAL_POLICY_MODE", {
						tripped_at: new Date().toISOString(),
						reason: retrieval.reason,
						source_tripwire: "retrieval_collapse",
					});
					console.warn(
						`[tripwire] RETRIEVAL_POLICY_MODE auto-flipped off: ${retrieval.reason}`,
						JSON.stringify(retrieval.day_ratios),
					);
				}
			} catch (e) {
				// Tripwire failures should never block the cycle.
				console.error("[tripwire] check failed", e);
			}
		}

		// =========================================================================
		// 2. Governed cycle dispatch.
		// =========================================================================
		// Scheduled Dream has no env-var off switch. The cron always runs the
		// cautious autonomous path: proposal -> grade -> bounded apply.
		const promise = runScheduledGovernedDream(env, controller);
		ctx.waitUntil(promise);
		const result = await promise;
		if (result.status === "skipped_no_backfill" || result.status === "skipped_locked") {
			controller.noRetry();
		}
	},
};

// Lazy redis client for tripwire bookkeeping. Returns null on missing creds
// so tests that don't set Upstash env vars still run.
function createRedisForTripwires(env: Env): Redis | null {
	if (!env.UPSTASH_REDIS_REST_URL || !env.UPSTASH_REDIS_REST_TOKEN) return null;
	return new Redis({
		url: env.UPSTASH_REDIS_REST_URL,
		token: env.UPSTASH_REDIS_REST_TOKEN,
		enableAutoPipelining: false,
	});
}
