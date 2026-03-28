import { Redis } from "@upstash/redis/cloudflare";
import { Index } from "@upstash/vector";
import {
	computeSalience,
	defaultInjectionTier,
	MEMORY_POLICY,
	resolveStoredInjectionTier,
} from "./salience";
import { formatConsolidationNote } from "./consolidation";

type EntryType = "knowledge" | "project";
type DreamStatus = "completed" | "skipped_no_backfill" | "skipped_locked" | "failed";
type DreamBucket = "stable" | "active" | "weak" | "decay_candidate";

interface RunDreamOptions {
	dryRun: boolean;
	trigger: "scheduled" | "manual" | "local_test";
	cron?: string | null;
	scheduledTime?: number | null;
	note?: string | null;
	candidateIds?: string[] | null;
	archiveLimit?: number | null;
	promotionLimit?: number | null;
	setAsLatest?: boolean;
}

interface LoadedEntry {
	id: string;
	type: EntryType;
	entry: Record<string, unknown>;
	metadata: Record<string, unknown>;
	label: string;
	updatedAt: string | null;
	contextType: string;
	injectionTier: 1 | 2 | 3;
	mentionCount: number;
	accessCount: number;
	sourceConversationCount: number;
	salienceScore: number;
}

interface ArchivedSnapshot {
	schema_version: 1;
	entry_id: string;
	entry_type: EntryType;
	run_id: string;
	archived_at: string;
	archive_reason: string;
	snapshot: Record<string, unknown>;
}

const DREAM_LOCK_KEY = "dream:lock";
const DREAM_LAST_RUN_KEY = "dream:last_run";
const DREAM_LAST_ATTEMPT_KEY = "dream:last_attempt";
const DREAM_RUN_PREFIX = "dream:run:";
const ARCHIVED_PREFIX = "archived";
const DREAM_LOCK_TTL_SECONDS = 30 * 60;
const DREAM_SAMPLE_LIMIT = 25;
const DREAM_SCAN_COUNT = 200;
const INDEX_REBUILD_LOCK_KEY = "index:rebuild:lock";
const INDEX_REBUILD_LOCK_TTL_SECONDS = 5 * 60;
const THIN_INDEX_STAGING_PREFIX = "index:staging:";
const THIN_INDEX_TOPIC_LIMIT = 100;
const THIN_INDEX_PROJECT_LIMIT = 50;

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

function toOptionalNumber(value: unknown): number | null {
	if (typeof value === "number" && Number.isFinite(value)) return value;
	if (typeof value === "string" && value.trim().length > 0) {
		const parsed = Number(value);
		return Number.isFinite(parsed) ? parsed : null;
	}
	return null;
}

function toOptionalInteger(value: unknown): number | null {
	const parsed = toOptionalNumber(value);
	return parsed === null ? null : Math.trunc(parsed);
}

function toStringArray(value: unknown): string[] {
	if (!Array.isArray(value)) return [];
	return value.filter((item): item is string => typeof item === "string");
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

function getEntryAccessKey(entryId: string): string {
	return `entry_access:${entryId}`;
}

function getEntryLastAccessedKey(entryId: string): string {
	return `entry_last_accessed:${entryId}`;
}

function getEntryKey(entryType: EntryType, entryId: string): string {
	return `${entryType}:${entryId}`;
}

function getArchivedSnapshotKey(entryType: EntryType, entryId: string, runId: string): string {
	return `${ARCHIVED_PREFIX}:${entryType}:${entryId}:${runId}`;
}

function getArchivedLatestKey(entryType: EntryType, entryId: string): string {
	return `${ARCHIVED_PREFIX}:${entryType}:${entryId}:latest`;
}

function getEntryLabel(entry: Record<string, unknown>): string {
	if (typeof entry.domain === "string" && entry.domain.length > 0) return entry.domain;
	if (typeof entry.name === "string" && entry.name.length > 0) return entry.name;
	return String(entry.id ?? "unknown");
}

function getEntryUpdatedAt(entry: Record<string, unknown>, metadata: Record<string, unknown>): string | null {
	return (
		(typeof metadata.last_seen === "string" && metadata.last_seen) ||
		(typeof metadata.updated_at === "string" && metadata.updated_at) ||
		(typeof metadata.last_touched === "string" && metadata.last_touched) ||
		null
	);
}

function normalizeEntry(raw: unknown, entryType: EntryType): Record<string, unknown> | null {
	const entry = parseStoredObject(raw);
	if (!entry) return null;

	const metadata = parseStoredObject(entry.metadata) ?? {};
	const normalizedMetadata: Record<string, unknown> = {
		...metadata,
		source_conversations: toStringArray(metadata.source_conversations),
		source_messages: toStringArray(metadata.source_messages),
		access_count: toOptionalInteger(metadata.access_count) ?? 0,
		last_accessed: typeof metadata.last_accessed === "string" ? metadata.last_accessed : null,
		context_type:
			typeof metadata.context_type === "string" && metadata.context_type.length > 0
				? metadata.context_type
				: "task_query",
		mention_count: toOptionalInteger(metadata.mention_count) ?? 1,
		archived: Boolean(metadata.archived),
		last_consolidated:
			typeof metadata.last_consolidated === "string" ? metadata.last_consolidated : null,
		consolidation_notes: toStringArray(metadata.consolidation_notes),
	};

	return {
		...entry,
		type: entryType,
		metadata: normalizedMetadata,
	};
}

function overlayAccessSignals(
	entry: Record<string, unknown>,
	accessCountRaw: unknown,
	lastAccessedRaw: unknown,
): Record<string, unknown> {
	const metadata = (entry.metadata as Record<string, unknown> | undefined) ?? {};
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
	entry.metadata = metadata;
	return entry;
}

function appendConsolidationNote(metadata: Record<string, unknown>, note: string): void {
	const notes = toStringArray(metadata.consolidation_notes);
	if (notes[notes.length - 1] !== note) {
		notes.push(note);
	}
	metadata.consolidation_notes = notes.slice(-20);
}

function setVectorMetadataBase(entry: LoadedEntry): Record<string, unknown> {
	return {
		type: entry.type,
		archived: Boolean(entry.metadata.archived),
		context_type: entry.metadata.context_type,
		injection_tier: entry.metadata.injection_tier,
		salience_score: entry.metadata.salience_score,
		mention_count: entry.metadata.mention_count,
		last_consolidated: entry.metadata.last_consolidated,
	};
}

function truncate(value: unknown, maxLength: number): string {
	const text = typeof value === "string" ? value : "";
	if (text.length <= maxLength) {
		return text;
	}
	return `${text.slice(0, maxLength - 3)}...`;
}

function sortTimestamp(value: string | null): number {
	if (!value) return Number.NEGATIVE_INFINITY;
	const parsed = new Date(value).getTime();
	return Number.isNaN(parsed) ? Number.NEGATIVE_INFINITY : parsed;
}

function getTopicState(entry: Record<string, unknown>): "active" | "contested" | "stale" {
	const rawState = typeof entry.state === "string" ? entry.state : "active";
	if (rawState === "contested" || rawState === "stale") {
		return rawState;
	}
	return "active";
}

function getConfidence(entry: Record<string, unknown>): "high" | "medium" | "low" {
	const rawConfidence = typeof entry.confidence === "string" ? entry.confidence : "medium";
	if (rawConfidence === "high" || rawConfidence === "low") {
		return rawConfidence;
	}
	return "medium";
}

function getRepoName(rawRepo: unknown): string | null {
	if (!rawRepo || typeof rawRepo !== "object" || Array.isArray(rawRepo)) {
		return null;
	}
	const repo = (rawRepo as Record<string, unknown>).repo;
	return typeof repo === "string" && repo.length > 0 ? repo : null;
}

function getRelatedRepos(entry: Record<string, unknown>): Record<string, unknown>[] {
	const relatedRepos = entry.related_repos;
	if (!Array.isArray(relatedRepos)) {
		return [];
	}
	return relatedRepos.filter(
		(repo): repo is Record<string, unknown> =>
			Boolean(repo) && typeof repo === "object" && !Array.isArray(repo),
	);
}

async function scanKeys(redis: Redis, match: string): Promise<string[]> {
	let cursor = "0";
	const keys: string[] = [];

	do {
		const [nextCursor, batch] = await redis.scan(cursor, { match, count: DREAM_SCAN_COUNT });
		keys.push(...batch);
		cursor = nextCursor;
	} while (cursor !== "0");

	return keys;
}

async function loadEntryBatchByType(
	redis: Redis,
	entryType: EntryType,
): Promise<{ entries: LoadedEntry[]; archivedCount: number }> {
	const keys = await scanKeys(redis, `${entryType}:*`);
	if (keys.length === 0) return { entries: [], archivedCount: 0 };

	const rawEntries = await redis.mget<unknown[]>(keys);
	const normalizedEntries = rawEntries
		.map((rawEntry) => normalizeEntry(rawEntry, entryType))
		.filter((entry): entry is Record<string, unknown> => entry !== null);
	const ids = normalizedEntries
		.map((entry) => (typeof entry.id === "string" ? entry.id : null))
		.filter((entryId): entryId is string => entryId !== null);

	const [accessCounts, lastAccessedValues] = await Promise.all([
		ids.length > 0 ? redis.mget<unknown[]>(ids.map(getEntryAccessKey)) : Promise.resolve([]),
		ids.length > 0 ? redis.mget<unknown[]>(ids.map(getEntryLastAccessedKey)) : Promise.resolve([]),
	]);

	const loadedEntries: LoadedEntry[] = [];
	let archivedCount = 0;
	for (let index = 0; index < normalizedEntries.length; index += 1) {
		const entry = normalizedEntries[index];
		const entryId = typeof entry.id === "string" ? entry.id : null;
		if (!entryId) continue;

		overlayAccessSignals(entry, accessCounts[index], lastAccessedValues[index]);
		const metadata = (entry.metadata as Record<string, unknown> | undefined) ?? {};
		if (metadata.archived === true) {
			archivedCount += 1;
			continue;
		}

		const contextType =
			typeof metadata.context_type === "string" ? metadata.context_type : "task_query";
		const mentionCount = Math.max(1, toOptionalInteger(metadata.mention_count) ?? 1);
		const accessCount = Math.max(0, toOptionalInteger(metadata.access_count) ?? 0);
		const salienceScore = computeSalience(entry);
		metadata.salience_score = salienceScore;
		metadata.injection_tier = resolveStoredInjectionTier(metadata);

		loadedEntries.push({
			id: entryId,
			type: entryType,
			entry,
			metadata,
			label: getEntryLabel(entry),
			updatedAt: getEntryUpdatedAt(entry, metadata),
			contextType,
			injectionTier: resolveStoredInjectionTier(metadata),
			mentionCount,
			accessCount,
			sourceConversationCount: toStringArray(metadata.source_conversations).length,
			salienceScore,
		});
	}

	return { entries: loadedEntries, archivedCount };
}

async function loadEntriesByType(redis: Redis, entryType: EntryType): Promise<LoadedEntry[]> {
	return (await loadEntryBatchByType(redis, entryType)).entries;
}

function isArchiveCandidate(entry: LoadedEntry): boolean {
	return (
		(entry.contextType === "task_query" || entry.contextType === "passing_reference") &&
		entry.mentionCount === 1 &&
		entry.accessCount === 0 &&
		entry.salienceScore < MEMORY_POLICY.dream_thresholds.archive_candidate_salience
	);
}

function isPromotionCandidate(entry: LoadedEntry): boolean {
	return (
		entry.contextType === "task_query" &&
		entry.mentionCount >= MEMORY_POLICY.dream_thresholds.promote_candidate_min_mentions &&
		(entry.accessCount > 0 || entry.sourceConversationCount > 1)
	);
}

function compareArchivePriority(left: LoadedEntry, right: LoadedEntry): number {
	if (left.salienceScore !== right.salienceScore) {
		return left.salienceScore - right.salienceScore;
	}
	const updatedDiff = sortTimestamp(left.updatedAt) - sortTimestamp(right.updatedAt);
	if (updatedDiff !== 0) {
		return updatedDiff;
	}
	return left.id.localeCompare(right.id);
}

function comparePromotionPriority(left: LoadedEntry, right: LoadedEntry): number {
	if (left.mentionCount !== right.mentionCount) {
		return right.mentionCount - left.mentionCount;
	}
	if (left.accessCount !== right.accessCount) {
		return right.accessCount - left.accessCount;
	}
	if (left.salienceScore !== right.salienceScore) {
		return right.salienceScore - left.salienceScore;
	}
	const updatedDiff = sortTimestamp(right.updatedAt) - sortTimestamp(left.updatedAt);
	if (updatedDiff !== 0) {
		return updatedDiff;
	}
	return left.id.localeCompare(right.id);
}

function classifyBucket(entry: LoadedEntry): DreamBucket {
	const immortal =
		MEMORY_POLICY.half_lives_days[
			entry.contextType as keyof typeof MEMORY_POLICY.half_lives_days
		] === "infinity";

	if (immortal || entry.injectionTier === 1 || entry.salienceScore >= 0.35) {
		return "stable";
	}
	if (entry.salienceScore >= MEMORY_POLICY.dream_thresholds.decay_candidate_salience) {
		return "active";
	}
	if (entry.salienceScore >= MEMORY_POLICY.dream_thresholds.archive_candidate_salience) {
		return "weak";
	}
	return "decay_candidate";
}

function summarizeArchiveCandidates(entries: LoadedEntry[]): Array<Record<string, unknown>> {
	return [...entries]
		.sort((left, right) => {
			if (left.salienceScore !== right.salienceScore) {
				return left.salienceScore - right.salienceScore;
			}
			const leftUpdated = left.updatedAt ? new Date(left.updatedAt).getTime() : 0;
			const rightUpdated = right.updatedAt ? new Date(right.updatedAt).getTime() : 0;
			return leftUpdated - rightUpdated;
		})
		.slice(0, DREAM_SAMPLE_LIMIT)
		.map((entry) => ({
			id: entry.id,
			type: entry.type,
			label: entry.label,
			context_type: entry.contextType,
			injection_tier: entry.injectionTier,
			salience_score: entry.salienceScore,
			mention_count: entry.mentionCount,
			access_count: entry.accessCount,
			updated_at: entry.updatedAt,
			reason: "salience below archive threshold with single mention and no retrieval reinforcement",
		}));
}

async function writeRunRecord(
	redis: Redis,
	runRecord: Record<string, unknown>,
	setAsLatest: boolean,
): Promise<void> {
	const runId = String(runRecord.run_id);
	await redis.set(`${DREAM_RUN_PREFIX}${runId}`, JSON.stringify(runRecord));
	await redis.set(DREAM_LAST_ATTEMPT_KEY, JSON.stringify(runRecord));
	if (setAsLatest) {
		await redis.set(DREAM_LAST_RUN_KEY, JSON.stringify(runRecord));
	}
}

function buildBaseRunRecord(
	runId: string,
	options: RunDreamOptions,
	startedAt: string,
): Record<string, unknown> {
	return {
		schema_version: 1,
		run_id: runId,
		run_at: startedAt,
		completed_at: null,
		status: "running",
		dry_run: options.dryRun,
		trigger: options.trigger,
		cron: options.cron ?? null,
		scheduled_time:
			typeof options.scheduledTime === "number"
				? new Date(options.scheduledTime).toISOString()
				: null,
		note: options.note ?? null,
		phases: {},
		counts: {},
		archive_candidates: [],
		next_action: null,
	};
}

async function acquireIndexRebuildLock(redis: Redis, runId: string): Promise<boolean> {
	const lockPayload = JSON.stringify({
		run_id: runId,
		acquired_at: new Date().toISOString(),
	});
	const result = await redis.set(INDEX_REBUILD_LOCK_KEY, lockPayload, {
		nx: true,
		ex: INDEX_REBUILD_LOCK_TTL_SECONDS,
	});
	return Boolean(result);
}

async function releaseIndexRebuildLock(redis: Redis, runId: string): Promise<void> {
	const currentLock = parseStoredObject(await redis.get(INDEX_REBUILD_LOCK_KEY));
	if (currentLock?.run_id === runId) {
		await redis.del(INDEX_REBUILD_LOCK_KEY);
	}
}

async function rebuildThinIndexWithHeldLock(
	redis: Redis,
	runId: string,
): Promise<Record<string, unknown>> {
	const [knowledgeBatch, projectBatch] = await Promise.all([
		loadEntryBatchByType(redis, "knowledge"),
		loadEntryBatchByType(redis, "project"),
	]);
	const generatedAt = new Date().toISOString();
	let contestedCount = 0;
	const rankedTopics = knowledgeBatch.entries
		.filter((entry) => entry.entry.state !== "deprecated")
		.map((entry) => {
			const topicState = getTopicState(entry.entry);
			if (topicState === "contested") {
				contestedCount += 1;
			}
			return entry;
		})
		.sort((left, right) => {
			if (left.injectionTier !== right.injectionTier) {
				return left.injectionTier - right.injectionTier;
			}
			if (left.salienceScore !== right.salienceScore) {
				return right.salienceScore - left.salienceScore;
			}
			return sortTimestamp(right.updatedAt) - sortTimestamp(left.updatedAt);
		});
	const rankedProjects = [...projectBatch.entries].sort((left, right) => {
		if (left.injectionTier !== right.injectionTier) {
			return left.injectionTier - right.injectionTier;
		}
		if (left.salienceScore !== right.salienceScore) {
			return right.salienceScore - left.salienceScore;
		}
		return sortTimestamp(right.updatedAt) - sortTimestamp(left.updatedAt);
	});

	const thinIndex = {
		generated_at: generatedAt,
		token_count: 0,
		topics: rankedTopics.slice(0, THIN_INDEX_TOPIC_LIMIT).map((entry) => ({
			id: entry.id,
			domain:
				typeof entry.entry.domain === "string" && entry.entry.domain.length > 0
					? entry.entry.domain
					: entry.label,
			current_view_summary: truncate(entry.entry.current_view, 80),
			state: getTopicState(entry.entry),
			confidence: getConfidence(entry.entry),
			last_updated: entry.updatedAt ?? generatedAt,
			top_repo: getRepoName(getRelatedRepos(entry.entry)[0]),
			context_type: entry.contextType,
			injection_tier: entry.injectionTier,
			salience_score: entry.salienceScore,
			mention_count: entry.mentionCount,
			archived: false,
		})),
		projects: rankedProjects.slice(0, THIN_INDEX_PROJECT_LIMIT).map((entry) => {
			const primaryRepo =
				getRelatedRepos(entry.entry).find(
					(repo) => (repo.is_primary === true),
				) ?? getRelatedRepos(entry.entry)[0];
			return {
				id: entry.id,
				name:
					typeof entry.entry.name === "string" && entry.entry.name.length > 0
						? entry.entry.name
						: entry.label,
				status:
					typeof entry.entry.status === "string" && entry.entry.status.length > 0
						? entry.entry.status
						: "active",
				goal_summary: truncate(entry.entry.goal, 80),
				current_phase:
					typeof entry.entry.current_phase === "string" ? entry.entry.current_phase : "",
				blocked_on:
					typeof entry.entry.blocked_on === "string" ? entry.entry.blocked_on : null,
				last_touched: entry.updatedAt ?? generatedAt,
				primary_repo: getRepoName(primaryRepo),
				context_type: entry.contextType,
				injection_tier: entry.injectionTier,
				salience_score: entry.salienceScore,
				mention_count: entry.mentionCount,
				archived: false,
			};
		}),
		recent_evolutions: [],
		contested_count: contestedCount,
		total_topic_count: rankedTopics.length,
		total_project_count: rankedProjects.length,
		tier_1_count: rankedTopics.filter((entry) => entry.injectionTier === 1).length +
			rankedProjects.filter((entry) => entry.injectionTier === 1).length,
		tier_2_count: rankedTopics.filter((entry) => entry.injectionTier === 2).length +
			rankedProjects.filter((entry) => entry.injectionTier === 2).length,
		tier_3_count: rankedTopics.filter((entry) => entry.injectionTier === 3).length +
			rankedProjects.filter((entry) => entry.injectionTier === 3).length,
		archived_count: knowledgeBatch.archivedCount + projectBatch.archivedCount,
	};
	thinIndex.token_count = Math.round(JSON.stringify(thinIndex).length / 4);

	const stagingKey = `${THIN_INDEX_STAGING_PREFIX}${runId}`;
	await redis.set(stagingKey, JSON.stringify(thinIndex));
	await redis.rename(stagingKey, "index:current");
	return thinIndex;
}

async function rebuildThinIndexSafely(redis: Redis, runId: string): Promise<Record<string, unknown>> {
	if (!(await acquireIndexRebuildLock(redis, runId))) {
		throw new Error("index_rebuild_lock_held");
	}

	try {
		return await rebuildThinIndexWithHeldLock(redis, runId);
	} finally {
		await releaseIndexRebuildLock(redis, runId);
	}
}

async function persistEntry(
	redis: Redis,
	vector: Index,
	entry: LoadedEntry,
): Promise<void> {
	entry.metadata.salience_score = computeSalience(entry.entry);
	entry.entry.metadata = entry.metadata;
	await redis.set(getEntryKey(entry.type, entry.id), JSON.stringify(entry.entry));
	await vector.update({
		id: entry.id,
		metadata: setVectorMetadataBase(entry),
		metadataUpdateMode: "PATCH",
	});
}

async function promoteEntry(
	redis: Redis,
	vector: Index,
	entry: LoadedEntry,
	runId: string,
	timestamp: string,
): Promise<Record<string, unknown>> {
	entry.metadata.context_type = "recurring_pattern";
	entry.metadata.injection_tier = 2;
	entry.metadata.last_consolidated = timestamp;
	appendConsolidationNote(
		entry.metadata,
		formatConsolidationNote({
			timestamp,
			source: "dream",
			action: "promote_context_type",
			detail: `task_query -> recurring_pattern (run ${runId})`,
		}),
	);
	entry.contextType = "recurring_pattern";
	entry.injectionTier = 2;
	entry.salienceScore = computeSalience(entry.entry);
	entry.metadata.salience_score = entry.salienceScore;
	entry.entry.metadata = entry.metadata;
	await persistEntry(redis, vector, entry);

	return {
		id: entry.id,
		type: entry.type,
		label: entry.label,
		context_type: entry.contextType,
		injection_tier: entry.injectionTier,
		salience_score: entry.salienceScore,
	};
}

async function archiveEntry(
	redis: Redis,
	vector: Index,
	entry: LoadedEntry,
	runId: string,
	timestamp: string,
	reason: string,
): Promise<Record<string, unknown>> {
	const activeKey = getEntryKey(entry.type, entry.id);
	const latestEntry = normalizeEntry(await redis.get(activeKey), entry.type) ?? entry.entry;
	const [accessCountRaw, lastAccessedRaw] = await Promise.all([
		redis.get(getEntryAccessKey(entry.id)),
		redis.get(getEntryLastAccessedKey(entry.id)),
	]);
	overlayAccessSignals(latestEntry, accessCountRaw, lastAccessedRaw);
	const latestMetadata = (latestEntry.metadata as Record<string, unknown> | undefined) ?? {};

	const archiveSnapshotKey = getArchivedSnapshotKey(entry.type, entry.id, runId);
	const archivedSnapshot: ArchivedSnapshot = {
		schema_version: 1,
		entry_id: entry.id,
		entry_type: entry.type,
		run_id: runId,
		archived_at: timestamp,
		archive_reason: reason,
		snapshot: JSON.parse(JSON.stringify(latestEntry)),
	};

	await redis.set(archiveSnapshotKey, JSON.stringify(archivedSnapshot));
	await redis.set(
		getArchivedLatestKey(entry.type, entry.id),
		JSON.stringify({
			entry_id: entry.id,
			entry_type: entry.type,
			run_id: runId,
			archived_at: timestamp,
			snapshot_key: archiveSnapshotKey,
		}),
	);

	latestMetadata.archived = true;
	latestMetadata.archived_at = timestamp;
	latestMetadata.archived_reason = reason;
	latestMetadata.archived_run_id = runId;
	latestMetadata.archive_snapshot_key = archiveSnapshotKey;
	latestMetadata.last_consolidated = timestamp;
	appendConsolidationNote(
		latestMetadata,
		formatConsolidationNote({
			timestamp,
			source: "dream",
			action: "archive_entry",
			detail: `${reason} (run ${runId})`,
		}),
	);
	latestEntry.metadata = latestMetadata;

	const archivedEntry: LoadedEntry = {
		...entry,
		entry: latestEntry,
		metadata: latestMetadata,
		contextType:
			typeof latestMetadata.context_type === "string" ? latestMetadata.context_type : entry.contextType,
		injectionTier: resolveStoredInjectionTier(latestMetadata),
		salienceScore: computeSalience(latestEntry),
	};
	archivedEntry.metadata.salience_score = archivedEntry.salienceScore;
	await persistEntry(redis, vector, archivedEntry);

	return {
		id: entry.id,
		type: entry.type,
		label: entry.label,
		snapshot_key: archiveSnapshotKey,
		archived_at: timestamp,
		reason,
	};
}

export async function restoreArchivedEntry(
	env: Env,
	entryId: string,
	reason: string,
): Promise<Record<string, unknown>> {
	const redis = createRedisClient(env);
	const vector = createVectorClient(env);
	const entryType: EntryType = entryId.startsWith("pe_") ? "project" : "knowledge";
	const latestPointer = parseStoredObject(await redis.get(getArchivedLatestKey(entryType, entryId)));
	if (!latestPointer?.snapshot_key || typeof latestPointer.snapshot_key !== "string") {
		throw new Error(`No archived snapshot found for ${entryId}`);
	}

	const archivedSnapshot = parseStoredObject(await redis.get(latestPointer.snapshot_key));
	const snapshotEntry = parseStoredObject(archivedSnapshot?.snapshot);
	if (!snapshotEntry) {
		throw new Error(`Archived snapshot is missing entry data for ${entryId}`);
	}

	const restoredEntry = normalizeEntry(snapshotEntry, entryType);
	if (!restoredEntry) {
		throw new Error(`Unable to restore archived entry ${entryId}`);
	}

	const timestamp = new Date().toISOString();
	const metadata = (restoredEntry.metadata as Record<string, unknown> | undefined) ?? {};
	metadata.archived = false;
	metadata.context_type = "explicit_save";
	metadata.injection_tier = 1;
	metadata.last_consolidated = timestamp;
	metadata.restored_at = timestamp;
	metadata.restored_reason = reason;
	appendConsolidationNote(
		metadata,
		formatConsolidationNote({
			timestamp,
			source: "operator",
			action: "restore_archived",
			detail: `restored as explicit_save (${reason})`,
		}),
	);
	delete metadata.archived_at;
	delete metadata.archived_reason;
	delete metadata.archived_run_id;
	delete metadata.archive_snapshot_key;
	restoredEntry.metadata = metadata;

	const restoredLoadedEntry: LoadedEntry = {
		id: entryId,
		type: entryType,
		entry: restoredEntry,
		metadata,
		label: getEntryLabel(restoredEntry),
		updatedAt: getEntryUpdatedAt(restoredEntry, metadata),
		contextType: "explicit_save",
		injectionTier: 1,
		mentionCount: Math.max(1, toOptionalInteger(metadata.mention_count) ?? 1),
		accessCount: Math.max(0, toOptionalInteger(metadata.access_count) ?? 0),
		sourceConversationCount: toStringArray(metadata.source_conversations).length,
		salienceScore: computeSalience(restoredEntry),
	};
	restoredLoadedEntry.metadata.salience_score = restoredLoadedEntry.salienceScore;

	const rebuildRunId = `restore_${entryId}_${timestamp.replace(/[:.]/g, "-")}`;
	if (!(await acquireIndexRebuildLock(redis, rebuildRunId))) {
		throw new Error("index_rebuild_lock_held");
	}

	try {
		await persistEntry(redis, vector, restoredLoadedEntry);
		await rebuildThinIndexWithHeldLock(redis, rebuildRunId);
	} finally {
		await releaseIndexRebuildLock(redis, rebuildRunId);
	}

	return {
		id: entryId,
		type: entryType,
		context_type: restoredLoadedEntry.contextType,
		injection_tier: restoredLoadedEntry.injectionTier,
		snapshot_key: latestPointer.snapshot_key,
		restored_at: timestamp,
	};
}

export async function setEntryContextType(
	env: Env,
	entryId: string,
	contextType: string,
	reason: string,
): Promise<Record<string, unknown>> {
	const redis = createRedisClient(env);
	const vector = createVectorClient(env);
	const entryType: EntryType = entryId.startsWith("pe_") ? "project" : "knowledge";
	const rawEntry = await redis.get(getEntryKey(entryType, entryId));
	const entry = normalizeEntry(rawEntry, entryType);

	if (!entry) {
		throw new Error(`Entry not found: ${entryId}`);
	}

	const metadata = (entry.metadata as Record<string, unknown> | undefined) ?? {};
	if (metadata.archived === true) {
		throw new Error(`Entry ${entryId} is archived. Restore it before changing context type.`);
	}

	const previousContextType =
		typeof metadata.context_type === "string" ? metadata.context_type : "task_query";
	const timestamp = new Date().toISOString();
	metadata.context_type = contextType;
	metadata.classification_status = "manual_override";
	metadata.auto_inferred = false;
	metadata.injection_tier = defaultInjectionTier(contextType);
	metadata.last_consolidated = timestamp;
	appendConsolidationNote(
		metadata,
		formatConsolidationNote({
			timestamp,
			source: "operator",
			action: "set_context_type",
			detail: `${previousContextType} -> ${contextType} (${reason})`,
		}),
	);
	entry.metadata = metadata;

	const loadedEntry: LoadedEntry = {
		id: entryId,
		type: entryType,
		entry,
		metadata,
		label: getEntryLabel(entry),
		updatedAt: getEntryUpdatedAt(entry, metadata),
		contextType,
		injectionTier: defaultInjectionTier(contextType),
		mentionCount: Math.max(1, toOptionalInteger(metadata.mention_count) ?? 1),
		accessCount: Math.max(0, toOptionalInteger(metadata.access_count) ?? 0),
		sourceConversationCount: toStringArray(metadata.source_conversations).length,
		salienceScore: computeSalience(entry),
	};
	loadedEntry.metadata.salience_score = loadedEntry.salienceScore;

	const rebuildRunId = `set_context_${entryId}_${timestamp.replace(/[:.]/g, "-")}`;
	if (!(await acquireIndexRebuildLock(redis, rebuildRunId))) {
		throw new Error("index_rebuild_lock_held");
	}

	try {
		await persistEntry(redis, vector, loadedEntry);
		await rebuildThinIndexWithHeldLock(redis, rebuildRunId);
	} finally {
		await releaseIndexRebuildLock(redis, rebuildRunId);
	}

	return {
		id: entryId,
		type: entryType,
		previous_context_type: previousContextType,
		context_type: contextType,
		injection_tier: loadedEntry.injectionTier,
		salience_score: loadedEntry.salienceScore,
		updated_at: timestamp,
	};
}

export async function runDreamCycle(
	env: Env,
	options: RunDreamOptions,
): Promise<Record<string, unknown>> {
	const redis = createRedisClient(env);
	const vector = createVectorClient(env);
	const startedAt = new Date(
		typeof options.scheduledTime === "number" ? options.scheduledTime : Date.now(),
	).toISOString();
	const runId = `dr_${startedAt.replace(/[:.]/g, "-")}`;
	const baseRunRecord = buildBaseRunRecord(runId, options, startedAt);
	const setAsLatest = options.setAsLatest ?? true;

	const migrationBackfillComplete = await redis.get("migration:backfill_complete");
	if (!migrationBackfillComplete) {
		const skippedRecord = {
			...baseRunRecord,
			status: "skipped_no_backfill",
			completed_at: new Date().toISOString(),
			next_action: "Backfill must complete before Dream can run.",
			phases: {
				survey: { status: "skipped", reason: "migration_backfill_incomplete" },
				replay: { status: "skipped", reason: "migration_backfill_incomplete" },
				consolidate: { status: "skipped", reason: "migration_backfill_incomplete" },
				prune: { status: "skipped", reason: "migration_backfill_incomplete" },
			},
		};
		await writeRunRecord(redis, skippedRecord, setAsLatest);
		return skippedRecord;
	}

	const lockPayload = JSON.stringify({
		run_id: runId,
		run_at: startedAt,
		trigger: options.trigger,
		dry_run: options.dryRun,
	});
	const lockResult = await redis.set(DREAM_LOCK_KEY, lockPayload, {
		nx: true,
		ex: DREAM_LOCK_TTL_SECONDS,
	});
	if (!lockResult) {
		const skippedRecord = {
			...baseRunRecord,
			status: "skipped_locked",
			completed_at: new Date().toISOString(),
			next_action: "Wait for the active Dream run to finish before starting another.",
			phases: {
				survey: { status: "skipped", reason: "dream_lock_held" },
				replay: { status: "skipped", reason: "dream_lock_held" },
				consolidate: { status: "skipped", reason: "dream_lock_held" },
				prune: { status: "skipped", reason: "dream_lock_held" },
			},
		};
		await writeRunRecord(redis, skippedRecord, false);
		return skippedRecord;
	}

	try {
		const [knowledgeBatch, projectBatch] = await Promise.all([
			loadEntryBatchByType(redis, "knowledge"),
			loadEntryBatchByType(redis, "project"),
		]);
		const knowledgeEntries = knowledgeBatch.entries;
		const projectEntries = projectBatch.entries;
		const allEntries = [...knowledgeEntries, ...projectEntries];
		const candidateIdFilter =
			options.candidateIds && options.candidateIds.length > 0
				? new Set(options.candidateIds)
				: null;
		const promotionCandidates = allEntries.filter((entry) => {
			if (candidateIdFilter && !candidateIdFilter.has(entry.id)) return false;
			return isPromotionCandidate(entry);
		}).sort(comparePromotionPriority);
		const archiveCandidates = allEntries.filter((entry) => {
			if (candidateIdFilter && !candidateIdFilter.has(entry.id)) return false;
			return isArchiveCandidate(entry);
		}).sort(compareArchivePriority);
		const bucketCounts: Record<DreamBucket, number> = {
			stable: 0,
			active: 0,
			weak: 0,
			decay_candidate: 0,
		};

		for (const entry of allEntries) {
			bucketCounts[classifyBucket(entry)] += 1;
		}

		const promotionCandidatesLimited =
			typeof options.promotionLimit === "number" && options.promotionLimit >= 0
				? promotionCandidates.slice(0, options.promotionLimit)
				: promotionCandidates;
		const promotedEntries: Array<Record<string, unknown>> = [];
		if (!options.dryRun) {
			for (const entry of promotionCandidatesLimited) {
				promotedEntries.push(await promoteEntry(redis, vector, entry, runId, startedAt));
			}
		}

		const archiveReason =
			"salience below archive threshold with single mention and no retrieval reinforcement";
		const archiveCandidatesLimited =
			typeof options.archiveLimit === "number" && options.archiveLimit >= 0
				? archiveCandidates.slice(0, options.archiveLimit)
				: archiveCandidates;
		const archivedEntries: Array<Record<string, unknown>> = [];
		if (!options.dryRun) {
			for (const entry of archiveCandidatesLimited) {
				archivedEntries.push(
					await archiveEntry(redis, vector, entry, runId, startedAt, archiveReason),
				);
			}
			await rebuildThinIndexSafely(redis, runId);
		}

		const completedAt = new Date().toISOString();
		const runRecord = {
			...baseRunRecord,
			status: "completed" as DreamStatus,
			completed_at: completedAt,
			phases: {
				survey: {
					status: "completed",
					knowledge_entries: knowledgeEntries.length,
					project_entries: projectEntries.length,
					buckets: bucketCounts,
				},
				replay: {
					status: options.dryRun ? "dry_run" : "completed",
					promotion_candidate_count: promotionCandidates.length,
					promoted_count: promotedEntries.length,
					promoted_entries: promotedEntries,
					deferred_items: [
						"duplicate merge detection",
						"contradiction detection",
						"temporal reference cleanup",
					],
				},
				consolidate: {
					status: options.dryRun ? "dry_run" : "completed",
					promoted_count: promotedEntries.length,
				},
				prune: {
					status: options.dryRun ? "dry_run" : "completed",
					archive_candidate_count: archiveCandidates.length,
					archive_limit: options.archiveLimit ?? null,
					archived_count: archivedEntries.length,
				},
			},
			counts: {
				total_entries: allEntries.length,
				knowledge_entries: knowledgeEntries.length,
				project_entries: projectEntries.length,
				stable: bucketCounts.stable,
				active: bucketCounts.active,
				weak: bucketCounts.weak,
				decay_candidates: bucketCounts.decay_candidate,
				archive_candidates: archiveCandidates.length,
				archived: archivedEntries.length,
				promotion_candidates: promotionCandidates.length,
				promoted: promotedEntries.length,
				promotion_limit: options.promotionLimit ?? null,
				archive_limit: options.archiveLimit ?? null,
			},
			archive_candidates: archiveCandidates.map((entry) => ({
				id: entry.id,
				type: entry.type,
				label: entry.label,
				context_type: entry.contextType,
				injection_tier: entry.injectionTier,
				salience_score: entry.salienceScore,
				mention_count: entry.mentionCount,
				access_count: entry.accessCount,
				updated_at: entry.updatedAt,
			})),
			archived_entries: archivedEntries,
			promoted_entries: promotedEntries,
			archive_candidates_sample: summarizeArchiveCandidates(archiveCandidates),
			next_action: options.dryRun
				? "Review archive candidates and enable live archive writes only after validating the dry-run output."
				: "Review archived entries and confirm restore semantics before enabling live nightly archival.",
		};

		await writeRunRecord(redis, runRecord, setAsLatest);
		return runRecord;
	} catch (error) {
		const failedRecord = {
			...baseRunRecord,
			status: "failed" as DreamStatus,
			completed_at: new Date().toISOString(),
			error: error instanceof Error ? error.message : String(error),
			next_action: "Inspect the Dream run audit and fix the Worker before retrying.",
		};
		await writeRunRecord(redis, failedRecord, false);
		throw error;
	} finally {
		const currentLock = parseStoredObject(await redis.get(DREAM_LOCK_KEY));
		if (currentLock?.run_id === runId) {
			await redis.del(DREAM_LOCK_KEY);
		}
	}
}
