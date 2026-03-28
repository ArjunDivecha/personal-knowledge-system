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
import { restoreArchivedEntry, runDreamCycle, setEntryContextType } from "./dream";
import { formatConsolidationNote } from "./consolidation";

// GitHub accounts to query
const GITHUB_ACCOUNTS = ['arjun-via', 'ArjunDivecha'];
const MEMORY_SCHEMA_VERSION = 2;
const MAX_RECONSOLIDATION_SEARCH_RESULTS = 5;
const MAX_RECONSOLIDATION_ERROR_LOGS = 100;
const RECONSOLIDATION_PROMOTION_THRESHOLD = 3;
const MAX_OPERATOR_DREAM_ARCHIVE_LIMIT = 10;
const RATE_LIMIT_WINDOW_SECONDS = 60 * 60;
const WRITE_TOOL_RATE_LIMIT = 24;
const OPERATOR_WRITE_RATE_LIMIT = 12;
const NIGHTLY_DREAM_ARCHIVE_LIMIT = 5;
const NIGHTLY_DREAM_PROMOTION_LIMIT = 10;
const CONTEXT_TYPES = [
	"professional_identity",
	"stated_preference",
	"explicit_save",
	"active_project",
	"recurring_pattern",
	"task_query",
	"passing_reference",
] as const;

type EntryType = "knowledge" | "project";
type AuthProps = {
	userId?: string;
	scope?: string;
	scopes?: string[];
};

function createRedisClient(env: Env): Redis {
	return new Redis({
		url: env.UPSTASH_REDIS_REST_URL,
		token: env.UPSTASH_REDIS_REST_TOKEN,
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

// Define our MCP agent with knowledge tools
export class KnowledgeMCP extends McpAgent<Env, unknown, AuthProps> {
	server = new McpServer({
		name: "Personal Knowledge System",
		version: "1.0.0",
	});

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
			async () => {
				const redis = this.getRedis(this.env);
				const rawIndex = await redis.get("index:current") as {
					topics?: Array<{ id: string; domain: string; current_view_summary?: string; state?: string; confidence?: string; last_updated?: string; context_type?: string; injection_tier?: number; salience_score?: number; mention_count?: number; archived?: boolean }>;
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

		// Tool: restore_archived
		this.server.tool(
			"restore_archived",
			"Restore an archived entry back into active memory. Requires mcp:write scope.",
			{
				id: z.string().describe("Entry ID to restore (ke_xxx or pe_xxx)"),
				reason: z.string().min(1).max(500).describe("Why this archived entry should be restored"),
			},
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

		// Tool: set_context_type
		this.server.tool(
			"set_context_type",
			"Override an active entry's context type. Requires mcp:write scope.",
			{
				id: z.string().describe("Entry ID to update (ke_xxx or pe_xxx)"),
				context_type: z.enum(CONTEXT_TYPES).describe("Replacement context type"),
				reason: z.string().min(1).max(500).describe("Why this override is needed"),
			},
			async ({ id, context_type, reason }) => {
				try {
					await this.requireWriteAccess("set_context_type");
					const result = await setEntryContextType(this.env, id, context_type, reason);
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

		// Tool: get_context
		this.server.tool(
			"get_context",
			"Get the current view and key insights for a topic or project. Use when you need to understand a specific topic quickly.",
			{ topic: z.string().describe("Topic domain or project name to look up") },
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
			"Tier-aware semantic search across all knowledge and project entries. Archived entries are excluded by default, and results are reranked by semantic match, salience, recency, and retrieval tier.",
			{
				query: z.string().describe("Search query"),
				limit: z.number().optional().describe("Max results (default 5)"),
				tier_filter: z.union([z.literal(1), z.literal(2), z.literal(3)]).optional()
					.describe("Optional tier filter: 1, 2, or 3"),
			},
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
						const finalScore = computeSearchScore({
							similarity: result.score,
							recency: recencyScore,
							salience: salienceScore,
							tier: effectiveTier,
							sourceWeight,
						});

						return {
							id: String(result.id),
							type: entryType,
							label: getEntryLabel(entry),
							summary: getEntrySummary(entry),
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
							final_score: finalScore,
							updated: updatedAt ?? null,
							metadata: {
								classification_status: entryMetadata.classification_status,
								context_type: entryMetadata.context_type,
								injection_tier: effectiveTier,
								salience_score: salienceScore,
								mention_count: entryMetadata.mention_count,
								archived: false,
							},
						};
					}));

					const filteredResults = rankedResults.filter((result): result is NonNullable<typeof result> => result !== null);
					filteredResults.sort((a, b) => {
						if (a.injection_tier !== b.injection_tier) return a.injection_tier - b.injection_tier;
						if (a.final_score !== b.final_score) return b.final_score - a.final_score;
						return b.similarity_score - a.similarity_score;
					});
					const topResults = filteredResults.slice(0, requestedLimit);
					for (const result of topResults.slice(0, MAX_RECONSOLIDATION_SEARCH_RESULTS)) {
						const entryType: EntryType = result.type === "project" ? "project" : "knowledge";
						this.scheduleReconsolidation(entryType, result.id);
					}

					return {
						content: [{
							type: "text",
							text: JSON.stringify({
								results: topResults,
								query,
								tier_filter: tier_filter ?? null,
								scoring: "ranked by retrieval tier, then a weighted score of semantic similarity, recency, salience, and source weight; archived entries excluded by default"
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
										allRepos.push({
											name: r.name,
											owner: account,
											description: r.description,
											language: r.language,
											stars: r.stargazers_count || 0,
											updated: r.updated_at,
											private: r.private || false,
										});
									}
									if (repos.length < 100) hasMore = false;
									else page++;
								}
							}
							allRepos.sort((a, b) => new Date(b.updated).getTime() - new Date(a.updated).getTime());
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

// Default handler for non-API routes - must be an object with fetch method
const defaultHandler = {
	async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
		const url = new URL(request.url);

		// Handle OAuth authorization - auto-approve for personal single-user system
		if (url.pathname === "/authorize") {
			try {
				// Parse the OAuth authorization request
				const authRequest = await env.OAUTH_PROVIDER.parseAuthRequest(request);

				if (!authRequest.clientId) {
					return new Response("Missing client_id", { status: 400 });
				}

				// Auto-approve: complete authorization immediately without login
				// This is safe for a personal single-user system
				const approvedScopes = normalizeScopes(authRequest.scope);
				const { redirectTo } = await env.OAUTH_PROVIDER.completeAuthorization({
					request: authRequest,
					userId: "arjun",
					metadata: {
						label: "Personal Knowledge MCP"
					},
					scope: authRequest.scope,
					props: {
						userId: "arjun",
						scope: authRequest.scope,
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

		// OAuth discovery endpoint for iOS Claude
		if (url.pathname === "/.well-known/oauth-authorization-server") {
			const baseUrl = `${url.protocol}//${url.host}`;
			return new Response(JSON.stringify({
				issuer: baseUrl,
				authorization_endpoint: `${baseUrl}/authorize`,
				token_endpoint: `${baseUrl}/token`,
				registration_endpoint: `${baseUrl}/register`,
				scopes_supported: ["mcp:read", "mcp:write"],
				response_types_supported: ["code"],
				grant_types_supported: ["authorization_code", "refresh_token"],
				token_endpoint_auth_methods_supported: ["client_secret_post", "client_secret_basic"],
			}), {
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
						<li><code>/sse</code> - MCP over SSE (for Claude)</li>
						<li><code>/mcp</code> - MCP over HTTP</li>
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
	},
	defaultHandler: defaultHandler as any,
	authorizeEndpoint: "/authorize",
	tokenEndpoint: "/token",
	clientRegistrationEndpoint: "/register",
	scopesSupported: ["mcp:read", "mcp:write"],
});

export default {
	fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
		return oauthProvider.fetch(request, env, ctx);
	},
	async scheduled(controller: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
		const promise = runDreamCycle(env, {
			dryRun: false,
			trigger: "scheduled",
			cron: controller.cron,
			scheduledTime: controller.scheduledTime,
			archiveLimit: NIGHTLY_DREAM_ARCHIVE_LIMIT,
			promotionLimit: NIGHTLY_DREAM_PROMOTION_LIMIT,
			note: `Nightly bounded Dream run with archiveLimit=${NIGHTLY_DREAM_ARCHIVE_LIMIT} and promotionLimit=${NIGHTLY_DREAM_PROMOTION_LIMIT}.`,
		});
		ctx.waitUntil(promise);
		const result = await promise;
		if (result.status === "skipped_no_backfill" || result.status === "skipped_locked") {
			controller.noRetry();
		}
	},
};
