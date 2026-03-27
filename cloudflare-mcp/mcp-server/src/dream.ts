import { Redis } from "@upstash/redis/cloudflare";
import { computeSalience, MEMORY_POLICY, resolveStoredInjectionTier } from "./salience";

type EntryType = "knowledge" | "project";
type DreamStatus = "completed" | "skipped_no_backfill" | "skipped_locked" | "failed";
type DreamBucket = "stable" | "active" | "weak" | "decay_candidate";

interface RunDreamOptions {
	dryRun: boolean;
	trigger: "scheduled" | "manual" | "local_test";
	cron?: string | null;
	scheduledTime?: number | null;
	note?: string | null;
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
	salienceScore: number;
}

const DREAM_LOCK_KEY = "dream:lock";
const DREAM_LAST_RUN_KEY = "dream:last_run";
const DREAM_LAST_ATTEMPT_KEY = "dream:last_attempt";
const DREAM_RUN_PREFIX = "dream:run:";
const DREAM_LOCK_TTL_SECONDS = 30 * 60;
const DREAM_SAMPLE_LIMIT = 25;
const DREAM_SCAN_COUNT = 200;

function createRedisClient(env: Env): Redis {
	return new Redis({
		url: env.UPSTASH_REDIS_REST_URL,
		token: env.UPSTASH_REDIS_REST_TOKEN,
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

async function loadEntriesByType(redis: Redis, entryType: EntryType): Promise<LoadedEntry[]> {
	const keys = await scanKeys(redis, `${entryType}:*`);
	if (keys.length === 0) return [];

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
	for (let index = 0; index < normalizedEntries.length; index += 1) {
		const entry = normalizedEntries[index];
		const entryId = typeof entry.id === "string" ? entry.id : null;
		if (!entryId) continue;

		overlayAccessSignals(entry, accessCounts[index], lastAccessedValues[index]);
		const metadata = (entry.metadata as Record<string, unknown> | undefined) ?? {};
		if (metadata.archived === true) continue;

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
			salienceScore,
		});
	}

	return loadedEntries;
}

function isArchiveCandidate(entry: LoadedEntry): boolean {
	return (
		(entry.contextType === "task_query" || entry.contextType === "passing_reference") &&
		entry.mentionCount === 1 &&
		entry.accessCount === 0 &&
		entry.salienceScore < MEMORY_POLICY.dream_thresholds.archive_candidate_salience
	);
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

export async function runDreamCycle(
	env: Env,
	options: RunDreamOptions,
): Promise<Record<string, unknown>> {
	const redis = createRedisClient(env);
	const startedAt = new Date(
		typeof options.scheduledTime === "number" ? options.scheduledTime : Date.now(),
	).toISOString();
	const runId = `dr_${startedAt.replace(/[:.]/g, "-")}`;
	const baseRunRecord = buildBaseRunRecord(runId, options, startedAt);

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
		await writeRunRecord(redis, skippedRecord, true);
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
		const [knowledgeEntries, projectEntries] = await Promise.all([
			loadEntriesByType(redis, "knowledge"),
			loadEntriesByType(redis, "project"),
		]);
		const allEntries = [...knowledgeEntries, ...projectEntries];
		const archiveCandidates = allEntries.filter(isArchiveCandidate);
		const promotionCandidates = allEntries.filter(
			(entry) =>
				entry.contextType === "task_query" &&
				entry.mentionCount >= MEMORY_POLICY.dream_thresholds.promote_candidate_min_mentions,
		);
		const bucketCounts: Record<DreamBucket, number> = {
			stable: 0,
			active: 0,
			weak: 0,
			decay_candidate: 0,
		};

		for (const entry of allEntries) {
			bucketCounts[classifyBucket(entry)] += 1;
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
					status: "deferred",
					promotion_candidate_count: promotionCandidates.length,
					reason:
						"Phase 5 scaffolding only; transcript replay, contradiction detection, and dedupe are not enabled yet.",
				},
				consolidate: {
					status: options.dryRun ? "dry_run" : "blocked",
					reason:
						"Current Dream implementation writes audit output only. Live consolidation and archival are intentionally disabled.",
				},
				prune: {
					status: options.dryRun ? "dry_run" : "blocked",
					archive_candidate_count: archiveCandidates.length,
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
				promotion_candidates: promotionCandidates.length,
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
			archive_candidates_sample: summarizeArchiveCandidates(archiveCandidates),
			next_action:
				"Review archive candidates and enable live archive writes only after validating the dry-run output.",
		};

		await writeRunRecord(redis, runRecord, true);
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
