import { Redis } from "@upstash/redis/cloudflare";
import { Index } from "@upstash/vector";
import OpenAI from "openai";
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
type KnowledgeEntryState = "active" | "contested" | "stale";
type KnowledgeEntryConfidence = "high" | "medium" | "low";

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

interface DuplicateMergePlan {
	fingerprint: string;
	canonical: LoadedEntry;
	duplicates: LoadedEntry[];
}

interface ContradictionPlan {
	entryIds: string[];
	label: string;
	reasons: string[];
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

interface UpdateEntryParams {
	entryId: string;
	expectedRevision: number;
	mutationId: string;
	reason: string;
	actorId: string;
	currentView?: string;
	confidence?: KnowledgeEntryConfidence;
	state?: KnowledgeEntryState;
	contextType?: string;
}

interface CreateEntryParams {
	mutationId: string;
	reason: string;
	actorId: string;
	domain: string;
	currentView: string;
	confidence?: KnowledgeEntryConfidence;
	state?: KnowledgeEntryState;
	contextType?: string;
	keyInsights?: string[];
	sourceConversationId?: string;
	sourceMessageIds?: string[];
	evidenceSnippet?: string;
}

interface ArchiveEntryParams {
	entryId: string;
	expectedRevision: number;
	mutationId: string;
	reason: string;
	actorId: string;
}

interface RestoreEntryParams {
	entryId: string;
	expectedRevision: number;
	mutationId: string;
	reason: string;
	actorId: string;
	restoreOverrides?: {
		currentView?: string;
		confidence?: KnowledgeEntryConfidence;
		state?: KnowledgeEntryState;
		contextType?: string;
	};
}

interface AddInsightParams {
	entryId: string;
	expectedRevision: number;
	mutationId: string;
	reason: string;
	actorId: string;
	insight: string;
	sourceConversationId?: string;
	sourceMessageIds?: string[];
	evidenceSnippet?: string;
}

interface ConsolidateEntriesParams {
	keepId: string;
	archiveIds: string[];
	expectedRevisions: Record<string, number>;
	mutationId: string;
	reason: string;
	actorId: string;
	updatedView?: string;
	confidence?: KnowledgeEntryConfidence;
	contextType?: string;
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
const MUTATION_LOG_KEY = "mutation_log";
const MUTATION_LOG_LIMIT = 1000;
const MUTATION_RESULT_PREFIX = "mutation_result:";
const MUTATION_RESULT_TTL_SECONDS = 72 * 60 * 60;
const THIN_INDEX_TOPIC_LIMIT = 100;
const THIN_INDEX_PROJECT_LIMIT = 50;
const DUPLICATE_FINGERPRINT_MIN_LENGTH = 6;
const CONTRADICTION_MARKER_PAIRS: Array<[string, string]> = [
	["outperform", "underperform"],
	["bullish", "bearish"],
	["rising", "falling"],
	["increase", "decrease"],
	["improve", "worsen"],
	["positive", "negative"],
	["buy", "sell"],
	["expand", "contract"],
	["accelerating", "slowing"],
];

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

function getMutationResultKey(mutationId: string): string {
	return `${MUTATION_RESULT_PREFIX}${mutationId}`;
}

async function generateEntryId(redis: Redis, entryType: EntryType): Promise<string> {
	const prefix = entryType === "knowledge" ? "ke" : "pe";
	for (let attempt = 0; attempt < 5; attempt += 1) {
		const id = `${prefix}_${crypto.randomUUID().replace(/-/g, "").slice(0, 12)}`;
		const existing = await redis.get(getEntryKey(entryType, id));
		if (!existing) {
			return id;
		}
	}
	throw new Error(`unable_to_allocate_${prefix}_id`);
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
		revision: toOptionalInteger(metadata.revision) ?? 0,
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
	const base = {
		type: entry.type,
		archived: Boolean(entry.metadata.archived),
		context_type: entry.metadata.context_type,
		injection_tier: entry.metadata.injection_tier,
		salience_score: entry.metadata.salience_score,
		mention_count: entry.metadata.mention_count,
		last_consolidated: entry.metadata.last_consolidated,
		updated_at: entry.updatedAt,
	};
	if (entry.type === "knowledge") {
		return {
			...base,
			domain: typeof entry.entry.domain === "string" ? entry.entry.domain : entry.label,
			state: typeof entry.entry.state === "string" ? entry.entry.state : "active",
			confidence: typeof entry.entry.confidence === "string" ? entry.entry.confidence : "medium",
		};
	}
	return {
		...base,
		name: typeof entry.entry.name === "string" ? entry.entry.name : entry.label,
		status: typeof entry.entry.status === "string" ? entry.entry.status : "active",
	};
}

function truncate(value: unknown, maxLength: number): string {
	const text = typeof value === "string" ? value : "";
	if (text.length <= maxLength) {
		return text;
	}
	return `${text.slice(0, maxLength - 3)}...`;
}

function normalizeComparableText(value: unknown): string {
	return typeof value === "string"
		? value
			.toLowerCase()
			.replace(/[^a-z0-9]+/g, " ")
			.replace(/\s+/g, " ")
			.trim()
		: "";
}

function tokenizeComparableText(value: unknown): string[] {
	const normalized = normalizeComparableText(value);
	return normalized.length > 0 ? normalized.split(" ") : [];
}

function computeTokenSimilarity(left: unknown, right: unknown): number {
	const leftTokens = new Set(tokenizeComparableText(left));
	const rightTokens = new Set(tokenizeComparableText(right));
	if (leftTokens.size === 0 || rightTokens.size === 0) {
		return 0;
	}

	let intersection = 0;
	for (const token of leftTokens) {
		if (rightTokens.has(token)) {
			intersection += 1;
		}
	}

	const union = new Set([...leftTokens, ...rightTokens]).size;
	return union === 0 ? 0 : intersection / union;
}

function getNarrativeText(entry: LoadedEntry): string {
	if (entry.type === "knowledge") {
		return typeof entry.entry.current_view === "string" ? entry.entry.current_view : "";
	}

	return [
		typeof entry.entry.goal === "string" ? entry.entry.goal : "",
		typeof entry.entry.current_phase === "string" ? entry.entry.current_phase : "",
		typeof entry.entry.blocked_on === "string" ? entry.entry.blocked_on : "",
	].filter(Boolean).join(" ");
}

function getDuplicateFingerprint(entry: LoadedEntry): string | null {
	const fingerprint = normalizeComparableText(entry.label);
	if (fingerprint.length < DUPLICATE_FINGERPRINT_MIN_LENGTH) {
		return null;
	}
	return `${entry.type}:${fingerprint}`;
}

function containsMarker(text: string, marker: string): boolean {
	return normalizeComparableText(text).includes(marker);
}

function findOpposingMarkerReason(left: string, right: string): string | null {
	for (const [positive, negative] of CONTRADICTION_MARKER_PAIRS) {
		const leftPositive = containsMarker(left, positive);
		const leftNegative = containsMarker(left, negative);
		const rightPositive = containsMarker(right, positive);
		const rightNegative = containsMarker(right, negative);
		if ((leftPositive && rightNegative) || (leftNegative && rightPositive)) {
			return `opposing markers (${positive} vs ${negative})`;
		}
	}
	return null;
}

function getEntryConfidence(entry: LoadedEntry): string {
	return entry.type === "knowledge" && typeof entry.entry.confidence === "string"
		? entry.entry.confidence
		: "medium";
}

function compareCanonicalPriority(left: LoadedEntry, right: LoadedEntry): number {
	if (left.injectionTier !== right.injectionTier) {
		return left.injectionTier - right.injectionTier;
	}
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

function entriesAreCompatibleDuplicates(left: LoadedEntry, right: LoadedEntry): boolean {
	if (left.type !== right.type) return false;
	if (getDuplicateFingerprint(left) !== getDuplicateFingerprint(right)) return false;
	if (left.type === "project") return true;

	const leftNarrative = getNarrativeText(left);
	const rightNarrative = getNarrativeText(right);
	if (!leftNarrative || !rightNarrative) {
		return true;
	}

	const opposingMarkerReason = findOpposingMarkerReason(leftNarrative, rightNarrative);
	if (opposingMarkerReason) {
		return false;
	}

	const similarity = computeTokenSimilarity(leftNarrative, rightNarrative);
	return similarity >= 0.3 ||
		normalizeComparableText(leftNarrative).includes(normalizeComparableText(rightNarrative)) ||
		normalizeComparableText(rightNarrative).includes(normalizeComparableText(leftNarrative));
}

function detectPairContradictionReason(left: LoadedEntry, right: LoadedEntry): string | null {
	if (left.type !== "knowledge" || right.type !== "knowledge") return null;
	if (getDuplicateFingerprint(left) !== getDuplicateFingerprint(right)) return null;

	const leftNarrative = getNarrativeText(left);
	const rightNarrative = getNarrativeText(right);
	if (!leftNarrative || !rightNarrative) return null;

	const opposingMarkerReason = findOpposingMarkerReason(leftNarrative, rightNarrative);
	if (opposingMarkerReason) {
		return opposingMarkerReason;
	}

	if (getEntryConfidence(left) === "low" || getEntryConfidence(right) === "low") {
		return null;
	}

	const similarity = computeTokenSimilarity(leftNarrative, rightNarrative);
	return similarity <= 0.12
		? `same topic has materially different views (similarity=${similarity.toFixed(2)})`
		: null;
}

function detectInternalContradictionReason(entry: LoadedEntry): string | null {
	if (entry.type !== "knowledge") return null;
	if (typeof entry.entry.state === "string" && entry.entry.state === "contested") return null;

	const positions = Array.isArray(entry.entry.positions)
		? entry.entry.positions.filter(
			(position): position is Record<string, unknown> =>
				Boolean(position) && typeof position === "object" && !Array.isArray(position),
		)
		: [];
	const views = positions
		.map((position) => (typeof position.view === "string" ? position.view : ""))
		.filter((view) => view.length > 0);
	if (views.length < 2) {
		return null;
	}

	for (let leftIndex = 0; leftIndex < views.length; leftIndex += 1) {
		for (let rightIndex = leftIndex + 1; rightIndex < views.length; rightIndex += 1) {
			const opposingMarkerReason = findOpposingMarkerReason(views[leftIndex], views[rightIndex]);
			if (opposingMarkerReason) {
				return `internal positions contain ${opposingMarkerReason}`;
			}

			const similarity = computeTokenSimilarity(views[leftIndex], views[rightIndex]);
			if (similarity <= 0.08) {
				return `internal positions materially diverge (similarity=${similarity.toFixed(2)})`;
			}
		}
	}

	return null;
}

function buildReplayPlans(entries: LoadedEntry[]): {
	duplicatePlans: DuplicateMergePlan[];
	contradictionPlans: ContradictionPlan[];
} {
	const groups = new Map<string, LoadedEntry[]>();
	for (const entry of entries) {
		const fingerprint = getDuplicateFingerprint(entry);
		if (!fingerprint) continue;
		const existing = groups.get(fingerprint) ?? [];
		existing.push(entry);
		groups.set(fingerprint, existing);
	}

	const contradictionPlans: ContradictionPlan[] = [];
	const duplicatePlans: DuplicateMergePlan[] = [];

	for (const group of groups.values()) {
		if (group.length < 2) continue;

		const contradictionReasons = new Set<string>();
		for (let leftIndex = 0; leftIndex < group.length; leftIndex += 1) {
			for (let rightIndex = leftIndex + 1; rightIndex < group.length; rightIndex += 1) {
				const reason = detectPairContradictionReason(group[leftIndex], group[rightIndex]);
				if (reason) {
					contradictionReasons.add(reason);
				}
			}
		}

		if (contradictionReasons.size > 0) {
			contradictionPlans.push({
				entryIds: group.map((entry) => entry.id),
				label: group[0].label,
				reasons: [...contradictionReasons],
			});
			continue;
		}

		if (group.every((entry, index) =>
			group.slice(index + 1).every((other) => entriesAreCompatibleDuplicates(entry, other))
		)) {
			const ordered = [...group].sort(compareCanonicalPriority);
			duplicatePlans.push({
				fingerprint: getDuplicateFingerprint(ordered[0]) ?? ordered[0].id,
				canonical: ordered[0],
				duplicates: ordered.slice(1),
			});
		}
	}

	for (const entry of entries) {
		const reason = detectInternalContradictionReason(entry);
		if (!reason) continue;
		contradictionPlans.push({
			entryIds: [entry.id],
			label: entry.label,
			reasons: [reason],
		});
	}

	return { duplicatePlans, contradictionPlans };
}

function mergeStringArraysUnique(...values: unknown[]): string[] {
	return [...new Set(values.flatMap((value) => toStringArray(value)))];
}

function mergeObjectArraysUnique(...values: unknown[]): Array<Record<string, unknown>> {
	const merged = new Map<string, Record<string, unknown>>();
	for (const value of values) {
		if (!Array.isArray(value)) continue;
		for (const item of value) {
			if (!item || typeof item !== "object" || Array.isArray(item)) continue;
			merged.set(JSON.stringify(item), item as Record<string, unknown>);
		}
	}
	return [...merged.values()];
}

function earliestIsoTimestamp(...values: Array<string | null | undefined>): string | null {
	let earliestValue: string | null = null;
	let earliestTime = Number.POSITIVE_INFINITY;

	for (const value of values) {
		if (!value) continue;
		const timestamp = new Date(value).getTime();
		if (Number.isNaN(timestamp)) continue;
		if (timestamp < earliestTime) {
			earliestTime = timestamp;
			earliestValue = value;
		}
	}

	return earliestValue;
}

function mergeSourceWeights(...values: unknown[]): Record<string, number> {
	const merged: Record<string, number> = {};
	for (const value of values) {
		if (!value || typeof value !== "object" || Array.isArray(value)) continue;
		for (const [key, rawWeight] of Object.entries(value as Record<string, unknown>)) {
			const weight = toOptionalNumber(rawWeight);
			if (weight === null) continue;
			merged[key] = (merged[key] ?? 0) + weight;
		}
	}
	return merged;
}

function ensureRelatedKnowledgeLink(
	entry: Record<string, unknown>,
	relatedId: string,
	relationship: string,
): void {
	const existingRelatedKnowledge = Array.isArray(entry.related_knowledge)
		? entry.related_knowledge
		: [];

	const relatedKnowledge = existingRelatedKnowledge
		.filter((item): item is Record<string, unknown> =>
			Boolean(item) && typeof item === "object" && !Array.isArray(item),
		);
	const exists = relatedKnowledge.some(
		(item) =>
			item.knowledge_id === relatedId &&
			item.relationship === relationship,
	);
	if (!exists) {
		relatedKnowledge.push({
			knowledge_id: relatedId,
			relationship,
		});
	}
	entry.related_knowledge = relatedKnowledge;
}

function removeRelatedKnowledgeLinks(
	entry: Record<string, unknown>,
	relatedIds: string[],
	relationships?: string[],
): void {
	const relatedIdSet = new Set(relatedIds);
	const relationshipSet = relationships ? new Set(relationships) : null;
	const existingRelatedKnowledge = Array.isArray(entry.related_knowledge)
		? entry.related_knowledge
		: [];
	const relatedKnowledge = existingRelatedKnowledge.filter(
		(item): item is Record<string, unknown> =>
			Boolean(item) && typeof item === "object" && !Array.isArray(item),
	);
	entry.related_knowledge = relatedKnowledge.filter((item) => {
		if (!relatedIdSet.has(String(item.knowledge_id ?? ""))) {
			return true;
		}
		if (!relationshipSet) {
			return false;
		}
		return !relationshipSet.has(String(item.relationship ?? ""));
	});
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
	options?: { embedding?: number[]; skipVector?: boolean },
): Promise<void> {
	entry.metadata.salience_score = computeSalience(entry.entry);
	entry.entry.metadata = entry.metadata;
	await redis.set(getEntryKey(entry.type, entry.id), JSON.stringify(entry.entry));
	if (options?.skipVector) {
		return;
	}
	if (options?.embedding) {
		await vector.upsert({
			id: entry.id,
			vector: options.embedding,
			metadata: setVectorMetadataBase(entry),
		});
		return;
	}
	await vector.update({
		id: entry.id,
		metadata: setVectorMetadataBase(entry),
		metadataUpdateMode: "PATCH",
	});
}

async function deleteVectorEntry(vector: Index, entryId: string): Promise<void> {
	const deletableVector = vector as Index & {
		delete?: (ids: string | string[]) => Promise<unknown>;
	};
	if (typeof deletableVector.delete === "function") {
		await deletableVector.delete(entryId);
	}
}

async function getEmbedding(env: Env, text: string): Promise<number[]> {
	if (!env.OPENAI_API_KEY) {
		throw new Error("OPENAI_API_KEY not configured");
	}

	const openai = new OpenAI({ apiKey: env.OPENAI_API_KEY });
	const response = await openai.embeddings.create({
		model: "text-embedding-3-large",
		input: text,
		dimensions: 3072,
	});
	return response.data[0].embedding;
}

async function appendMutationLog(
	redis: Redis,
	event: Record<string, unknown>,
): Promise<void> {
	await redis.lpush(MUTATION_LOG_KEY, JSON.stringify(event));
	await redis.ltrim(MUTATION_LOG_KEY, 0, MUTATION_LOG_LIMIT - 1);
}

function appendEvolutionNote(
	entry: Record<string, unknown>,
	timestamp: string,
	actorId: string,
	reason: string,
): void {
	const currentEvolution = Array.isArray(entry.evolution) ? entry.evolution : [];
	currentEvolution.push({
		date: timestamp,
		actor: actorId,
		change_summary: reason,
	});
	entry.evolution = currentEvolution.slice(-50);
}

function buildThinIndexTopicEntry(entry: LoadedEntry, generatedAt: string): Record<string, unknown> {
	return {
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
		archived: Boolean(entry.metadata.archived),
	};
}

function buildThinIndexProjectEntry(entry: LoadedEntry, generatedAt: string): Record<string, unknown> {
	const primaryRepo =
		getRelatedRepos(entry.entry).find((repo) => repo.is_primary === true) ??
		getRelatedRepos(entry.entry)[0];
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
		archived: Boolean(entry.metadata.archived),
	};
}

function buildEntryEmbeddingText(entry: LoadedEntry): string {
	if (entry.type === "knowledge") {
		const insightTexts = Array.isArray(entry.entry.key_insights)
			? entry.entry.key_insights
				.filter(
					(item): item is Record<string, unknown> =>
						Boolean(item) && typeof item === "object" && !Array.isArray(item),
				)
				.map((item) => (typeof item.insight === "string" ? item.insight.trim() : ""))
				.filter((item) => item.length > 0)
				.slice(0, 3)
			: [];
		return [
			typeof entry.entry.domain === "string" ? entry.entry.domain : entry.label,
			typeof entry.entry.current_view === "string" ? entry.entry.current_view : "",
			...insightTexts,
		]
			.map((part) => part.trim())
			.filter((part) => part.length > 0)
			.join(" ");
	}

	return [
		typeof entry.entry.name === "string" ? entry.entry.name : entry.label,
		typeof entry.entry.goal === "string" ? entry.entry.goal : "",
		typeof entry.entry.current_phase === "string" ? entry.entry.current_phase : "",
	]
		.map((part) => part.trim())
		.filter((part) => part.length > 0)
		.join(" ");
}

function buildLoadedEntry(
	entryId: string,
	entryType: EntryType,
	entry: Record<string, unknown>,
): LoadedEntry {
	const metadata = (entry.metadata as Record<string, unknown> | undefined) ?? {};
	const loadedEntry: LoadedEntry = {
		id: entryId,
		type: entryType,
		entry,
		metadata,
		label: getEntryLabel(entry),
		updatedAt: getEntryUpdatedAt(entry, metadata),
		contextType:
			typeof metadata.context_type === "string" ? metadata.context_type : "task_query",
		injectionTier: resolveStoredInjectionTier(metadata),
		mentionCount: Math.max(1, toOptionalInteger(metadata.mention_count) ?? 1),
		accessCount: Math.max(0, toOptionalInteger(metadata.access_count) ?? 0),
		sourceConversationCount: toStringArray(metadata.source_conversations).length,
		salienceScore: computeSalience(entry),
	};
	loadedEntry.metadata.salience_score = loadedEntry.salienceScore;
	return loadedEntry;
}

async function loadLoadedEntry(
	redis: Redis,
	entryType: EntryType,
	entryId: string,
): Promise<LoadedEntry | null> {
	const entry = normalizeEntry(await redis.get(getEntryKey(entryType, entryId)), entryType);
	if (!entry) {
		return null;
	}
	const [accessCountRaw, lastAccessedRaw] = await Promise.all([
		redis.get(getEntryAccessKey(entryId)),
		redis.get(getEntryLastAccessedKey(entryId)),
	]);
	overlayAccessSignals(entry, accessCountRaw, lastAccessedRaw);
	return buildLoadedEntry(entryId, entryType, entry);
}

async function patchThinIndexEntry(
	redis: Redis,
	entry: LoadedEntry,
	generatedAt: string,
): Promise<void> {
	const rawIndex = parseStoredObject(await redis.get("index:current"));
	if (!rawIndex) {
		return;
	}

	if (entry.type === "knowledge") {
		const existingTopics = Array.isArray(rawIndex.topics)
			? rawIndex.topics.filter(
				(topic): topic is Record<string, unknown> =>
					Boolean(topic) && typeof topic === "object" && !Array.isArray(topic),
			)
			: [];
		const thinEntry = buildThinIndexTopicEntry(entry, generatedAt);
		let found = false;
		const nextTopics = existingTopics.map((topic) => {
			if (topic.id !== entry.id) {
				return topic;
			}
			found = true;
			return {
				...topic,
				...thinEntry,
			};
		});
		if (!found && !thinEntry.archived) {
			nextTopics.push(thinEntry);
		}
		nextTopics.sort((left, right) => {
			const tierDiff =
				(Number(left.injection_tier ?? 3) - Number(right.injection_tier ?? 3));
			if (tierDiff !== 0) return tierDiff;
			const salienceDiff =
				Number(right.salience_score ?? 0) - Number(left.salience_score ?? 0);
			if (salienceDiff !== 0) return salienceDiff;
			return sortTimestamp(
				typeof right.last_updated === "string" ? right.last_updated : null,
			) - sortTimestamp(
				typeof left.last_updated === "string" ? left.last_updated : null,
			);
		});
		rawIndex.topics = nextTopics.slice(0, THIN_INDEX_TOPIC_LIMIT);
	} else {
		const existingProjects = Array.isArray(rawIndex.projects)
			? rawIndex.projects.filter(
				(project): project is Record<string, unknown> =>
					Boolean(project) && typeof project === "object" && !Array.isArray(project),
			)
			: [];
		const thinEntry = buildThinIndexProjectEntry(entry, generatedAt);
		let found = false;
		const nextProjects = existingProjects.map((project) => {
			if (project.id !== entry.id) {
				return project;
			}
			found = true;
			return {
				...project,
				...thinEntry,
			};
		});
		if (!found && !thinEntry.archived) {
			nextProjects.push(thinEntry);
		}
		nextProjects.sort((left, right) => {
			const tierDiff =
				(Number(left.injection_tier ?? 3) - Number(right.injection_tier ?? 3));
			if (tierDiff !== 0) return tierDiff;
			const salienceDiff =
				Number(right.salience_score ?? 0) - Number(left.salience_score ?? 0);
			if (salienceDiff !== 0) return salienceDiff;
			return sortTimestamp(
				typeof right.last_touched === "string" ? right.last_touched : null,
			) - sortTimestamp(
				typeof left.last_touched === "string" ? left.last_touched : null,
			);
		});
		rawIndex.projects = nextProjects.slice(0, THIN_INDEX_PROJECT_LIMIT);
	}

	rawIndex.generated_at = generatedAt;
	rawIndex.token_count = 0;
	rawIndex.token_count = Math.round(JSON.stringify(rawIndex).length / 4);
	await redis.set("index:current", JSON.stringify(rawIndex));
}

async function incrementThinIndexCountsForCreate(
	redis: Redis,
	entry: LoadedEntry,
	generatedAt: string,
): Promise<void> {
	const rawIndex = parseStoredObject(await redis.get("index:current"));
	if (!rawIndex) {
		return;
	}

	if (entry.type === "knowledge") {
		rawIndex.total_topic_count = Math.max(
			0,
			toOptionalInteger(rawIndex.total_topic_count) ?? 0,
		) + 1;
	} else {
		rawIndex.total_project_count = Math.max(
			0,
			toOptionalInteger(rawIndex.total_project_count) ?? 0,
		) + 1;
	}

	const tierKey = `tier_${entry.injectionTier}_count`;
	rawIndex[tierKey] = Math.max(
		0,
		toOptionalInteger(rawIndex[tierKey]) ?? 0,
	) + 1;

	rawIndex.generated_at = generatedAt;
	rawIndex.token_count = Math.round(JSON.stringify(rawIndex).length / 4);
	await redis.set("index:current", JSON.stringify(rawIndex));
}

async function storeMutationResult(
	redis: Redis,
	mutationId: string,
	result: Record<string, unknown>,
): Promise<void> {
	await redis.set(getMutationResultKey(mutationId), JSON.stringify(result), {
		ex: MUTATION_RESULT_TTL_SECONDS,
	});
}

async function syncEntryAccessSignals(redis: Redis, entry: LoadedEntry): Promise<void> {
	const accessCount = Math.max(0, toOptionalInteger(entry.metadata.access_count) ?? entry.accessCount);
	await redis.set(getEntryAccessKey(entry.id), String(accessCount));

	const lastAccessed =
		typeof entry.metadata.last_accessed === "string" && entry.metadata.last_accessed.length > 0
			? entry.metadata.last_accessed
			: null;
	if (lastAccessed) {
		await redis.set(getEntryLastAccessedKey(entry.id), lastAccessed);
	} else {
		await redis.del(getEntryLastAccessedKey(entry.id));
	}
}

function mergeCanonicalEntry(
	canonical: LoadedEntry,
	duplicates: LoadedEntry[],
	runId: string,
	timestamp: string,
): LoadedEntry {
	const canonicalMetadata = canonical.metadata;

	for (const duplicate of duplicates) {
		const duplicateMetadata = duplicate.metadata;
		const duplicateEntry = duplicate.entry;

		canonicalMetadata.source_conversations = mergeStringArraysUnique(
			canonicalMetadata.source_conversations,
			duplicateMetadata.source_conversations,
		);
		canonicalMetadata.source_messages = mergeStringArraysUnique(
			canonicalMetadata.source_messages,
			duplicateMetadata.source_messages,
		);
		canonicalMetadata.source_weights = mergeSourceWeights(
			canonicalMetadata.source_weights,
			duplicateMetadata.source_weights,
		);
		canonicalMetadata.first_seen = earliestIsoTimestamp(
			typeof canonicalMetadata.first_seen === "string" ? canonicalMetadata.first_seen : null,
			typeof duplicateMetadata.first_seen === "string" ? duplicateMetadata.first_seen : null,
			typeof canonicalMetadata.created_at === "string" ? canonicalMetadata.created_at : null,
			typeof duplicateMetadata.created_at === "string" ? duplicateMetadata.created_at : null,
		);
		canonicalMetadata.last_seen = latestIsoTimestamp(
			typeof canonicalMetadata.last_seen === "string" ? canonicalMetadata.last_seen : null,
			typeof duplicateMetadata.last_seen === "string" ? duplicateMetadata.last_seen : null,
			typeof canonicalMetadata.updated_at === "string" ? canonicalMetadata.updated_at : null,
			typeof duplicateMetadata.updated_at === "string" ? duplicateMetadata.updated_at : null,
			canonical.updatedAt,
			duplicate.updatedAt,
		);
		canonicalMetadata.created_at = earliestIsoTimestamp(
			typeof canonicalMetadata.created_at === "string" ? canonicalMetadata.created_at : null,
			typeof duplicateMetadata.created_at === "string" ? duplicateMetadata.created_at : null,
		) ?? (typeof canonicalMetadata.created_at === "string" ? canonicalMetadata.created_at : timestamp);
		canonicalMetadata.updated_at = latestIsoTimestamp(
			typeof canonicalMetadata.updated_at === "string" ? canonicalMetadata.updated_at : null,
			typeof duplicateMetadata.updated_at === "string" ? duplicateMetadata.updated_at : null,
			canonical.updatedAt,
			duplicate.updatedAt,
			timestamp,
		) ?? timestamp;
		canonicalMetadata.access_count =
			(toOptionalInteger(canonicalMetadata.access_count) ?? canonical.accessCount) +
			(toOptionalInteger(duplicateMetadata.access_count) ?? duplicate.accessCount);
		canonicalMetadata.last_accessed = latestIsoTimestamp(
			typeof canonicalMetadata.last_accessed === "string" ? canonicalMetadata.last_accessed : null,
			typeof duplicateMetadata.last_accessed === "string" ? duplicateMetadata.last_accessed : null,
		);
		canonicalMetadata.mention_count =
			toStringArray(canonicalMetadata.source_conversations).length > 0
				? toStringArray(canonicalMetadata.source_conversations).length
				: (toOptionalInteger(canonicalMetadata.mention_count) ?? canonical.mentionCount) +
					(toOptionalInteger(duplicateMetadata.mention_count) ?? duplicate.mentionCount);

		if (canonical.type === "knowledge") {
			canonical.entry.key_insights = mergeObjectArraysUnique(
				canonical.entry.key_insights,
				duplicateEntry.key_insights,
			);
			canonical.entry.knows_how_to = mergeObjectArraysUnique(
				canonical.entry.knows_how_to,
				duplicateEntry.knows_how_to,
			);
			canonical.entry.open_questions = mergeObjectArraysUnique(
				canonical.entry.open_questions,
				duplicateEntry.open_questions,
			);
			canonical.entry.positions = mergeObjectArraysUnique(
				canonical.entry.positions,
				duplicateEntry.positions,
			);
			canonical.entry.evolution = mergeObjectArraysUnique(
				canonical.entry.evolution,
				duplicateEntry.evolution,
			);
		} else {
			canonical.entry.decisions_made = mergeObjectArraysUnique(
				canonical.entry.decisions_made,
				duplicateEntry.decisions_made,
			);
			canonical.entry.tech_stack = mergeStringArraysUnique(
				canonical.entry.tech_stack,
				duplicateEntry.tech_stack,
			);
			canonical.entry.phase_history = mergeObjectArraysUnique(
				canonical.entry.phase_history,
				duplicateEntry.phase_history,
			);
			if ((!canonical.entry.goal || String(canonical.entry.goal).length === 0) &&
				typeof duplicateEntry.goal === "string" && duplicateEntry.goal.length > 0) {
				canonical.entry.goal = duplicateEntry.goal;
			}
			if ((!canonical.entry.current_phase || String(canonical.entry.current_phase).length === 0) &&
				typeof duplicateEntry.current_phase === "string" && duplicateEntry.current_phase.length > 0) {
				canonical.entry.current_phase = duplicateEntry.current_phase;
			}
			if ((!canonical.entry.blocked_on || String(canonical.entry.blocked_on).length === 0) &&
				typeof duplicateEntry.blocked_on === "string" && duplicateEntry.blocked_on.length > 0) {
				canonical.entry.blocked_on = duplicateEntry.blocked_on;
			}
		}

		canonical.entry.related_repos = mergeObjectArraysUnique(
			canonical.entry.related_repos,
			duplicateEntry.related_repos,
		);
		canonical.entry.related_knowledge = mergeObjectArraysUnique(
			canonical.entry.related_knowledge,
			duplicateEntry.related_knowledge,
		);
	}

	canonicalMetadata.last_consolidated = timestamp;
	appendConsolidationNote(
		canonicalMetadata,
		formatConsolidationNote({
			timestamp,
			source: "dream",
			action: "merge_duplicate_entries",
			detail: `merged ${duplicates.map((entry) => entry.id).join(",")} into ${canonical.id} (run ${runId})`,
		}),
	);
	canonical.entry.metadata = canonicalMetadata;
	canonical.contextType =
		typeof canonicalMetadata.context_type === "string" ? canonicalMetadata.context_type : canonical.contextType;
	canonical.injectionTier = resolveStoredInjectionTier(canonicalMetadata);
	canonical.updatedAt = getEntryUpdatedAt(canonical.entry, canonicalMetadata);
	canonical.mentionCount = Math.max(1, toOptionalInteger(canonicalMetadata.mention_count) ?? canonical.mentionCount);
	canonical.accessCount = Math.max(0, toOptionalInteger(canonicalMetadata.access_count) ?? canonical.accessCount);
	canonical.sourceConversationCount = toStringArray(canonicalMetadata.source_conversations).length;
	canonical.salienceScore = computeSalience(canonical.entry);
	canonical.metadata.salience_score = canonical.salienceScore;

	return canonical;
}

async function applyDuplicateMergePlan(
	redis: Redis,
	vector: Index,
	plan: DuplicateMergePlan,
	runId: string,
	timestamp: string,
): Promise<Record<string, unknown>> {
	const canonical = mergeCanonicalEntry(plan.canonical, plan.duplicates, runId, timestamp);
	await persistEntry(redis, vector, canonical);
	await syncEntryAccessSignals(redis, canonical);

	const archivedDuplicates: Array<Record<string, unknown>> = [];
	for (const duplicate of plan.duplicates) {
		archivedDuplicates.push(
			await archiveEntry(
				redis,
				vector,
				duplicate,
				runId,
				timestamp,
				`merged duplicate into ${canonical.id}`,
			),
		);
		await redis.del(getEntryAccessKey(duplicate.id), getEntryLastAccessedKey(duplicate.id));
	}

	return {
		canonical_id: canonical.id,
		type: canonical.type,
		label: canonical.label,
		merged_entry_ids: plan.duplicates.map((entry) => entry.id),
		context_type: canonical.contextType,
		injection_tier: canonical.injectionTier,
		mention_count: canonical.mentionCount,
		access_count: canonical.accessCount,
		archived_duplicates: archivedDuplicates,
	};
}

async function markEntryContested(
	redis: Redis,
	vector: Index,
	entry: LoadedEntry,
	reasons: string[],
	conflictingWith: string[],
	runId: string,
	timestamp: string,
): Promise<Record<string, unknown>> {
	if (entry.type !== "knowledge") {
		throw new Error(`Contradiction handling only supports knowledge entries: ${entry.id}`);
	}

	const metadata = entry.metadata;
	entry.entry.state = "contested";
	for (const relatedId of conflictingWith) {
		ensureRelatedKnowledgeLink(entry.entry, relatedId, "contradicts");
	}
	metadata.last_consolidated = timestamp;
	appendConsolidationNote(
		metadata,
		formatConsolidationNote({
			timestamp,
			source: "dream",
			action: "mark_contested",
			detail: `${reasons.join("; ")} (run ${runId})`,
		}),
	);
	entry.entry.metadata = metadata;
	entry.updatedAt = getEntryUpdatedAt(entry.entry, metadata);
	entry.salienceScore = computeSalience(entry.entry);
	entry.metadata.salience_score = entry.salienceScore;
	await persistEntry(redis, vector, entry);

	return {
		id: entry.id,
		type: entry.type,
		label: entry.label,
		state: entry.entry.state,
		conflicting_with: conflictingWith,
		reasons,
	};
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
	await persistEntry(redis, vector, archivedEntry, { skipVector: true });
	await deleteVectorEntry(vector, entry.id);

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
		const embedding = await getEmbedding(env, buildEntryEmbeddingText(restoredLoadedEntry));
		await persistEntry(redis, vector, restoredLoadedEntry, { embedding });
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

export async function archiveExistingEntry(
	env: Env,
	params: ArchiveEntryParams,
): Promise<Record<string, unknown>> {
	const redis = createRedisClient(env);
	const vector = createVectorClient(env);
	const storedMutation = parseStoredObject(await redis.get(getMutationResultKey(params.mutationId)));
	if (storedMutation) {
		return storedMutation;
	}

	const entryType: EntryType = params.entryId.startsWith("pe_") ? "project" : "knowledge";
	const rawEntry = await redis.get(getEntryKey(entryType, params.entryId));
	const entry = normalizeEntry(rawEntry, entryType);
	if (!entry) {
		const result = {
			ok: false,
			error: "entry_not_found",
			id: params.entryId,
		};
		await storeMutationResult(redis, params.mutationId, result);
		return result;
	}

	const metadata = (entry.metadata as Record<string, unknown> | undefined) ?? {};
	if (metadata.archived === true) {
		const result = {
			ok: false,
			error: "entry_archived",
			id: params.entryId,
		};
		await storeMutationResult(redis, params.mutationId, result);
		return result;
	}

	const currentRevision = toOptionalInteger(metadata.revision) ?? 0;
	if (params.expectedRevision !== currentRevision) {
		const result = {
			ok: false,
			error: "conflict",
			id: params.entryId,
			expected_revision: params.expectedRevision,
			actual_revision: currentRevision,
			current_summary: {
				updated_at: getEntryUpdatedAt(entry, metadata),
				archived: false,
			},
		};
		await storeMutationResult(redis, params.mutationId, result);
		return result;
	}

	const timestamp = new Date().toISOString();
	const runId = `operator_archive_${params.entryId}_${timestamp.replace(/[:.]/g, "-")}`;
	const [accessCountRaw, lastAccessedRaw] = await Promise.all([
		redis.get(getEntryAccessKey(params.entryId)),
		redis.get(getEntryLastAccessedKey(params.entryId)),
	]);
	overlayAccessSignals(entry, accessCountRaw, lastAccessedRaw);

	const archiveSnapshotKey = getArchivedSnapshotKey(entryType, params.entryId, runId);
	const archivedSnapshot: ArchivedSnapshot = {
		schema_version: 1,
		entry_id: params.entryId,
		entry_type: entryType,
		run_id: runId,
		archived_at: timestamp,
		archive_reason: params.reason,
		snapshot: JSON.parse(JSON.stringify(entry)),
	};
	await redis.set(archiveSnapshotKey, JSON.stringify(archivedSnapshot));
	await redis.set(
		getArchivedLatestKey(entryType, params.entryId),
		JSON.stringify({
			entry_id: params.entryId,
			entry_type: entryType,
			run_id: runId,
			archived_at: timestamp,
			snapshot_key: archiveSnapshotKey,
		}),
	);

	const previousState =
		entryType === "knowledge" && typeof entry.state === "string" ? entry.state : null;
	metadata.archived = true;
	metadata.archived_at = timestamp;
	metadata.archived_reason = params.reason;
	metadata.archived_run_id = runId;
	metadata.archive_snapshot_key = archiveSnapshotKey;
	metadata.updated_at = timestamp;
	metadata.updated_by = {
		actor_id: params.actorId,
		tool: "archive_entry",
	};
	metadata.revision = currentRevision + 1;
	metadata.last_consolidated = timestamp;
	appendConsolidationNote(
		metadata,
		formatConsolidationNote({
			timestamp,
			source: "operator",
			action: "archive_entry",
			detail: params.reason,
		}),
	);
	entry.metadata = metadata;

	const loadedEntry = buildLoadedEntry(params.entryId, entryType, entry);

	if (entryType === "knowledge" && previousState) {
		await redis.srem(`by_state:${previousState}`, params.entryId);
		await redis.sadd("by_state:archived", params.entryId);
	}

	await persistEntry(redis, vector, loadedEntry, { skipVector: true });
	await deleteVectorEntry(vector, params.entryId);
	await patchThinIndexEntry(redis, loadedEntry, timestamp);

	const result = {
		ok: true,
		id: params.entryId,
		type: entryType,
		mutation_id: params.mutationId,
		revision: metadata.revision,
		archived: true,
		archived_at: timestamp,
		snapshot_key: archiveSnapshotKey,
		side_effects: {
			vector: "deleted",
		},
		entry,
	};
	await appendMutationLog(redis, {
		ts: timestamp,
		mutation_id: params.mutationId,
		tool: "archive_entry",
		client: "mcp",
		actor_id: params.actorId,
		request_id: params.mutationId,
		ids_affected: [params.entryId],
		before_revisions: { [params.entryId]: currentRevision },
		after_revisions: { [params.entryId]: metadata.revision as number },
		reason: params.reason,
	});
	await storeMutationResult(redis, params.mutationId, result);
	return result;
}

export async function restoreEntry(
	env: Env,
	params: RestoreEntryParams,
): Promise<Record<string, unknown>> {
	const redis = createRedisClient(env);
	const vector = createVectorClient(env);
	const storedMutation = parseStoredObject(await redis.get(getMutationResultKey(params.mutationId)));
	if (storedMutation) {
		return storedMutation;
	}

	const entryType: EntryType = params.entryId.startsWith("pe_") ? "project" : "knowledge";
	const rawEntry = await redis.get(getEntryKey(entryType, params.entryId));
	const currentEntry = normalizeEntry(rawEntry, entryType);
	if (!currentEntry) {
		const result = {
			ok: false,
			error: "entry_not_found",
			id: params.entryId,
		};
		await storeMutationResult(redis, params.mutationId, result);
		return result;
	}

	const currentMetadata = (currentEntry.metadata as Record<string, unknown> | undefined) ?? {};
	if (currentMetadata.archived !== true) {
		const result = {
			ok: false,
			error: "entry_not_archived",
			id: params.entryId,
		};
		await storeMutationResult(redis, params.mutationId, result);
		return result;
	}

	const currentRevision = toOptionalInteger(currentMetadata.revision) ?? 0;
	if (params.expectedRevision !== currentRevision) {
		const result = {
			ok: false,
			error: "conflict",
			id: params.entryId,
			expected_revision: params.expectedRevision,
			actual_revision: currentRevision,
			current_summary: {
				updated_at: getEntryUpdatedAt(currentEntry, currentMetadata),
				archived: true,
			},
		};
		await storeMutationResult(redis, params.mutationId, result);
		return result;
	}

	const latestPointer = parseStoredObject(await redis.get(getArchivedLatestKey(entryType, params.entryId)));
	if (!latestPointer?.snapshot_key || typeof latestPointer.snapshot_key !== "string") {
		const result = {
			ok: false,
			error: "archive_snapshot_missing",
			id: params.entryId,
		};
		await storeMutationResult(redis, params.mutationId, result);
		return result;
	}

	const archivedSnapshot = parseStoredObject(await redis.get(latestPointer.snapshot_key));
	const snapshotEntry = normalizeEntry(archivedSnapshot?.snapshot, entryType);
	if (!snapshotEntry) {
		const result = {
			ok: false,
			error: "archive_snapshot_missing",
			id: params.entryId,
		};
		await storeMutationResult(redis, params.mutationId, result);
		return result;
	}

	const timestamp = new Date().toISOString();
	const restoredMetadata = (snapshotEntry.metadata as Record<string, unknown> | undefined) ?? {};
	restoredMetadata.archived = false;
	restoredMetadata.updated_at = timestamp;
	restoredMetadata.updated_by = {
		actor_id: params.actorId,
		tool: "restore_entry",
	};
	restoredMetadata.revision = currentRevision + 1;
	restoredMetadata.restored_at = timestamp;
	restoredMetadata.restored_reason = params.reason;
	restoredMetadata.last_consolidated = timestamp;
	delete restoredMetadata.archived_at;
	delete restoredMetadata.archived_reason;
	delete restoredMetadata.archived_run_id;
	delete restoredMetadata.archive_snapshot_key;
	if (params.restoreOverrides?.currentView !== undefined && entryType === "knowledge") {
		snapshotEntry.current_view = params.restoreOverrides.currentView;
	}
	if (params.restoreOverrides?.confidence !== undefined && entryType === "knowledge") {
		snapshotEntry.confidence = params.restoreOverrides.confidence;
	}
	if (params.restoreOverrides?.state !== undefined && entryType === "knowledge") {
		snapshotEntry.state = params.restoreOverrides.state;
	}
	if (params.restoreOverrides?.contextType !== undefined) {
		restoredMetadata.context_type = params.restoreOverrides.contextType;
		restoredMetadata.classification_status = "manual_override";
		restoredMetadata.auto_inferred = false;
		restoredMetadata.injection_tier = defaultInjectionTier(params.restoreOverrides.contextType);
	}
	appendConsolidationNote(
		restoredMetadata,
		formatConsolidationNote({
			timestamp,
			source: "operator",
			action: "restore_entry",
			detail: params.reason,
		}),
	);
	snapshotEntry.metadata = restoredMetadata;

	const restoredLoadedEntry = buildLoadedEntry(params.entryId, entryType, snapshotEntry);

	if (entryType === "knowledge") {
		await redis.srem("by_state:archived", params.entryId);
		const restoredState =
			typeof snapshotEntry.state === "string" ? snapshotEntry.state : "active";
		await redis.sadd(`by_state:${restoredState}`, params.entryId);
	}

	const embedding = await getEmbedding(env, buildEntryEmbeddingText(restoredLoadedEntry));
	await persistEntry(redis, vector, restoredLoadedEntry, { embedding });
	await patchThinIndexEntry(redis, restoredLoadedEntry, timestamp);

	const result = {
		ok: true,
		id: params.entryId,
		type: entryType,
		mutation_id: params.mutationId,
		revision: restoredMetadata.revision,
		archived: false,
		restored_at: timestamp,
		side_effects: {
			vector: "recreated",
		},
		entry: snapshotEntry,
	};
	await appendMutationLog(redis, {
		ts: timestamp,
		mutation_id: params.mutationId,
		tool: "restore_entry",
		client: "mcp",
		actor_id: params.actorId,
		request_id: params.mutationId,
		ids_affected: [params.entryId],
		before_revisions: { [params.entryId]: currentRevision },
		after_revisions: { [params.entryId]: restoredMetadata.revision as number },
		reason: params.reason,
	});
	await storeMutationResult(redis, params.mutationId, result);
	return result;
}

export async function consolidateEntries(
	env: Env,
	params: ConsolidateEntriesParams,
): Promise<Record<string, unknown>> {
	const redis = createRedisClient(env);
	const vector = createVectorClient(env);
	const storedMutation = parseStoredObject(await redis.get(getMutationResultKey(params.mutationId)));
	if (storedMutation) {
		return storedMutation;
	}

	const uniqueArchiveIds = [...new Set(params.archiveIds)].filter((id) => id !== params.keepId);
	if (uniqueArchiveIds.length === 0) {
		const result = {
			ok: false,
			error: "invalid_request",
			message: "Provide at least one distinct archive id.",
		};
		await storeMutationResult(redis, params.mutationId, result);
		return result;
	}

	const entryType: EntryType = params.keepId.startsWith("pe_") ? "project" : "knowledge";
	const touchedIds = [params.keepId, ...uniqueArchiveIds];
	if (touchedIds.some((id) => (id.startsWith("pe_") ? "project" : "knowledge") !== entryType)) {
		const result = {
			ok: false,
			error: "mixed_entry_types",
			ids: touchedIds,
		};
		await storeMutationResult(redis, params.mutationId, result);
		return result;
	}

	const missingExpectedRevision = touchedIds.find((id) => typeof params.expectedRevisions[id] !== "number");
	if (missingExpectedRevision) {
		const result = {
			ok: false,
			error: "invalid_request",
			message: `Missing expected revision for ${missingExpectedRevision}.`,
		};
		await storeMutationResult(redis, params.mutationId, result);
		return result;
	}

	if (entryType !== "knowledge" && (params.updatedView !== undefined || params.confidence !== undefined)) {
		const result = {
			ok: false,
			error: "unsupported_entry_type",
			id: params.keepId,
			message: "updated_view and confidence are only supported for knowledge entries.",
		};
		await storeMutationResult(redis, params.mutationId, result);
		return result;
	}

	const loadedEntries = await Promise.all(
		touchedIds.map((id) => loadLoadedEntry(redis, entryType, id)),
	);
	const missingIndex = loadedEntries.findIndex((entry) => !entry);
	if (missingIndex >= 0) {
		const result = {
			ok: false,
			error: "entry_not_found",
			id: touchedIds[missingIndex],
		};
		await storeMutationResult(redis, params.mutationId, result);
		return result;
	}

	const resolvedEntries = loadedEntries as LoadedEntry[];
	const archivedEntry = resolvedEntries.find((entry) => entry.metadata.archived === true);
	if (archivedEntry) {
		const result = {
			ok: false,
			error: "entry_archived",
			id: archivedEntry.id,
		};
		await storeMutationResult(redis, params.mutationId, result);
		return result;
	}

	const beforeRevisions: Record<string, number> = {};
	for (const entry of resolvedEntries) {
		const revision = toOptionalInteger(entry.metadata.revision) ?? 0;
		beforeRevisions[entry.id] = revision;
		if (params.expectedRevisions[entry.id] !== revision) {
			const result = {
				ok: false,
				error: "conflict",
				id: entry.id,
				expected_revision: params.expectedRevisions[entry.id],
				actual_revision: revision,
			};
			await storeMutationResult(redis, params.mutationId, result);
			return result;
		}
	}

	const timestamp = new Date().toISOString();
	const runId = `operator_consolidate_${params.keepId}_${timestamp.replace(/[:.]/g, "-")}`;
	const canonical = mergeCanonicalEntry(
		resolvedEntries[0],
		resolvedEntries.slice(1),
		runId,
		timestamp,
	);
	const duplicateEntries = resolvedEntries.slice(1);

	if (entryType === "knowledge") {
		removeRelatedKnowledgeLinks(canonical.entry, uniqueArchiveIds, ["contradicts"]);
		for (const archiveId of uniqueArchiveIds) {
			ensureRelatedKnowledgeLink(canonical.entry, archiveId, "supersedes");
		}
		if (params.updatedView !== undefined) {
			canonical.entry.current_view = params.updatedView;
		}
		if (params.confidence !== undefined) {
			canonical.entry.confidence = params.confidence;
		}
		canonical.entry.state = "active";
	}

	if (params.contextType !== undefined) {
		canonical.metadata.context_type = params.contextType;
		canonical.metadata.classification_status = "manual_override";
		canonical.metadata.auto_inferred = false;
		canonical.metadata.injection_tier = defaultInjectionTier(params.contextType);
		canonical.contextType = params.contextType;
		canonical.injectionTier = defaultInjectionTier(params.contextType);
	}

	if (entryType === "knowledge") {
		appendEvolutionNote(canonical.entry, timestamp, params.actorId, params.reason);
	}
	canonical.metadata.updated_at = timestamp;
	canonical.metadata.updated_by = {
		actor_id: params.actorId,
		tool: "consolidate_entries",
	};
	canonical.metadata.revision = beforeRevisions[params.keepId] + 1;
	canonical.entry.metadata = canonical.metadata;
	canonical.updatedAt = getEntryUpdatedAt(canonical.entry, canonical.metadata);
	canonical.salienceScore = computeSalience(canonical.entry);
	canonical.metadata.salience_score = canonical.salienceScore;

	const rebuildRunId = `consolidate_${params.keepId}_${timestamp.replace(/[:.]/g, "-")}`;
	if (!(await acquireIndexRebuildLock(redis, rebuildRunId))) {
		throw new Error("index_rebuild_lock_held");
	}

	const archivedResults: Array<Record<string, unknown>> = [];
	const afterRevisions: Record<string, number> = {
		[params.keepId]: canonical.metadata.revision as number,
	};

	try {
		if (entryType === "knowledge") {
			const keepPreviousState =
				typeof resolvedEntries[0].entry.state === "string" ? resolvedEntries[0].entry.state : "active";
			if (keepPreviousState !== "active") {
				await redis.srem(`by_state:${keepPreviousState}`, params.keepId);
				await redis.sadd("by_state:active", params.keepId);
			}
		}

		const embedding = await getEmbedding(env, buildEntryEmbeddingText(canonical));
		await persistEntry(redis, vector, canonical, { embedding });
		await syncEntryAccessSignals(redis, canonical);
		await patchThinIndexEntry(redis, canonical, timestamp);

		for (const duplicate of duplicateEntries) {
			const duplicateMetadata = duplicate.metadata;
			const previousRevision = beforeRevisions[duplicate.id];
			const snapshotKey = getArchivedSnapshotKey(entryType, duplicate.id, runId);
			const archivedSnapshot: ArchivedSnapshot = {
				schema_version: 1,
				entry_id: duplicate.id,
				entry_type: entryType,
				run_id: runId,
				archived_at: timestamp,
				archive_reason: `${params.reason} (consolidated into ${params.keepId})`,
				snapshot: JSON.parse(JSON.stringify(duplicate.entry)),
			};
			await redis.set(snapshotKey, JSON.stringify(archivedSnapshot));
			await redis.set(
				getArchivedLatestKey(entryType, duplicate.id),
				JSON.stringify({
					entry_id: duplicate.id,
					entry_type: entryType,
					run_id: runId,
					archived_at: timestamp,
					snapshot_key: snapshotKey,
				}),
			);

			if (entryType === "knowledge") {
				const duplicateState =
					typeof duplicate.entry.state === "string" ? duplicate.entry.state : "active";
				await redis.srem(`by_state:${duplicateState}`, duplicate.id);
				await redis.sadd("by_state:archived", duplicate.id);
			}

			duplicateMetadata.archived = true;
			duplicateMetadata.archived_at = timestamp;
			duplicateMetadata.archived_reason = `${params.reason} (consolidated into ${params.keepId})`;
			duplicateMetadata.archived_run_id = runId;
			duplicateMetadata.archive_snapshot_key = snapshotKey;
			duplicateMetadata.updated_at = timestamp;
			duplicateMetadata.updated_by = {
				actor_id: params.actorId,
				tool: "consolidate_entries",
			};
			duplicateMetadata.revision = previousRevision + 1;
			duplicateMetadata.last_consolidated = timestamp;
			appendConsolidationNote(
				duplicateMetadata,
				formatConsolidationNote({
					timestamp,
					source: "operator",
					action: "archive_entry",
					detail: `consolidated into ${params.keepId}: ${params.reason}`,
				}),
			);
			duplicate.entry.metadata = duplicateMetadata;

			const archivedDuplicate = buildLoadedEntry(duplicate.id, entryType, duplicate.entry);
			await persistEntry(redis, vector, archivedDuplicate, { skipVector: true });
			await deleteVectorEntry(vector, duplicate.id);
			await patchThinIndexEntry(redis, archivedDuplicate, timestamp);
			await redis.del(getEntryAccessKey(duplicate.id), getEntryLastAccessedKey(duplicate.id));

			afterRevisions[duplicate.id] = duplicateMetadata.revision as number;
			archivedResults.push({
				id: duplicate.id,
				archived: true,
				revision: duplicateMetadata.revision,
				snapshot_key: snapshotKey,
			});
		}
	} finally {
		await releaseIndexRebuildLock(redis, rebuildRunId);
	}

	const result = {
		ok: true,
		mutation_id: params.mutationId,
		keep_id: params.keepId,
		archive_ids: uniqueArchiveIds,
		keep_entry: canonical.entry,
		archived_entries: archivedResults,
		side_effects: {
			kept_vector: "reembedded",
			archived_vectors: "deleted",
		},
	};
	await appendMutationLog(redis, {
		ts: timestamp,
		mutation_id: params.mutationId,
		tool: "consolidate_entries",
		client: "mcp",
		actor_id: params.actorId,
		request_id: params.mutationId,
		ids_affected: touchedIds,
		before_revisions: beforeRevisions,
		after_revisions: afterRevisions,
		reason: params.reason,
	});
	await storeMutationResult(redis, params.mutationId, result);
	return result;
}

export async function addInsight(
	env: Env,
	params: AddInsightParams,
): Promise<Record<string, unknown>> {
	const redis = createRedisClient(env);
	const vector = createVectorClient(env);
	const storedMutation = parseStoredObject(await redis.get(getMutationResultKey(params.mutationId)));
	if (storedMutation) {
		return storedMutation;
	}

	if (params.entryId.startsWith("pe_")) {
		const result = {
			ok: false,
			error: "unsupported_entry_type",
			id: params.entryId,
			entry_type: "project",
		};
		await storeMutationResult(redis, params.mutationId, result);
		return result;
	}

	const rawEntry = await redis.get(getEntryKey("knowledge", params.entryId));
	const entry = normalizeEntry(rawEntry, "knowledge");
	if (!entry) {
		const result = {
			ok: false,
			error: "entry_not_found",
			id: params.entryId,
		};
		await storeMutationResult(redis, params.mutationId, result);
		return result;
	}

	const metadata = (entry.metadata as Record<string, unknown> | undefined) ?? {};
	if (metadata.archived === true) {
		const result = {
			ok: false,
			error: "entry_archived",
			id: params.entryId,
		};
		await storeMutationResult(redis, params.mutationId, result);
		return result;
	}

	const currentRevision = toOptionalInteger(metadata.revision) ?? 0;
	if (params.expectedRevision !== currentRevision) {
		const result = {
			ok: false,
			error: "conflict",
			id: params.entryId,
			expected_revision: params.expectedRevision,
			actual_revision: currentRevision,
			current_summary: {
				updated_at: getEntryUpdatedAt(entry, metadata),
				key_insights_count: Array.isArray(entry.key_insights) ? entry.key_insights.length : 0,
			},
		};
		await storeMutationResult(redis, params.mutationId, result);
		return result;
	}

	const existingInsights = Array.isArray(entry.key_insights)
		? entry.key_insights.filter(
			(item): item is Record<string, unknown> =>
				Boolean(item) && typeof item === "object" && !Array.isArray(item),
		)
		: [];
	const normalizedInsight = normalizeComparableText(params.insight);
	const duplicateInsight = existingInsights.find(
		(item) => normalizeComparableText(item.insight) === normalizedInsight,
	);
	if (duplicateInsight) {
		const result = {
			ok: true,
			id: params.entryId,
			type: "knowledge",
			mutation_id: params.mutationId,
			revision: currentRevision,
			added: false,
			no_op: true,
			reason: "duplicate_insight",
			entry,
		};
		await storeMutationResult(redis, params.mutationId, result);
		return result;
	}

	const timestamp = new Date().toISOString();
	const mergedSourceConversations = mergeStringArraysUnique(
		metadata.source_conversations,
		params.sourceConversationId ? [params.sourceConversationId] : [],
	);
	metadata.source_conversations = mergedSourceConversations;
	metadata.source_messages = mergeStringArraysUnique(
		metadata.source_messages,
		params.sourceMessageIds ?? [],
	);
	metadata.mention_count = Math.max(
		1,
		mergedSourceConversations.length > 0
			? mergedSourceConversations.length
			: (toOptionalInteger(metadata.mention_count) ?? 1),
	);

	entry.key_insights = [
		...existingInsights,
		{
			insight: params.insight,
			evidence: {
				conversation_id: params.sourceConversationId ?? `mcp:${params.actorId}`,
				message_ids: params.sourceMessageIds ?? [],
				snippet: params.evidenceSnippet ?? params.reason,
			},
		},
	];
	appendEvolutionNote(entry, timestamp, params.actorId, params.reason);
	metadata.updated_at = timestamp;
	metadata.updated_by = {
		actor_id: params.actorId,
		tool: "add_insight",
	};
	metadata.revision = currentRevision + 1;
	entry.metadata = metadata;

	const loadedEntry: LoadedEntry = {
		id: params.entryId,
		type: "knowledge",
		entry,
		metadata,
		label: getEntryLabel(entry),
		updatedAt: getEntryUpdatedAt(entry, metadata),
		contextType:
			typeof metadata.context_type === "string" ? metadata.context_type : "task_query",
		injectionTier: resolveStoredInjectionTier(metadata),
		mentionCount: Math.max(1, toOptionalInteger(metadata.mention_count) ?? 1),
		accessCount: Math.max(0, toOptionalInteger(metadata.access_count) ?? 0),
		sourceConversationCount: toStringArray(metadata.source_conversations).length,
		salienceScore: computeSalience(entry),
	};
	loadedEntry.metadata.salience_score = loadedEntry.salienceScore;

	const rebuildRunId = `add_insight_${params.entryId}_${timestamp.replace(/[:.]/g, "-")}`;
	if (!(await acquireIndexRebuildLock(redis, rebuildRunId))) {
		throw new Error("index_rebuild_lock_held");
	}

	try {
		const embedding = await getEmbedding(env, buildEntryEmbeddingText(loadedEntry));
		await persistEntry(redis, vector, loadedEntry, { embedding });
		await patchThinIndexEntry(redis, loadedEntry, timestamp);
	} finally {
		await releaseIndexRebuildLock(redis, rebuildRunId);
	}

	const result = {
		ok: true,
		id: params.entryId,
		type: "knowledge",
		mutation_id: params.mutationId,
		revision: metadata.revision,
		added: true,
		updated_at: timestamp,
		side_effects: {
			vector: "reembedded",
		},
		entry,
	};
	await appendMutationLog(redis, {
		ts: timestamp,
		mutation_id: params.mutationId,
		tool: "add_insight",
		client: "mcp",
		actor_id: params.actorId,
		request_id: params.mutationId,
		ids_affected: [params.entryId],
		before_revisions: { [params.entryId]: currentRevision },
		after_revisions: { [params.entryId]: metadata.revision as number },
		reason: params.reason,
	});
	await storeMutationResult(redis, params.mutationId, result);
	return result;
}

export async function createEntry(
	env: Env,
	params: CreateEntryParams,
): Promise<Record<string, unknown>> {
	const redis = createRedisClient(env);
	const vector = createVectorClient(env);
	const storedMutation = parseStoredObject(await redis.get(getMutationResultKey(params.mutationId)));
	if (storedMutation) {
		return storedMutation;
	}

	const timestamp = new Date().toISOString();
	const entryId = await generateEntryId(redis, "knowledge");
	const contextType = params.contextType ?? "explicit_save";
	const state = params.state ?? "active";
	const confidence = params.confidence ?? "medium";
	const sourceConversations = params.sourceConversationId ? [params.sourceConversationId] : [];
	const sourceMessageIds = [...new Set(params.sourceMessageIds ?? [])];
	const keyInsights = [...new Set((params.keyInsights ?? []).map((value) => value.trim()).filter((value) => value.length > 0))];
	const evidence = {
		conversation_id: params.sourceConversationId ?? `mcp:${params.actorId}`,
		message_ids: sourceMessageIds,
		snippet: params.evidenceSnippet ?? params.reason,
	};

	const entry: Record<string, unknown> = {
		id: entryId,
		type: "knowledge",
		domain: params.domain.trim(),
		current_view: params.currentView.trim(),
		state,
		confidence,
		positions: [],
		key_insights: keyInsights.map((insight) => ({
			insight,
			evidence,
		})),
		knows_how_to: [],
		open_questions: [],
		related_repos: [],
		related_knowledge: [],
		evolution: [],
		metadata: {
			created_at: timestamp,
			updated_at: timestamp,
			first_seen: timestamp,
			last_seen: timestamp,
			updated_by: {
				actor_id: params.actorId,
				tool: "create_entry",
			},
			source: "mcp",
			source_conversations: sourceConversations,
			source_messages: sourceMessageIds,
			context_type: contextType,
			classification_status: "manual_override",
			auto_inferred: false,
			injection_tier: defaultInjectionTier(contextType),
			mention_count: Math.max(1, sourceConversations.length || 1),
			access_count: 0,
			revision: 1,
			archived: false,
		},
	};
	appendEvolutionNote(entry, timestamp, params.actorId, params.reason);

	const loadedEntry = buildLoadedEntry(entryId, "knowledge", entry);
	await redis.sadd(`by_state:${state}`, entryId);
	const embedding = await getEmbedding(env, buildEntryEmbeddingText(loadedEntry));
	await persistEntry(redis, vector, loadedEntry, { embedding });
	await syncEntryAccessSignals(redis, loadedEntry);
	await patchThinIndexEntry(redis, loadedEntry, timestamp);
	await incrementThinIndexCountsForCreate(redis, loadedEntry, timestamp);

	const result = {
		ok: true,
		id: entryId,
		type: "knowledge",
		mutation_id: params.mutationId,
		revision: 1,
		created: true,
		created_at: timestamp,
		side_effects: {
			vector: "created",
			index: "patched",
		},
		entry,
	};
	await appendMutationLog(redis, {
		ts: timestamp,
		mutation_id: params.mutationId,
		tool: "create_entry",
		client: "mcp",
		actor_id: params.actorId,
		request_id: params.mutationId,
		ids_affected: [entryId],
		before_revisions: {},
		after_revisions: { [entryId]: 1 },
		reason: params.reason,
	});
	await storeMutationResult(redis, params.mutationId, result);
	return result;
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

export async function updateEntry(
	env: Env,
	params: UpdateEntryParams,
): Promise<Record<string, unknown>> {
	const redis = createRedisClient(env);
	const vector = createVectorClient(env);
	const storedMutation = parseStoredObject(await redis.get(getMutationResultKey(params.mutationId)));
	if (storedMutation) {
		return storedMutation;
	}

	const entryType: EntryType = params.entryId.startsWith("pe_") ? "project" : "knowledge";
	if (
		entryType !== "knowledge" &&
		(params.currentView !== undefined || params.confidence !== undefined || params.state !== undefined)
	) {
		const result = {
			ok: false,
			error: "unsupported_entry_type",
			id: params.entryId,
			message: "current_view, confidence, and state updates are only supported for knowledge entries.",
		};
		await storeMutationResult(redis, params.mutationId, result);
		return result;
	}

	const rawEntry = await redis.get(getEntryKey(entryType, params.entryId));
	const entry = normalizeEntry(rawEntry, entryType);
	if (!entry) {
		const result = {
			ok: false,
			error: "entry_not_found",
			id: params.entryId,
		};
		await storeMutationResult(redis, params.mutationId, result);
		return result;
	}

	const metadata = (entry.metadata as Record<string, unknown> | undefined) ?? {};
	if (metadata.archived === true) {
		const result = {
			ok: false,
			error: "entry_archived",
			id: params.entryId,
		};
		await storeMutationResult(redis, params.mutationId, result);
		return result;
	}

	const currentRevision = toOptionalInteger(metadata.revision) ?? 0;
	if (params.expectedRevision !== currentRevision) {
		const result = {
			ok: false,
			error: "conflict",
			id: params.entryId,
			expected_revision: params.expectedRevision,
			actual_revision: currentRevision,
			current_summary: {
				state: typeof entry.state === "string" ? entry.state : "active",
				updated_at: getEntryUpdatedAt(entry, metadata),
			},
		};
		await storeMutationResult(redis, params.mutationId, result);
		return result;
	}

	if (
		params.currentView === undefined &&
		params.confidence === undefined &&
		params.state === undefined &&
		params.contextType === undefined
	) {
		const result = {
			ok: false,
			error: "invalid_request",
			message: "Provide at least one field to update.",
		};
		await storeMutationResult(redis, params.mutationId, result);
		return result;
	}

	const previousState = typeof entry.state === "string" ? entry.state : "active";
	const previousConfidence = typeof entry.confidence === "string" ? entry.confidence : "medium";
	const previousView = typeof entry.current_view === "string" ? entry.current_view : "";
	const previousContextType =
		typeof metadata.context_type === "string" ? metadata.context_type : "task_query";
	const timestamp = new Date().toISOString();

	if (params.currentView !== undefined && entryType === "knowledge") {
		entry.current_view = params.currentView;
	}
	if (params.confidence !== undefined && entryType === "knowledge") {
		entry.confidence = params.confidence;
	}
	if (params.state !== undefined && entryType === "knowledge") {
		entry.state = params.state;
	}
	if (params.contextType !== undefined) {
		metadata.context_type = params.contextType;
		metadata.classification_status = "manual_override";
		metadata.auto_inferred = false;
		metadata.injection_tier = defaultInjectionTier(params.contextType);
		appendConsolidationNote(
			metadata,
			formatConsolidationNote({
				timestamp,
				source: "operator",
				action: "set_context_type",
				detail: `${previousContextType} -> ${params.contextType} (${params.reason})`,
			}),
		);
	}

	if (entryType === "knowledge") {
		appendEvolutionNote(entry, timestamp, params.actorId, params.reason);
	}
	metadata.updated_at = timestamp;
	metadata.updated_by = {
		actor_id: params.actorId,
		tool: "update_entry",
	};
	metadata.revision = currentRevision + 1;
	entry.metadata = metadata;

	const loadedEntry = buildLoadedEntry(params.entryId, entryType, entry);

	const currentViewChanged =
		entryType === "knowledge" &&
		params.currentView !== undefined &&
		params.currentView !== previousView;
	const contextTypeChanged =
		params.contextType !== undefined && params.contextType !== previousContextType;
	const rebuildRunId = `update_${params.entryId}_${timestamp.replace(/[:.]/g, "-")}`;
	if (!(await acquireIndexRebuildLock(redis, rebuildRunId))) {
		throw new Error("index_rebuild_lock_held");
	}

	try {
		if (entryType === "knowledge" && params.state !== undefined && params.state !== previousState) {
			await redis.srem(`by_state:${previousState}`, params.entryId);
			await redis.sadd(`by_state:${params.state}`, params.entryId);
		}

		if (currentViewChanged) {
			const embedding = await getEmbedding(env, buildEntryEmbeddingText(loadedEntry));
			await persistEntry(redis, vector, loadedEntry, { embedding });
		} else {
			await persistEntry(redis, vector, loadedEntry);
		}

		await patchThinIndexEntry(redis, loadedEntry, timestamp);
	} finally {
		await releaseIndexRebuildLock(redis, rebuildRunId);
	}

	const result = {
		ok: true,
		id: params.entryId,
		type: entryType,
		mutation_id: params.mutationId,
		revision: metadata.revision,
		updated_at: timestamp,
		changes: {
			current_view_changed: currentViewChanged,
			confidence_changed:
				entryType === "knowledge" &&
				params.confidence !== undefined &&
				params.confidence !== previousConfidence,
			state_changed:
				entryType === "knowledge" &&
				params.state !== undefined &&
				params.state !== previousState,
			context_type_changed: contextTypeChanged,
		},
		side_effects: {
			vector: currentViewChanged ? "reembedded" : "metadata_updated",
		},
		entry,
	};

	await appendMutationLog(redis, {
		ts: timestamp,
		mutation_id: params.mutationId,
		tool: "update_entry",
		client: "mcp",
		actor_id: params.actorId,
		request_id: params.mutationId,
		ids_affected: [params.entryId],
		before_revisions: { [params.entryId]: currentRevision },
		after_revisions: { [params.entryId]: metadata.revision as number },
		reason: params.reason,
	});
	await storeMutationResult(redis, params.mutationId, result);

	return result;
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
		let [knowledgeBatch, projectBatch] = await Promise.all([
			loadEntryBatchByType(redis, "knowledge"),
			loadEntryBatchByType(redis, "project"),
		]);
		let knowledgeEntries = knowledgeBatch.entries;
		let projectEntries = projectBatch.entries;
		let allEntries = [...knowledgeEntries, ...projectEntries];
		const candidateIdFilter =
			options.candidateIds && options.candidateIds.length > 0
				? new Set(options.candidateIds)
				: null;

		const replayEntries = candidateIdFilter
			? allEntries.filter((entry) => candidateIdFilter.has(entry.id))
			: allEntries;
		const { duplicatePlans, contradictionPlans } = buildReplayPlans(replayEntries);
		const mergedEntries: Array<Record<string, unknown>> = [];
		const contradictionEntries: Array<Record<string, unknown>> = [];

		if (!options.dryRun) {
			for (const plan of duplicatePlans) {
				mergedEntries.push(await applyDuplicateMergePlan(redis, vector, plan, runId, startedAt));
			}

			if (contradictionPlans.length > 0) {
				const contradictionById = new Map<string, { entry: LoadedEntry; reasons: Set<string>; conflictingWith: Set<string> }>();
				for (const plan of contradictionPlans) {
					for (const entryId of plan.entryIds) {
						const entry = replayEntries.find((candidate) => candidate.id === entryId);
						if (!entry) continue;
						const existing = contradictionById.get(entryId) ?? {
							entry,
							reasons: new Set<string>(),
							conflictingWith: new Set<string>(),
						};
						for (const reason of plan.reasons) {
							existing.reasons.add(reason);
						}
						for (const relatedId of plan.entryIds) {
							if (relatedId !== entryId) {
								existing.conflictingWith.add(relatedId);
							}
						}
						contradictionById.set(entryId, existing);
					}
				}

				for (const { entry, reasons, conflictingWith } of contradictionById.values()) {
					contradictionEntries.push(
						await markEntryContested(
							redis,
							vector,
							entry,
							[...reasons].sort(),
							[...conflictingWith].sort(),
							runId,
							startedAt,
						),
					);
				}
			}

			if (mergedEntries.length > 0 || contradictionEntries.length > 0) {
				[knowledgeBatch, projectBatch] = await Promise.all([
					loadEntryBatchByType(redis, "knowledge"),
					loadEntryBatchByType(redis, "project"),
				]);
				knowledgeEntries = knowledgeBatch.entries;
				projectEntries = projectBatch.entries;
				allEntries = [...knowledgeEntries, ...projectEntries];
			}
		}

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
			if (
				mergedEntries.length > 0 ||
				contradictionEntries.length > 0 ||
				promotedEntries.length > 0 ||
				archivedEntries.length > 0
			) {
				await rebuildThinIndexSafely(redis, runId);
			}
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
					duplicate_candidate_count: duplicatePlans.length,
					duplicate_merge_count: mergedEntries.length,
					merged_entries: mergedEntries,
					contradiction_count: contradictionPlans.length,
					contradiction_entries: contradictionEntries,
					promotion_candidate_count: promotionCandidates.length,
					promoted_count: promotedEntries.length,
					promoted_entries: promotedEntries,
					deferred_items: [
						"temporal reference cleanup",
					],
				},
				consolidate: {
					status: options.dryRun ? "dry_run" : "completed",
					merged_count: mergedEntries.length,
					contradiction_count: contradictionEntries.length,
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
				duplicate_merge_candidates: duplicatePlans.length,
				merged_duplicates: mergedEntries.length,
				contradictions_detected: contradictionPlans.length,
				entries_marked_contested: contradictionEntries.length,
				promotion_candidates: promotionCandidates.length,
				promoted: promotedEntries.length,
				promotion_limit: options.promotionLimit ?? null,
				archive_limit: options.archiveLimit ?? null,
			},
			duplicate_plans: duplicatePlans.map((plan) => ({
				canonical_id: plan.canonical.id,
				type: plan.canonical.type,
				label: plan.canonical.label,
				duplicate_ids: plan.duplicates.map((entry) => entry.id),
			})),
			contradiction_plans: contradictionPlans.map((plan) => ({
				entry_ids: plan.entryIds,
				label: plan.label,
				reasons: plan.reasons,
			})),
			merged_entries: mergedEntries,
			contradiction_entries: contradictionEntries,
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
				? "Review duplicate merges, contradiction flags, and archive candidates before enabling live Dream writes."
				: "Review merged duplicates, contested entries, and archived entries to confirm Dream behavior.",
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
