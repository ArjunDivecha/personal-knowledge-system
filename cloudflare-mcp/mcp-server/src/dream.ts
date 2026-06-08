import { Redis } from "@upstash/redis/cloudflare";
import { Index } from "@upstash/vector";
import OpenAI from "openai";
import {
	assignTierByPercentile,
	computeSalience,
	defaultInjectionTier,
	MEMORY_POLICY,
	resolveStoredInjectionTier,
} from "./salience";
import { formatConsolidationNote } from "./consolidation";
import { recordDestructiveAction } from "./tripwires";
import {
	type JudgeQueueItem,
	type PendingVerdict,
	buildJudgeRubric,
	enqueueJudgeItem,
	isDuplicateMergeBorderline,
	readPendingVerdicts,
	settleJudgeItem,
} from "./judgeQueue";
import {
	PHASE9_DEFAULT_PROBE_SET_KEY,
	PHASE9_VALIDATION_GATE,
	buildPhase9RollbackRecommendation,
	buildPhase9ValidationGatePayload,
	evaluatePhase9OutcomeGate,
	evaluatePhase9OutcomeProbes,
	parsePhase9OutcomeProbes,
	type Phase9OutcomeEvalReport,
	type Phase9OutcomeGateReport,
	type Phase9OutcomeProbe,
} from "./phase9OutcomeGate";

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
	// Phase 1: see RunDreamProposalOptions.semantic.
	semantic?: boolean | null;
}

interface RunDreamProposalOptions {
	trigger: "manual" | "local_test";
	actorId: string;
	note?: string | null;
	candidateIds?: string[] | null;
	archiveLimit?: number | null;
	promotionLimit?: number | null;
	// Phase 1: run semantic (embedding-NN) dedup candidate generation. Defaults
	// to running only for targeted (candidate_ids) proposals; pass true to force
	// it on an unfiltered run (bounded by SEMANTIC_DEDUP_MAX_QUERIES).
	semantic?: boolean | null;
}

interface ApplyDreamProposalOptions {
	proposalId: string;
	mutationId: string;
	actorId: string;
	reason: string;
	operationIds?: string[] | null;
	requireGradePass?: boolean | null;
	gradeId?: string | null;
	phase9OutcomeGate?: boolean | null;
	phase9AutoRollback?: boolean | null;
	phase9ProbeSetKey?: string | null;
	phase9Probes?: unknown[] | null;
	phase9WriteValidationLedger?: boolean | null;
}

interface GradeDreamProposalOptions {
	proposalId: string;
	actorId: string;
	rubricVersion?: string | null;
}

interface RollbackDreamApplyOptions {
	proposalId: string;
	applyMutationId: string;
	rollbackMutationId: string;
	actorId: string;
	reason: string;
	operationIds?: string[] | null;
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
	// Phase 1 semantic entity resolution: true when the group was formed by
	// embedding similarity without exact-fingerprint agreement across all
	// members. Semantic-only plans are always judge/operator-gated (R1.6).
	semanticOnly?: boolean;
	// Max pairwise cosine observed within the group (1.0 for lexical-exact).
	maxCosine?: number;
}

interface ContradictionPlan {
	entryIds: string[];
	label: string;
	reasons: string[];
	proposalKind?: string;
	operationId?: string;
	evidence?: Record<string, unknown>;
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
const DREAM_RUN_INDEX_KEY = "dream:runs:index";
const DREAM_LAST_PROPOSAL_KEY = "dream:proposal:last";
const CORRECTION_CONTEST_HINT_PREFIX = "dream:contest_hint:";
const ARCHIVED_PREFIX = "archived";
const DREAM_LOCK_TTL_SECONDS = 30 * 60;
const DREAM_LOCK_STALE_AFTER_SECONDS = 5 * 60;
const DREAM_SAMPLE_LIMIT = 25;
const DREAM_STORAGE_SAMPLE_LIMIT = 10;
const DREAM_STORAGE_FALLBACK_SAMPLE_LIMIT = 3;
const DREAM_STORAGE_MAX_BYTES = 9 * 1024 * 1024;
const DREAM_STORAGE_MAX_REQUEST_BYTES = 10 * 1024 * 1024 - 64 * 1024;
const DREAM_SCAN_COUNT = 200;
const DREAM_MGET_BATCH_SIZE = 25;
const INDEX_REBUILD_LOCK_KEY = "index:rebuild:lock";
const INDEX_REBUILD_LOCK_TTL_SECONDS = 5 * 60;
const THIN_INDEX_STAGING_PREFIX = "index:staging:";
const MUTATION_LOG_KEY = "mutation_log";
const MUTATION_LOG_LIMIT = 1000;
const MUTATION_RESULT_PREFIX = "mutation_result:";
const MUTATION_RESULT_TTL_SECONDS = 72 * 60 * 60;
const VALIDATION_LAST_KEY = "validation:last";
const VALIDATION_GATE_STATUS_KEY = "validation:gate_status";
const VALIDATION_HISTORY_PREFIX = "validation:history:";
const VALIDATION_HISTORY_LIMIT = 100;
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

const DREAM_STORAGE_SAMPLED_FIELDS = [
	"duplicate_plans",
	"contradiction_plans",
	"merged_entries",
	"contradiction_entries",
	"archive_candidates",
	"archived_entries",
	"promoted_entries",
	"archive_candidates_sample",
] as const;

const DREAM_REPLAY_DETAIL_FIELDS = [
	"merged_entries",
	"contradiction_entries",
	"promoted_entries",
] as const;

interface DreamRunStorageOptions {
	maxBytes?: number;
	sampleLimit?: number;
	fallbackSampleLimit?: number;
}

interface DreamLockState {
	run_id?: string;
	run_at?: string;
	trigger?: string;
	dry_run?: boolean;
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

function toObjectArray(value: unknown): Record<string, unknown>[] {
	if (!Array.isArray(value)) return [];
	return value.filter(
		(item): item is Record<string, unknown> =>
			Boolean(item) && typeof item === "object" && !Array.isArray(item),
	);
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
		(typeof metadata.updated_at === "string" && metadata.updated_at) ||
		(typeof metadata.last_seen === "string" && metadata.last_seen) ||
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
		// Layer 2 quarantine: suppresses auto-injection without changing tier.
		// Cleared by any retrieval reinforcement.
		injection_quarantine: Boolean(metadata.injection_quarantine),
		quarantined_at:
			typeof metadata.quarantined_at === "string" ? metadata.quarantined_at : null,
		// Layer 2 demote streak: counts consecutive nights below tier threshold.
		// Stored so quarantine + demote rules can fire deterministically.
		quarantine_streak_nights: toOptionalInteger(metadata.quarantine_streak_nights) ?? 0,
	};
	normalizedMetadata.injection_tier = resolveStoredInjectionTier(normalizedMetadata);

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
	const sourceConversations = toStringArray(entry.metadata.source_conversations);
	const base = {
		type: entry.type,
		archived: Boolean(entry.metadata.archived),
		classification_status:
			typeof entry.metadata.classification_status === "string" && entry.metadata.classification_status.length > 0
				? entry.metadata.classification_status
				: "pending",
		context_type: entry.metadata.context_type,
		injection_tier: entry.injectionTier,
		salience_score: entry.metadata.salience_score,
		mention_count: entry.metadata.mention_count,
		last_consolidated: entry.metadata.last_consolidated,
		updated_at: entry.updatedAt,
		...(sourceConversations.length === 1
			? { source: sourceConversations[0] }
			: sourceConversations.length > 1
				? { source: sourceConversations.slice(0, 3).join(",") }
				: {}),
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

	return null;
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

// ===========================================================================
// Phase 1 — Semantic Entity Resolution (PRD docs/pks-memory-quality-...md §7)
// ===========================================================================
// De-duplication by embedding similarity, not just title equality. Reuses the
// existing safety gates: opposing-marker veto routes contradictory pairs to
// mark_contested; compareCanonicalPriority picks the canonical; mergeCanonical
// Entry performs the merge. Semantic-only groups are judge/operator-gated.
//
// The neighbour lookup is injected (NeighborFn) so the grouping/classification
// logic is unit-testable without a live vector store.

export interface SemanticDedupConfig {
	cosineThreshold: number;   // COSINE_DUP_THRESHOLD
	neighborK: number;         // DEDUP_NEIGHBOR_K
	maxQueries: number;        // SEMANTIC_DEDUP_MAX_QUERIES (Worker subrequest cap)
	maxClusterSize: number;    // SEMANTIC_MAX_CLUSTER_SIZE (over-merge guard)
}

export interface NeighborHit {
	id: string;
	score: number;
}

// Given an entry, return its nearest neighbours (id + cosine) of the SAME type.
export type NeighborFn = (entry: LoadedEntry) => Promise<NeighborHit[]>;

interface DedupEdge {
	a: string;
	b: string;
	cosine: number;
}

// --- Pure union-find over edges (exported for tests) ----------------------
export function connectedComponents(
	ids: string[],
	edges: Array<{ a: string; b: string }>,
): string[][] {
	const parent = new Map<string, string>();
	const find = (x: string): string => {
		parent.set(x, parent.get(x) ?? x);
		let root = x;
		while (parent.get(root) !== root) root = parent.get(root)!;
		// path compression
		let cur = x;
		while (parent.get(cur) !== root) {
			const next = parent.get(cur)!;
			parent.set(cur, root);
			cur = next;
		}
		return root;
	};
	const union = (a: string, b: string) => {
		const ra = find(a);
		const rb = find(b);
		if (ra !== rb) parent.set(ra, rb);
	};
	for (const id of ids) find(id);
	for (const { a, b } of edges) {
		if (parent.has(a) && parent.has(b)) union(a, b);
	}
	const groups = new Map<string, string[]>();
	for (const id of parent.keys()) {
		const root = find(id);
		const arr = groups.get(root) ?? [];
		arr.push(id);
		groups.set(root, arr);
	}
	return [...groups.values()];
}

// --- Classify a connected component into a dup plan or a contradiction -----
// Returns either a DuplicateMergePlan or a ContradictionPlan-shaped object.
// Pure: depends only on the entries + existing predicates. Exported for tests.
export function classifyDuplicateComponent(
	members: LoadedEntry[],
	pairCosine: Map<string, number>,
): { kind: "duplicate"; plan: DuplicateMergePlan } | { kind: "contradiction"; plan: ContradictionPlan } | null {
	if (members.length < 2) return null;

	// Opposing-marker veto across any pair → the whole component is contested,
	// never merged. A high embedding score must not override a contradiction.
	const reasons = new Set<string>();
	for (let i = 0; i < members.length; i += 1) {
		for (let j = i + 1; j < members.length; j += 1) {
			if (members[i].type === "knowledge" && members[j].type === "knowledge") {
				const reason = findOpposingMarkerReason(
					getNarrativeText(members[i]),
					getNarrativeText(members[j]),
				);
				if (reason) reasons.add(reason);
			}
		}
	}
	if (reasons.size > 0) {
		return {
			kind: "contradiction",
			plan: {
				entryIds: members.map((m) => m.id),
				label: members[0].label,
				reasons: [...reasons],
			},
		};
	}

	// Otherwise a merge. Canonical = existing priority order.
	const ordered = [...members].sort(compareCanonicalPriority);
	// semanticOnly = members do NOT all share one exact fingerprint.
	const fingerprints = new Set(ordered.map((m) => getDuplicateFingerprint(m) ?? `__null__:${m.id}`));
	const semanticOnly = fingerprints.size > 1;

	let maxCosine = 0;
	for (let i = 0; i < ordered.length; i += 1) {
		for (let j = i + 1; j < ordered.length; j += 1) {
			const key = ordered[i].id < ordered[j].id
				? `${ordered[i].id}|${ordered[j].id}`
				: `${ordered[j].id}|${ordered[i].id}`;
			maxCosine = Math.max(maxCosine, pairCosine.get(key) ?? (semanticOnly ? 0 : 1));
		}
	}

	return {
		kind: "duplicate",
		plan: {
			fingerprint: getDuplicateFingerprint(ordered[0]) ?? ordered[0].id,
			canonical: ordered[0],
			duplicates: ordered.slice(1),
			semanticOnly,
			maxCosine: Math.round(maxCosine * 10000) / 10000,
		},
	};
}

// Bound a promise; reject if it doesn't settle within ms.
function withTimeout<T>(p: Promise<T>, ms: number, label: string): Promise<T> {
	return new Promise<T>((resolve, reject) => {
		const timer = setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms);
		p.then(
			(v) => { clearTimeout(timer); resolve(v); },
			(e) => { clearTimeout(timer); reject(e); },
		);
	});
}

const SEMANTIC_PROBE_TIMEOUT_MS = 1500;
const SEMANTIC_CALL_TIMEOUT_MS = 4000;

// --- Build semantic edges via the injected neighbour function -------------
// Fail-open: a single health probe runs first; if the neighbour lookup is
// unavailable (unreachable vector store, missing capability), semantic dedup
// is disabled for the run and the caller falls back to lexical-only.
async function buildSemanticEdges(
	entries: LoadedEntry[],
	neighborFn: NeighborFn,
	config: SemanticDedupConfig,
): Promise<{ edges: DedupEdge[]; capped: boolean; queriesRun: number; disabled: boolean }> {
	const byId = new Map(entries.map((e) => [e.id, e]));
	const queryEntries = entries.slice(0, Math.max(0, config.maxQueries));
	const capped = entries.length > queryEntries.length;
	const edges: DedupEdge[] = [];
	let queriesRun = 0;

	if (queryEntries.length === 0) {
		return { edges, capped, queriesRun, disabled: false };
	}

	// Health probe on the first entry. If it fails fast, skip semantic entirely.
	try {
		await withTimeout(neighborFn(queryEntries[0]), SEMANTIC_PROBE_TIMEOUT_MS, "semantic probe");
	} catch {
		return { edges: [], capped: false, queriesRun: 0, disabled: true };
	}

	for (const entry of queryEntries) {
		queriesRun += 1;
		let hits: NeighborHit[];
		try {
			hits = await withTimeout(neighborFn(entry), SEMANTIC_CALL_TIMEOUT_MS, "semantic query");
		} catch {
			continue;
		}
		for (const hit of hits) {
			if (hit.id === entry.id) continue;
			const other = byId.get(hit.id);
			if (!other) continue;                 // only merge among loaded entries
			if (other.type !== entry.type) continue;
			if (hit.score < config.cosineThreshold) continue;
			edges.push({ a: entry.id, b: hit.id, cosine: hit.score });
		}
	}
	return { edges, capped, queriesRun, disabled: false };
}

// --- Full replay-plan builder: lexical (existing) ∪ semantic (new) --------
export async function buildReplayPlansWithSemantic(
	entries: LoadedEntry[],
	neighborFn: NeighborFn | null,
	config: SemanticDedupConfig,
): Promise<{
	duplicatePlans: DuplicateMergePlan[];
	contradictionPlans: ContradictionPlan[];
	semantic: {
		enabled: boolean;
		capped: boolean;
		queriesRun: number;
		edges: number;
		oversized_clusters?: number;
		oversized_samples?: Array<{ size: number; sample_domains: string[] }>;
		max_cluster_size?: number;
		disabled_reason?: string;
	};
}> {
	// 1. Lexical edges: entries sharing an exact fingerprint.
	const fingerprintGroups = new Map<string, string[]>();
	for (const entry of entries) {
		const fp = getDuplicateFingerprint(entry);
		if (!fp) continue;
		const arr = fingerprintGroups.get(fp) ?? [];
		arr.push(entry.id);
		fingerprintGroups.set(fp, arr);
	}
	const lexicalEdges: Array<{ a: string; b: string }> = [];
	for (const ids of fingerprintGroups.values()) {
		for (let i = 1; i < ids.length; i += 1) {
			lexicalEdges.push({ a: ids[0], b: ids[i] });
		}
	}

	// 2. Semantic edges (if a neighbour function is available).
	let semanticEdges: DedupEdge[] = [];
	let capped = false;
	let queriesRun = 0;
	let semanticDisabled = false;
	if (neighborFn && config.maxQueries > 0) {
		const result = await buildSemanticEdges(entries, neighborFn, config);
		semanticEdges = result.edges;
		capped = result.capped;
		queriesRun = result.queriesRun;
		semanticDisabled = result.disabled;
	}

	const pairCosine = new Map<string, number>();
	for (const e of semanticEdges) {
		const key = e.a < e.b ? `${e.a}|${e.b}` : `${e.b}|${e.a}`;
		pairCosine.set(key, Math.max(pairCosine.get(key) ?? 0, e.cosine));
	}

	// 3. Unify lexical + semantic edges, find components.
	const allEdges = [
		...lexicalEdges,
		...semanticEdges.map((e) => ({ a: e.a, b: e.b })),
	];
	const ids = entries.map((e) => e.id);
	const byId = new Map(entries.map((e) => [e.id, e]));
	const components = connectedComponents(ids, allEdges);

	// 4. Classify each multi-member component.
	//    Over-merge guard: union-find transitive closure can chain unrelated
	//    entries (A~B~C~…) into one giant component even though the ends are
	//    dissimilar. Components larger than maxClusterSize are NOT merged
	//    semantically; instead we fall back to LEXICAL-only sub-grouping
	//    (exact-fingerprint subgroups still merge safely) and flag the
	//    oversized component for threshold tuning (PRD open question #1).
	const duplicatePlans: DuplicateMergePlan[] = [];
	const contradictionPlans: ContradictionPlan[] = [];
	const oversizedClusters: Array<{ size: number; sample_domains: string[] }> = [];
	const maxClusterSize = config.maxClusterSize > 0 ? config.maxClusterSize : 6;

	const classifyAndCollect = (members: LoadedEntry[]) => {
		const classified = classifyDuplicateComponent(members, pairCosine);
		if (!classified) return;
		if (classified.kind === "duplicate") duplicatePlans.push(classified.plan);
		else contradictionPlans.push(classified.plan);
	};

	for (const comp of components) {
		if (comp.length < 2) continue;
		const members = comp.map((id) => byId.get(id)!).filter(Boolean);
		if (members.length <= maxClusterSize) {
			classifyAndCollect(members);
			continue;
		}
		// Oversized: drop cross-topic semantic edges; merge only exact-fingerprint
		// subgroups within this component.
		oversizedClusters.push({
			size: members.length,
			sample_domains: members.slice(0, 5).map((m) => m.label),
		});
		const byFingerprint = new Map<string, LoadedEntry[]>();
		for (const m of members) {
			const fp = getDuplicateFingerprint(m);
			if (!fp) continue;
			const arr = byFingerprint.get(fp) ?? [];
			arr.push(m);
			byFingerprint.set(fp, arr);
		}
		for (const subgroup of byFingerprint.values()) {
			if (subgroup.length >= 2) classifyAndCollect(subgroup);
		}
	}

	// 5. Preserve single-entry internal-contradiction detection from the
	//    original lexical path.
	for (const entry of entries) {
		const reason = detectInternalContradictionReason(entry);
		if (!reason) continue;
		contradictionPlans.push({ entryIds: [entry.id], label: entry.label, reasons: [reason] });
	}

	return {
		duplicatePlans,
		contradictionPlans,
		semantic: {
			enabled: Boolean(neighborFn && config.maxQueries > 0) && !semanticDisabled,
			capped,
			queriesRun,
			edges: semanticEdges.length,
			oversized_clusters: oversizedClusters.length,
			oversized_samples: oversizedClusters.slice(0, 5),
			max_cluster_size: maxClusterSize,
			...(semanticDisabled ? { disabled_reason: "neighbour lookup unavailable (fail-open to lexical)" } : {}),
		},
	};
}

// --- Real neighbour function backed by Upstash Vector ---------------------
// Fetches the entry's stored embedding (never re-embeds) then queries NN.
// Caches fetched vectors per run to avoid duplicate fetches.
function makeVectorNeighborFn(
	vector: Index,
	config: SemanticDedupConfig,
	vectorCache: Map<string, number[] | null>,
): NeighborFn {
	return async (entry: LoadedEntry): Promise<NeighborHit[]> => {
		let vec = vectorCache.get(entry.id);
		if (vec === undefined) {
			try {
				const fetched = await vector.fetch([entry.id], { includeVectors: true });
				const row = Array.isArray(fetched) ? fetched[0] : null;
				vec = row && Array.isArray((row as { vector?: number[] }).vector)
					? ((row as { vector: number[] }).vector)
					: null;
			} catch {
				vec = null;
			}
			vectorCache.set(entry.id, vec);
		}
		if (!vec) return [];
		const results = await vector.query({
			vector: vec,
			topK: config.neighborK + 5,
			includeMetadata: false,
		});
		return results.map((r) => ({ id: String(r.id), score: Number(r.score ?? 0) }));
	};
}

function readSemanticDedupConfig(): SemanticDedupConfig {
	const dedup = (MEMORY_POLICY as Record<string, unknown>).dedup as Record<string, unknown> | undefined;
	return {
		cosineThreshold: typeof dedup?.COSINE_DUP_THRESHOLD === "number" ? dedup.COSINE_DUP_THRESHOLD : 0.86,
		neighborK: typeof dedup?.DEDUP_NEIGHBOR_K === "number" ? dedup.DEDUP_NEIGHBOR_K : 10,
		maxQueries: typeof dedup?.SEMANTIC_DEDUP_MAX_QUERIES === "number" ? dedup.SEMANTIC_DEDUP_MAX_QUERIES : 400,
		maxClusterSize: typeof dedup?.SEMANTIC_MAX_CLUSTER_SIZE === "number" ? dedup.SEMANTIC_MAX_CLUSTER_SIZE : 6,
	};
}

function safeOperationIdPart(value: string): string {
	return value.replace(/[^a-zA-Z0-9_:-]/g, "_").slice(0, 120);
}

async function loadCorrectionContestPlans(
	redis: Redis,
	entriesById: Map<string, LoadedEntry>,
	alreadyContestedEntryIds: Set<string>,
): Promise<ContradictionPlan[]> {
	const keys = await scanKeys(redis, `${CORRECTION_CONTEST_HINT_PREFIX}*`);
	if (keys.length === 0) return [];

	const hints = await mgetBatched<unknown>(redis, keys);
	const plans: ContradictionPlan[] = [];
	for (let index = 0; index < keys.length; index += 1) {
		const key = keys[index];
		const hint = parseStoredObject(hints[index]);
		if (!hint) continue;
		const status = typeof hint.status === "string" ? hint.status : "pending";
		if (status !== "pending") continue;
		if (hint.proposal_kind !== "contest" || hint.source !== "correction_event") continue;

		const entryId = typeof hint.target_entry_id === "string" ? hint.target_entry_id : null;
		const eventId = typeof hint.event_id === "string" ? hint.event_id : key.slice(CORRECTION_CONTEST_HINT_PREFIX.length);
		if (!entryId || alreadyContestedEntryIds.has(entryId)) continue;

		const entry = entriesById.get(entryId);
		if (!entry || entry.type !== "knowledge" || getTopicState(entry.entry) !== "active") continue;

		const correctedBelief = typeof hint.corrected_belief === "string" ? hint.corrected_belief : "";
		const newBelief = typeof hint.new_belief === "string" ? hint.new_belief : "";
		const reason = typeof hint.reason === "string" && hint.reason.length > 0
			? hint.reason
			: "user correction contradicts prior memory";
		plans.push({
			entryIds: [entryId],
			label: entry.label,
			reasons: [reason],
			proposalKind: "contest",
			operationId: `dop_contest_${safeOperationIdPart(eventId)}_${safeOperationIdPart(entryId)}`,
			evidence: {
				correction_hint_key: key,
				event_id: eventId,
				conversation_id: hint.conversation_id ?? null,
				message_id: hint.message_id ?? null,
				corrected_belief: correctedBelief,
				new_belief: newBelief,
				correction_confidence: hint.correction_confidence ?? null,
				judge_confidence: hint.judge_confidence ?? null,
				similarity: hint.similarity ?? null,
				reasons: [reason],
				target: summarizeProposalEntry(entry),
			},
		});
	}

	return plans;
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

async function mgetBatched<T>(redis: Redis, keys: string[], batchSize = DREAM_MGET_BATCH_SIZE): Promise<T[]> {
	if (keys.length === 0) {
		return [];
	}

	const results: T[] = [];
	for (let index = 0; index < keys.length; index += batchSize) {
		const batch = keys.slice(index, index + batchSize);
		const values = await redis.mget<T[]>(batch);
		results.push(...values);
	}
	return results;
}

async function loadEntryBatchByType(
	redis: Redis,
	entryType: EntryType,
): Promise<{ entries: LoadedEntry[]; archivedCount: number }> {
	const keys = await scanKeys(redis, `${entryType}:*`);
	if (keys.length === 0) return { entries: [], archivedCount: 0 };

	const rawEntries = await mgetBatched<unknown>(redis, keys);
	const normalizedEntries = rawEntries
		.map((rawEntry) => normalizeEntry(rawEntry, entryType))
		.filter((entry): entry is Record<string, unknown> => entry !== null);
	const ids = normalizedEntries
		.map((entry) => (typeof entry.id === "string" ? entry.id : null))
		.filter((entryId): entryId is string => entryId !== null);

	const [accessCounts, lastAccessedValues] = await Promise.all([
		ids.length > 0 ? mgetBatched<unknown>(redis, ids.map(getEntryAccessKey)) : Promise.resolve([]),
		ids.length > 0 ? mgetBatched<unknown>(redis, ids.map(getEntryLastAccessedKey)) : Promise.resolve([]),
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

export function isArchiveCandidate(entry: LoadedEntry): boolean {
	// Phase 4 (R4.2): admit any zero-access, single-source, low-salience entry
	// regardless of context_type — EXCEPT protected identity/explicit-save
	// types, which are never auto-archived. Broadened from the old
	// task_query/passing_reference-only rule so prune can keep pace with intake.
	const protectedTypes: string[] =
		(MEMORY_POLICY.dream_thresholds as Record<string, unknown>).archive_protected_context_types as string[] ?? [];
	if (protectedTypes.includes(entry.contextType)) return false;
	// Never archive an entry that is contested/flagged for operator review —
	// archiving it would silently resolve a contradiction the operator hasn't
	// seen. Covers entries contested in a prior run (state persisted) and, in
	// the live cycle, entries marked contested earlier this run (the cycle
	// reloads the snapshot after marking, so their state reads "contested").
	if (typeof entry.entry.state === "string" && entry.entry.state === "contested") return false;
	return (
		entry.accessCount === 0 &&
		entry.sourceConversationCount <= 1 &&
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

function jsonSizeBytes(value: unknown): number {
	return new TextEncoder().encode(JSON.stringify(value)).length;
}

function estimateRedisSetRequestBytes(key: string, serializedValue: string): number {
	return jsonSizeBytes(["set", key, serializedValue]);
}

function cloneJsonRecord<T extends Record<string, unknown>>(value: T): T {
	return JSON.parse(JSON.stringify(value)) as T;
}

function compactStoredValue(value: unknown, depth = 0): unknown {
	if (typeof value === "string") {
		return truncate(value, depth === 0 ? 280 : 180);
	}
	if (
		value === null ||
		typeof value === "number" ||
		typeof value === "boolean"
	) {
		return value;
	}
	if (Array.isArray(value)) {
		const nestedLimit = depth === 0 ? 5 : 3;
		return value.slice(0, nestedLimit).map((item) => compactStoredValue(item, depth + 1));
	}
	if (value && typeof value === "object") {
		const entries = Object.entries(value as Record<string, unknown>);
		const compacted: Record<string, unknown> = {};
		for (const [key, nestedValue] of entries.slice(0, 12)) {
			compacted[key] = compactStoredValue(nestedValue, depth + 1);
		}
		if (entries.length > 12) {
			compacted.__truncated_field_count = entries.length - 12;
		}
		return compacted;
	}
	return null;
}

function sampleStoredArray(values: unknown[], sampleLimit: number): unknown[] {
	return values.slice(0, sampleLimit).map((item) => compactStoredValue(item));
}

function buildMinimalStoredPhases(phases: Record<string, unknown>): Record<string, unknown> {
	const minimalPhases: Record<string, unknown> = {};
	for (const [phaseName, phaseRaw] of Object.entries(phases)) {
		const phase = parseStoredObject(phaseRaw);
		if (!phase) continue;
		const minimalPhase: Record<string, unknown> = {};
		for (const [key, value] of Object.entries(phase)) {
			if (typeof value === "number" || typeof value === "boolean" || value === null) {
				minimalPhase[key] = value;
				continue;
			}
			if (typeof value === "string") {
				minimalPhase[key] = truncate(value, 220);
			}
		}
		minimalPhases[phaseName] = minimalPhase;
	}
	return minimalPhases;
}

function buildMinimalStoredRunRecord(
	runRecord: Record<string, unknown>,
	compaction: Record<string, unknown>,
	fallbackSampleLimit: number,
): Record<string, unknown> {
	const phases = parseStoredObject(runRecord.phases) ?? {};
	const minimalRecord: Record<string, unknown> = {
		schema_version: runRecord.schema_version ?? 1,
		run_id: runRecord.run_id ?? null,
		run_at: runRecord.run_at ?? null,
		completed_at: runRecord.completed_at ?? null,
		status: runRecord.status ?? null,
		dry_run: runRecord.dry_run ?? null,
		trigger: runRecord.trigger ?? null,
		cron: runRecord.cron ?? null,
		scheduled_time: runRecord.scheduled_time ?? null,
		note: typeof runRecord.note === "string" ? truncate(runRecord.note, 280) : runRecord.note ?? null,
		error: typeof runRecord.error === "string" ? truncate(runRecord.error, 360) : runRecord.error ?? null,
		next_action:
			typeof runRecord.next_action === "string"
				? truncate(runRecord.next_action, 280)
				: runRecord.next_action ?? null,
		counts: parseStoredObject(runRecord.counts) ?? {},
		phases: buildMinimalStoredPhases(phases),
	};

	for (const field of DREAM_STORAGE_SAMPLED_FIELDS) {
		const raw = runRecord[field];
		if (Array.isArray(raw) && raw.length > 0) {
			minimalRecord[field] = sampleStoredArray(raw, fallbackSampleLimit);
		}
	}

	minimalRecord.storage_compaction = {
		...compaction,
		mode: "minimal",
		fallback_sample_limit: fallbackSampleLimit,
	};
	return minimalRecord;
}

export function compactDreamRunRecordForStorage(
	runRecord: Record<string, unknown>,
	options: DreamRunStorageOptions = {},
): Record<string, unknown> {
	const maxBytes = options.maxBytes ?? DREAM_STORAGE_MAX_BYTES;
	const sampleLimit = options.sampleLimit ?? DREAM_STORAGE_SAMPLE_LIMIT;
	const fallbackSampleLimit = options.fallbackSampleLimit ?? DREAM_STORAGE_FALLBACK_SAMPLE_LIMIT;
	const originalSize = jsonSizeBytes(runRecord);

	if (originalSize <= maxBytes) {
		return runRecord;
	}

	const storedRecord = cloneJsonRecord(runRecord);
	const compaction: Record<string, unknown> = {
		mode: "sampled",
		max_bytes: maxBytes,
		original_size_bytes: originalSize,
		sample_limit: sampleLimit,
		sampled_fields: {} as Record<string, unknown>,
		removed_fields: [] as string[],
	};

	const phases = parseStoredObject(storedRecord.phases);
	const replay = phases ? parseStoredObject(phases.replay) : null;
	if (phases && replay) {
		for (const field of DREAM_REPLAY_DETAIL_FIELDS) {
			if (Array.isArray(replay[field])) {
				delete replay[field];
				((compaction.removed_fields as string[]) ?? []).push(`phases.replay.${field}`);
			}
		}
		phases.replay = replay;
		storedRecord.phases = phases;
	}

	for (const field of DREAM_STORAGE_SAMPLED_FIELDS) {
		const raw = storedRecord[field];
		if (!Array.isArray(raw)) continue;
		const sampled = sampleStoredArray(raw, sampleLimit);
		storedRecord[field] = sampled;
		((compaction.sampled_fields as Record<string, unknown>) ?? {})[field] = {
			total_count: raw.length,
			stored_count: sampled.length,
		};
	}

	if (typeof storedRecord.next_action === "string") {
		storedRecord.next_action = truncate(storedRecord.next_action, 280);
	}

	storedRecord.storage_compaction = compaction;
	const sampledSize = jsonSizeBytes(storedRecord);
	if (sampledSize <= maxBytes) {
		(storedRecord.storage_compaction as Record<string, unknown>).stored_size_bytes = sampledSize;
		return storedRecord;
	}

	const minimalRecord = buildMinimalStoredRunRecord(runRecord, compaction, fallbackSampleLimit);
	const minimalSize = jsonSizeBytes(minimalRecord);
	(minimalRecord.storage_compaction as Record<string, unknown>).stored_size_bytes = minimalSize;
	return minimalRecord;
}

async function writeRunRecord(
	redis: Redis,
	runRecord: Record<string, unknown>,
	setAsLatest: boolean,
): Promise<void> {
	const runId = String(runRecord.run_id);
	const keys = [
		`${DREAM_RUN_PREFIX}${runId}`,
		DREAM_LAST_ATTEMPT_KEY,
		...(setAsLatest ? [DREAM_LAST_RUN_KEY] : []),
	];

	const storedRunRecord = compactDreamRunRecordForStorage(runRecord, {
		maxBytes: 1,
		sampleLimit: 1,
		fallbackSampleLimit: 1,
	});
	const serialized = JSON.stringify(storedRunRecord);
	const maxRequestBytes = Math.max(...keys.map((key) => estimateRedisSetRequestBytes(key, serialized)));

	if (maxRequestBytes > DREAM_STORAGE_MAX_REQUEST_BYTES) {
		throw new Error(
			`Dream audit record still exceeds Redis request budget after compaction (${maxRequestBytes} bytes).`,
		);
	}

	await redis.set(`${DREAM_RUN_PREFIX}${runId}`, serialized);
	await redis.set(DREAM_LAST_ATTEMPT_KEY, serialized);
	if (setAsLatest) {
		await redis.set(DREAM_LAST_RUN_KEY, serialized);
	}
}

async function updateDreamRunIndex(redis: Redis, runId: string, maxRuns = 50): Promise<void> {
	const rawIndex = await redis.get(DREAM_RUN_INDEX_KEY);
	const existing = Array.isArray(rawIndex)
		? rawIndex
		: typeof rawIndex === "string"
			? (() => {
				try {
					const parsed = JSON.parse(rawIndex);
					return Array.isArray(parsed) ? parsed : [];
				} catch {
					return [];
				}
			})()
			: [];
	const nextIndex = [
		runId,
		...existing.filter((value): value is string => typeof value === "string" && value !== runId),
	].slice(0, maxRuns);
	await redis.set(DREAM_RUN_INDEX_KEY, JSON.stringify(nextIndex));
}

function isDreamLockStale(lockState: DreamLockState | null, nowMs: number = Date.now()): boolean {
	if (!lockState) {
		return true;
	}

	const runAt = typeof lockState.run_at === "string" ? Date.parse(lockState.run_at) : Number.NaN;
	if (!Number.isFinite(runAt)) {
		return true;
	}

	return nowMs - runAt >= DREAM_LOCK_STALE_AFTER_SECONDS * 1000;
}

async function acquireDreamLock(
	redis: Redis,
	lockPayload: string,
	nowMs: number,
): Promise<{ acquired: boolean; existingLock: DreamLockState | null; reclaimedStaleLock: boolean }> {
	const initialAttempt = await redis.set(DREAM_LOCK_KEY, lockPayload, {
		nx: true,
		ex: DREAM_LOCK_TTL_SECONDS,
	});
	if (initialAttempt) {
		return { acquired: true, existingLock: null, reclaimedStaleLock: false };
	}

	const existingLock = parseStoredObject(await redis.get(DREAM_LOCK_KEY)) as DreamLockState | null;
	if (!isDreamLockStale(existingLock, nowMs)) {
		return { acquired: false, existingLock, reclaimedStaleLock: false };
	}

	await redis.del(DREAM_LOCK_KEY);
	const retryAttempt = await redis.set(DREAM_LOCK_KEY, lockPayload, {
		nx: true,
		ex: DREAM_LOCK_TTL_SECONDS,
	});
	if (retryAttempt) {
		return { acquired: true, existingLock, reclaimedStaleLock: true };
	}

	return {
		acquired: false,
		existingLock: (parseStoredObject(await redis.get(DREAM_LOCK_KEY)) as DreamLockState | null) ?? existingLock,
		reclaimedStaleLock: false,
	};
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
	entry.injectionTier = resolveStoredInjectionTier(entry.metadata);
	entry.metadata.injection_tier = entry.injectionTier;
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

function getPhase9OutcomeAuditKey(proposalId: string, applyMutationId: string): string {
	return `${DREAM_RUN_PREFIX}${proposalId}:phase9:${applyMutationId}`;
}

async function loadPhase9OutcomeProbes(
	redis: Redis,
	options: ApplyDreamProposalOptions,
): Promise<Phase9OutcomeProbe[]> {
	if (options.phase9Probes && options.phase9Probes.length > 0) {
		return parsePhase9OutcomeProbes(options.phase9Probes);
	}
	const probeSetKey = options.phase9ProbeSetKey ?? PHASE9_DEFAULT_PROBE_SET_KEY;
	const raw = await redis.get(probeSetKey);
	return parsePhase9OutcomeProbes(raw);
}

async function evaluateStoredPhase9OutcomeProbes(
	redis: Redis,
	probes: Phase9OutcomeProbe[],
): Promise<Phase9OutcomeEvalReport> {
	const [knowledgeEntries, projectEntries] = await Promise.all([
		loadEntriesByType(redis, "knowledge"),
		loadEntriesByType(redis, "project"),
	]);
	return evaluatePhase9OutcomeProbes(
		probes,
		[...knowledgeEntries, ...projectEntries].map((entry) => ({
			id: entry.id,
			type: entry.type,
			entry: entry.entry,
			metadata: entry.metadata,
			label: entry.label,
			summary: getNarrativeText(entry),
		})),
	);
}

async function storePhase9OutcomeAudit(
	redis: Redis,
	proposalId: string,
	applyMutationId: string,
	payload: Record<string, unknown>,
): Promise<void> {
	await redis.set(
		getPhase9OutcomeAuditKey(proposalId, applyMutationId),
		JSON.stringify(payload),
	);
}

async function writePhase9ValidationGate(
	redis: Redis,
	report: Phase9OutcomeGateReport,
): Promise<Record<string, unknown>> {
	const payload = buildPhase9ValidationGatePayload(report);
	const generatedAt = new Date().toISOString();
	const ledgerRecord = {
		schema_version: 1,
		generated_at: generatedAt,
		gate: PHASE9_VALIDATION_GATE,
		passed: payload.passed,
		status: payload.passed ? "pass" : "fail",
		issues: payload.issues,
		report_path: getPhase9OutcomeAuditKey(
			report.proposal_id ?? "unknown_proposal",
			report.apply_mutation_id ?? "unknown_apply",
		),
		details: payload.details,
	};
	const existingStatus = parseStoredObject(await redis.get(VALIDATION_GATE_STATUS_KEY)) ?? {};
	const existingGates = parseStoredObject(existingStatus.gates) ?? {};
	const gates = {
		...existingGates,
		[PHASE9_VALIDATION_GATE]: ledgerRecord,
	};
	const overallPassed = Object.values(gates).every((gate) =>
		parseStoredObject(gate)?.passed === true,
	);
	const gateStatus = {
		schema_version: 1,
		updated_at: generatedAt,
		overall_status: overallPassed ? "green" : "red",
		overall_passed: overallPassed,
		gates,
	};
	await redis.set(VALIDATION_LAST_KEY, JSON.stringify(ledgerRecord));
	await redis.set(VALIDATION_GATE_STATUS_KEY, JSON.stringify(gateStatus));
	await redis.lpush(`${VALIDATION_HISTORY_PREFIX}${generatedAt.slice(0, 10)}`, JSON.stringify(ledgerRecord));
	await redis.ltrim(`${VALIDATION_HISTORY_PREFIX}${generatedAt.slice(0, 10)}`, 0, VALIDATION_HISTORY_LIMIT - 1);
	return ledgerRecord;
}

function getExpectedRevision(entry: LoadedEntry): number {
	return toOptionalInteger(entry.metadata.revision) ?? 0;
}

function summarizeProposalEntry(entry: LoadedEntry): Record<string, unknown> {
	return {
		id: entry.id,
		type: entry.type,
		label: entry.label,
		context_type: entry.contextType,
		injection_tier: entry.injectionTier,
		salience_score: entry.salienceScore,
		mention_count: entry.mentionCount,
		access_count: entry.accessCount,
		updated_at: entry.updatedAt,
		expected_revision: getExpectedRevision(entry),
	};
}

function buildDreamProposalOperations(
	duplicatePlans: DuplicateMergePlan[],
	contradictionPlans: ContradictionPlan[],
	entriesById: Map<string, LoadedEntry>,
	promotionCandidates: LoadedEntry[],
	archiveCandidates: LoadedEntry[],
): Array<Record<string, unknown>> {
	const operations: Array<Record<string, unknown>> = [];

	// Entries contested in THIS proposal must not also be proposed for archive:
	// a no-write proposal never mutates the snapshot, so their state is still
	// "active" here and isArchiveCandidate wouldn't catch them. Archiving an
	// entry the same run it is flagged for review would void that review.
	const contestedThisRun = new Set<string>(contradictionPlans.flatMap((plan) => plan.entryIds));

	for (const plan of duplicatePlans) {
		const archiveIds = plan.duplicates.map((entry) => entry.id);
		const expectedRevisions: Record<string, number> = {
			[plan.canonical.id]: getExpectedRevision(plan.canonical),
		};
		for (const duplicate of plan.duplicates) {
			expectedRevisions[duplicate.id] = getExpectedRevision(duplicate);
		}
		operations.push({
			operation_id: `dop_merge_${plan.canonical.id}_${archiveIds.join("_")}`,
			type: "duplicate_merge",
			keep_id: plan.canonical.id,
			archive_ids: archiveIds,
			expected_revisions: expectedRevisions,
			semantic_only: Boolean(plan.semanticOnly),
			max_cosine: plan.maxCosine ?? null,
			requires_judge: Boolean(plan.semanticOnly),
			reason: plan.semanticOnly
				? `Dream detected semantically near-duplicate entries (cosine ${plan.maxCosine ?? "?"}) with differing titles — requires judge/operator confirmation.`
				: "Dream detected compatible duplicate entries with the same normalized topic fingerprint.",
			evidence: {
				fingerprint: plan.fingerprint,
				semantic_only: Boolean(plan.semanticOnly),
				max_cosine: plan.maxCosine ?? null,
				canonical: summarizeProposalEntry(plan.canonical),
				duplicates: plan.duplicates.map(summarizeProposalEntry),
			},
			rollback: {
				method: "restore_archived",
				entry_ids: archiveIds,
			},
		});
	}

	for (const plan of contradictionPlans) {
		const expectedRevisions = Object.fromEntries(
			plan.entryIds.map((entryId) => [
				entryId,
				entriesById.has(entryId) ? getExpectedRevision(entriesById.get(entryId)!) : null,
			]),
		);
		operations.push({
			operation_id: plan.operationId ?? `dop_contest_${plan.entryIds.join("_")}`,
			type: "mark_contested",
			...(plan.proposalKind ? { proposal_kind: plan.proposalKind } : {}),
			entry_ids: plan.entryIds,
			expected_revisions: expectedRevisions,
			reason: plan.proposalKind === "contest"
				? "A user correction appears to contradict this prior memory; contest it for operator review."
				: "Dream detected contradictory views that require operator review before consolidation.",
			evidence: {
				label: plan.label,
				reasons: plan.reasons,
				...(plan.evidence ?? {}),
			},
			rollback: {
				method: "update_entry",
				restore_state: "active",
			},
		});
	}

	for (const entry of promotionCandidates) {
		operations.push({
			operation_id: `dop_promote_${entry.id}`,
			type: "promote_context_type",
			entry_id: entry.id,
			expected_revision: getExpectedRevision(entry),
			proposed_context_type: defaultInjectionTier(entry.contextType) <= 2
				? entry.contextType
				: "recurring_pattern",
			reason: "Dream found repeated or retrieved task-query memory that is strong enough to become durable recurring context.",
			evidence: summarizeProposalEntry(entry),
			rollback: {
				method: "set_context_type",
				restore_context_type: entry.contextType,
			},
		});
	}

	for (const entry of archiveCandidates) {
		if (contestedThisRun.has(entry.id)) continue;
		operations.push({
			operation_id: `dop_archive_${entry.id}`,
			type: "archive_entry",
			entry_id: entry.id,
			expected_revision: getExpectedRevision(entry),
			reason: "Dream found low-salience single-mention memory with no retrieval reinforcement.",
			evidence: summarizeProposalEntry(entry),
			rollback: {
				method: "restore_archived",
				entry_id: entry.id,
			},
		});
	}

	return operations;
}

export async function runDreamProposal(
	env: Env,
	options: RunDreamProposalOptions,
): Promise<Record<string, unknown>> {
	const redis = createRedisClient(env);
	const createdAt = new Date().toISOString();
	const runId = `dpr_${createdAt.replace(/[:.]/g, "-")}`;

	const migrationBackfillComplete = await redis.get("migration:backfill_complete");
	if (!migrationBackfillComplete) {
		return {
			schema_version: 1,
			run_id: runId,
			status: "skipped_no_backfill",
			created_at: createdAt,
			completed_at: new Date().toISOString(),
			dry_run: true,
			trigger: options.trigger,
			actor_id: options.actorId,
			note: options.note ?? null,
			next_action: "Backfill must complete before Dream can generate proposals.",
		};
	}

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
	const candidateEntries = candidateIdFilter
		? allEntries.filter((entry) => candidateIdFilter.has(entry.id))
		: allEntries;
	// Phase 1: semantic entity resolution. Reuse stored embeddings via a
	// vector-backed neighbour function (never re-embeds), bounded by
	// SEMANTIC_DEDUP_MAX_QUERIES. Runs for targeted (candidate_ids) proposals
	// or when options.semantic === true; the unfiltered nightly proposal stays
	// lexical-only to keep it cheap and within Worker subrequest limits.
	const runSemantic = Boolean(candidateIdFilter) || options.semantic === true;
	const semanticConfig = readSemanticDedupConfig();
	const proposalNeighborFn = runSemantic
		? makeVectorNeighborFn(createVectorClient(env), semanticConfig, new Map<string, number[] | null>())
		: null;
	const {
		duplicatePlans,
		contradictionPlans: replayContradictionPlans,
		semantic: proposalSemantic,
	} = await buildReplayPlansWithSemantic(candidateEntries, proposalNeighborFn, semanticConfig);
	const entriesById = new Map(candidateEntries.map((entry) => [entry.id, entry]));
	const replayContestedIds = new Set(replayContradictionPlans.flatMap((plan) => plan.entryIds));
	const correctionContestPlans = await loadCorrectionContestPlans(redis, entriesById, replayContestedIds);
	const contradictionPlans = [...replayContradictionPlans, ...correctionContestPlans];
	const promotionCandidates = candidateEntries
		.filter(isPromotionCandidate)
		.sort(comparePromotionPriority);
	const archiveCandidates = candidateEntries
		.filter(isArchiveCandidate)
		.sort(compareArchivePriority);
	const promotionCandidatesLimited =
		typeof options.promotionLimit === "number" && options.promotionLimit >= 0
			? promotionCandidates.slice(0, options.promotionLimit)
			: promotionCandidates;
	const archiveCandidatesLimited =
		typeof options.archiveLimit === "number" && options.archiveLimit >= 0
			? archiveCandidates.slice(0, options.archiveLimit)
			: archiveCandidates;
	const bucketCounts: Record<DreamBucket, number> = {
		stable: 0,
		active: 0,
		weak: 0,
		decay_candidate: 0,
	};

	for (const entry of allEntries) {
		bucketCounts[classifyBucket(entry)] += 1;
	}

	const operations = buildDreamProposalOperations(
		duplicatePlans,
		contradictionPlans,
		entriesById,
		promotionCandidatesLimited,
		archiveCandidatesLimited,
	);
	const candidateRevisions = Object.fromEntries(
		candidateEntries.map((entry) => [entry.id, getExpectedRevision(entry)]),
	);
	const snapshot = {
		schema_version: 1,
		run_id: runId,
		captured_at: createdAt,
		candidate_ids: candidateEntries.map((entry) => entry.id),
		candidate_revisions: candidateRevisions,
		counts: {
			total_entries: allEntries.length,
			knowledge_entries: knowledgeEntries.length,
			project_entries: projectEntries.length,
			candidate_entries: candidateEntries.length,
			buckets: bucketCounts,
		},
	};
	const proposal = {
		schema_version: 1,
		run_id: runId,
		status: "proposal_ready",
		created_at: createdAt,
		completed_at: new Date().toISOString(),
		dry_run: true,
		trigger: options.trigger,
		actor_id: options.actorId,
		note: options.note ?? null,
		candidate_ids: candidateEntries.map((entry) => entry.id),
		candidate_revisions: candidateRevisions,
		operation_count: operations.length,
		operations,
		counts: {
			total_entries: allEntries.length,
			knowledge_entries: knowledgeEntries.length,
			project_entries: projectEntries.length,
			candidate_entries: candidateEntries.length,
			duplicate_merge_candidates: duplicatePlans.length,
			semantic_only_merges: duplicatePlans.filter((p) => p.semanticOnly).length,
			lexical_merges: duplicatePlans.filter((p) => !p.semanticOnly).length,
			semantic_dedup: proposalSemantic,
			contradictions_detected: contradictionPlans.length,
			correction_contest_candidates: correctionContestPlans.length,
			promotion_candidates: promotionCandidates.length,
			promotion_limit: options.promotionLimit ?? null,
			archive_candidates: archiveCandidates.length,
			archive_limit: options.archiveLimit ?? null,
			stable: bucketCounts.stable,
			active: bucketCounts.active,
			weak: bucketCounts.weak,
			decay_candidates: bucketCounts.decay_candidate,
		},
		requires_operator_review: operations.length > 0,
		risk_score: duplicatePlans.length > 0 || contradictionPlans.length > 0 ? "medium" : "low",
		next_action: operations.length > 0
			? "Review proposed operations and apply them through explicit write tools only."
			: "No Dream governance operations are proposed for this snapshot.",
	};

	await redis.set(`${DREAM_RUN_PREFIX}${runId}:snapshot`, JSON.stringify(snapshot));
	await redis.set(`${DREAM_RUN_PREFIX}${runId}:proposal`, JSON.stringify(proposal));
	await redis.set(`${DREAM_RUN_PREFIX}${runId}`, JSON.stringify(proposal));
	await redis.set(DREAM_LAST_PROPOSAL_KEY, JSON.stringify(proposal));
	await updateDreamRunIndex(redis, runId);
	return proposal;
}

function getDreamGradeKey(proposalId: string, gradeId?: string | null): string {
	return gradeId
		? `${DREAM_RUN_PREFIX}${proposalId}:grade:${gradeId}`
		: `${DREAM_RUN_PREFIX}${proposalId}:grade`;
}

function buildGradeIssue(
	code: string,
	message: string,
	operationId?: string | null,
): Record<string, unknown> {
	return {
		code,
		message,
		operation_id: operationId ?? null,
	};
}

function gradeDreamProposalRecord(
	proposal: Record<string, unknown>,
	options: { actorId: string; rubricVersion?: string | null },
): Record<string, unknown> {
	const gradedAt = new Date().toISOString();
	const proposalId = typeof proposal.run_id === "string" ? proposal.run_id : "unknown_proposal";
	const operations = toObjectArray(proposal.operations);
	const candidateIds = new Set(toStringArray(proposal.candidate_ids));
	const issues: Array<Record<string, unknown>> = [];
	const allowedOperationTypes = new Set([
		"archive_entry",
		"promote_context_type",
		"mark_contested",
		"duplicate_merge",
	]);

	if (proposal.status !== "proposal_ready") {
		issues.push(buildGradeIssue("proposal_not_ready", `Proposal status is ${String(proposal.status)}`));
	}
	if (candidateIds.size === 0 && operations.length > 0) {
		issues.push(buildGradeIssue("missing_snapshot_candidates", "Mutating proposal has no candidate snapshot ids."));
	}
	if (!parseStoredObject(proposal.candidate_revisions) && operations.length > 0) {
		issues.push(buildGradeIssue("missing_candidate_revisions", "Mutating proposal has no candidate revision map."));
	}

	const seenOperationIds = new Set<string>();
	for (const operation of operations) {
		const operationId = typeof operation.operation_id === "string" ? operation.operation_id : null;
		const operationType = typeof operation.type === "string" ? operation.type : null;
		if (!operationId) {
			issues.push(buildGradeIssue("missing_operation_id", "Operation is missing operation_id."));
		} else if (seenOperationIds.has(operationId)) {
			issues.push(buildGradeIssue("duplicate_operation_id", "Operation id is duplicated.", operationId));
		} else {
			seenOperationIds.add(operationId);
		}
		if (!operationType || !allowedOperationTypes.has(operationType)) {
			issues.push(buildGradeIssue("unsupported_operation_type", `Unsupported operation type ${String(operationType)}`, operationId));
		}
		const touchedIds = getOperationTouchedIds(operation);
		if (touchedIds.length === 0) {
			issues.push(buildGradeIssue("operation_touches_no_entries", "Operation does not identify any touched entries.", operationId));
		}
		for (const entryId of touchedIds) {
			if (!candidateIds.has(entryId)) {
				issues.push(buildGradeIssue("entry_outside_snapshot", `Operation touches ${entryId}, which is outside the proposal snapshot.`, operationId));
			}
		}
		const expectedRevisions = getOperationExpectedRevisions(operation);
		for (const entryId of touchedIds) {
			if (expectedRevisions[entryId] === undefined) {
				issues.push(buildGradeIssue("missing_expected_revision", `Operation is missing expected revision for ${entryId}.`, operationId));
			}
		}
		if (!parseStoredObject(operation.rollback)) {
			issues.push(buildGradeIssue("missing_rollback_metadata", "Operation is missing rollback metadata.", operationId));
		}
		const reason = typeof operation.reason === "string" ? operation.reason.trim() : "";
		if (!reason) {
			issues.push(buildGradeIssue("missing_reason", "Operation is missing a reason.", operationId));
		}
		if (!parseStoredObject(operation.evidence)) {
			issues.push(buildGradeIssue("missing_evidence", "Operation is missing evidence.", operationId));
		}
	}

	const counts = parseStoredObject(proposal.counts) ?? {};
	const archiveLimit = toOptionalInteger(counts.archive_limit);
	const promotionLimit = toOptionalInteger(counts.promotion_limit);
	const archiveOps = operations.filter((operation) => operation.type === "archive_entry").length;
	const promotionOps = operations.filter((operation) => operation.type === "promote_context_type").length;
	if (archiveLimit !== null && archiveOps > archiveLimit) {
		issues.push(buildGradeIssue("archive_limit_exceeded", `Archive operations ${archiveOps} exceed limit ${archiveLimit}.`));
	}
	if (promotionLimit !== null && promotionOps > promotionLimit) {
		issues.push(buildGradeIssue("promotion_limit_exceeded", `Promotion operations ${promotionOps} exceed limit ${promotionLimit}.`));
	}

	const passed = issues.length === 0;
	return {
		schema_version: 1,
		grade_id: `dpg_${gradedAt.replace(/[:.]/g, "-")}`,
		proposal_id: proposalId,
		graded_at: gradedAt,
		graded_by: options.actorId,
		rubric_version: options.rubricVersion ?? "deterministic-v1",
		status: passed ? "passed" : "failed",
		passed,
		hard_fail_count: issues.length,
		issues,
		operation_count: operations.length,
		operation_ids: operations
			.map((operation) => operation.operation_id)
			.filter((operationId): operationId is string => typeof operationId === "string"),
		rubric: {
			evidence_sufficiency: passed,
			revision_safety: passed,
			idempotency_safety: passed,
			reversibility: passed,
			blast_radius: passed,
			retrieval_index_impact: passed,
			policy_threshold_compliance: passed,
			operator_review_requirement: operations.length > 0,
		},
		next_action: passed
			? "Proposal passed deterministic hard gates and may be applied through apply_dream_proposal."
			: "Fix or regenerate the proposal before applying.",
	};
}

export async function gradeDreamProposal(
	env: Env,
	options: GradeDreamProposalOptions,
): Promise<Record<string, unknown>> {
	const redis = createRedisClient(env);
	const proposal = parseStoredObject(await redis.get(`${DREAM_RUN_PREFIX}${options.proposalId}:proposal`)) ??
		parseStoredObject(await redis.get(`${DREAM_RUN_PREFIX}${options.proposalId}`));
	if (!proposal) {
		return {
			ok: false,
			error: "proposal_not_found",
			proposal_id: options.proposalId,
		};
	}
	const grade = gradeDreamProposalRecord(proposal, {
		actorId: options.actorId,
		rubricVersion: options.rubricVersion,
	});
	await redis.set(getDreamGradeKey(options.proposalId), JSON.stringify(grade));
	await redis.set(getDreamGradeKey(options.proposalId, String(grade.grade_id)), JSON.stringify(grade));
	return grade;
}

function getEntryTypeFromId(entryId: string): EntryType {
	return entryId.startsWith("pe_") ? "project" : "knowledge";
}

function getOperationTouchedIds(operation: Record<string, unknown>): string[] {
	const ids = new Set<string>();
	const entryId = typeof operation.entry_id === "string" ? operation.entry_id : null;
	const keepId = typeof operation.keep_id === "string" ? operation.keep_id : null;
	if (entryId) ids.add(entryId);
	if (keepId) ids.add(keepId);
	for (const value of toStringArray(operation.entry_ids)) ids.add(value);
	for (const value of toStringArray(operation.archive_ids)) ids.add(value);
	return [...ids];
}

function getOperationExpectedRevisions(operation: Record<string, unknown>): Record<string, number> {
	const expectedRevisions: Record<string, number> = {};
	const expectedRevisionMap = parseStoredObject(operation.expected_revisions);
	if (expectedRevisionMap) {
		for (const [entryId, rawRevision] of Object.entries(expectedRevisionMap)) {
			const revision = toOptionalInteger(rawRevision);
			if (revision !== null) {
				expectedRevisions[entryId] = revision;
			}
		}
	}
	const entryId = typeof operation.entry_id === "string" ? operation.entry_id : null;
	const expectedRevision = toOptionalInteger(operation.expected_revision);
	if (entryId && expectedRevision !== null) {
		expectedRevisions[entryId] = expectedRevision;
	}
	return expectedRevisions;
}

async function loadTouchedEntries(
	redis: Redis,
	entryIds: string[],
): Promise<Map<string, LoadedEntry>> {
	const entries = new Map<string, LoadedEntry>();
	for (const entryId of entryIds) {
		const loadedEntry = await loadLoadedEntry(redis, getEntryTypeFromId(entryId), entryId);
		if (loadedEntry) {
			entries.set(entryId, loadedEntry);
		}
	}
	return entries;
}

function validateOperationRevisions(
	operation: Record<string, unknown>,
	entriesById: Map<string, LoadedEntry>,
): Record<string, unknown> | null {
	const operationId = typeof operation.operation_id === "string" ? operation.operation_id : "unknown_operation";
	for (const entryId of getOperationTouchedIds(operation)) {
		const entry = entriesById.get(entryId);
		if (!entry) {
			return {
				ok: false,
				error: "entry_not_found",
				operation_id: operationId,
				id: entryId,
			};
		}
		if (entry.metadata.archived === true) {
			return {
				ok: false,
				error: "entry_archived",
				operation_id: operationId,
				id: entryId,
			};
		}
	}

	const expectedRevisions = getOperationExpectedRevisions(operation);
	for (const [entryId, expectedRevision] of Object.entries(expectedRevisions)) {
		const entry = entriesById.get(entryId);
		if (!entry) {
			return {
				ok: false,
				error: "entry_not_found",
				operation_id: operationId,
				id: entryId,
			};
		}
		const actualRevision = getExpectedRevision(entry);
		if (actualRevision !== expectedRevision) {
			return {
				ok: false,
				error: "conflict",
				operation_id: operationId,
				id: entryId,
				expected_revision: expectedRevision,
				actual_revision: actualRevision,
			};
		}
	}
	return null;
}

function getOperationReason(operation: Record<string, unknown>, fallback: string): string {
	return typeof operation.reason === "string" && operation.reason.length > 0
		? operation.reason
		: fallback;
}

async function markCorrectionContestHintApplied(
	redis: Redis,
	operation: Record<string, unknown>,
	applyRunId: string,
	timestamp: string,
): Promise<void> {
	try {
		if (operation.proposal_kind !== "contest") return;
		const evidence = parseStoredObject(operation.evidence);
		const hintKey = typeof evidence?.correction_hint_key === "string" ? evidence.correction_hint_key : null;
		if (!hintKey || !hintKey.startsWith(CORRECTION_CONTEST_HINT_PREFIX)) return;
		const hint = parseStoredObject(await redis.get(hintKey));
		if (!hint) return;
		await redis.set(hintKey, JSON.stringify({
			...hint,
			status: "applied",
			applied_at: timestamp,
			applied_run_id: applyRunId,
			operation_id: operation.operation_id ?? null,
		}));
	} catch (error) {
		console.error("[dream] failed to mark correction contest hint applied", error);
	}
}

async function applyDreamProposalOperation(
	redis: Redis,
	vector: Index,
	operation: Record<string, unknown>,
	entriesById: Map<string, LoadedEntry>,
	applyRunId: string,
	timestamp: string,
): Promise<Record<string, unknown>> {
	const operationId = typeof operation.operation_id === "string" ? operation.operation_id : "unknown_operation";
	const operationType = typeof operation.type === "string" ? operation.type : "unknown";

	if (operationType === "duplicate_merge") {
		const keepId = typeof operation.keep_id === "string" ? operation.keep_id : null;
		const archiveIds = toStringArray(operation.archive_ids);
		if (!keepId || archiveIds.length === 0) {
			return { ok: false, operation_id: operationId, error: "invalid_duplicate_merge_operation" };
		}
		const canonical = entriesById.get(keepId);
		const duplicates = archiveIds
			.map((entryId) => entriesById.get(entryId))
			.filter((entry): entry is LoadedEntry => Boolean(entry));
		if (!canonical || duplicates.length !== archiveIds.length) {
			return { ok: false, operation_id: operationId, error: "entry_not_found" };
		}
		const result = await applyDuplicateMergePlan(
			redis,
			vector,
			{
				fingerprint:
					typeof parseStoredObject(operation.evidence)?.fingerprint === "string"
						? String(parseStoredObject(operation.evidence)?.fingerprint)
						: canonical.id,
				canonical,
				duplicates,
			},
			applyRunId,
			timestamp,
		);
		return { ok: true, operation_id: operationId, type: operationType, result };
	}

	if (operationType === "mark_contested") {
		const entryIds = toStringArray(operation.entry_ids);
		const reasons = toStringArray(parseStoredObject(operation.evidence)?.reasons);
		const results: Array<Record<string, unknown>> = [];
		for (const entryId of entryIds) {
			const entry = entriesById.get(entryId);
			if (!entry) {
				return { ok: false, operation_id: operationId, error: "entry_not_found", id: entryId };
			}
			results.push(
				await markEntryContested(
					redis,
					vector,
					entry,
					reasons.length > 0 ? reasons : [getOperationReason(operation, "Dream proposal contested marker")],
					entryIds.filter((candidateId) => candidateId !== entryId),
					applyRunId,
					timestamp,
				),
			);
		}
		await markCorrectionContestHintApplied(redis, operation, applyRunId, timestamp);
		return { ok: true, operation_id: operationId, type: operationType, results };
	}

	if (operationType === "promote_context_type") {
		const entryId = typeof operation.entry_id === "string" ? operation.entry_id : null;
		const entry = entryId ? entriesById.get(entryId) : null;
		if (!entry) {
			return { ok: false, operation_id: operationId, error: "entry_not_found", id: entryId };
		}
		const result = await promoteEntry(redis, vector, entry, applyRunId, timestamp);
		return { ok: true, operation_id: operationId, type: operationType, result };
	}

	if (operationType === "archive_entry") {
		const entryId = typeof operation.entry_id === "string" ? operation.entry_id : null;
		const entry = entryId ? entriesById.get(entryId) : null;
		if (!entry) {
			return { ok: false, operation_id: operationId, error: "entry_not_found", id: entryId };
		}
		const result = await archiveEntry(
			redis,
			vector,
			entry,
			applyRunId,
			timestamp,
			getOperationReason(operation, "applied Dream proposal archive operation"),
		);
		return { ok: true, operation_id: operationId, type: operationType, result };
	}

	return {
		ok: false,
		operation_id: operationId,
		error: "unsupported_operation_type",
		type: operationType,
	};
}

export async function applyDreamProposal(
	env: Env,
	options: ApplyDreamProposalOptions,
): Promise<Record<string, unknown>> {
	const redis = createRedisClient(env);
	const vector = createVectorClient(env);
	const storedMutation = parseStoredObject(await redis.get(getMutationResultKey(options.mutationId)));
	if (storedMutation) {
		return storedMutation;
	}

	const proposal = parseStoredObject(await redis.get(`${DREAM_RUN_PREFIX}${options.proposalId}:proposal`)) ??
		parseStoredObject(await redis.get(`${DREAM_RUN_PREFIX}${options.proposalId}`));
	if (!proposal) {
		const result = {
			ok: false,
			error: "proposal_not_found",
			proposal_id: options.proposalId,
			mutation_id: options.mutationId,
		};
		await storeMutationResult(redis, options.mutationId, result);
		return result;
	}

	if (proposal.status !== "proposal_ready") {
		const result = {
			ok: false,
			error: "proposal_not_applicable",
			proposal_id: options.proposalId,
			status: proposal.status ?? null,
			mutation_id: options.mutationId,
		};
		await storeMutationResult(redis, options.mutationId, result);
		return result;
	}

	const allOperations = toObjectArray(proposal.operations);
	const selectedIds = options.operationIds && options.operationIds.length > 0
		? new Set(options.operationIds)
		: null;
	const operations = selectedIds
		? allOperations.filter((operation) => {
			const operationId = typeof operation.operation_id === "string" ? operation.operation_id : null;
			return operationId ? selectedIds.has(operationId) : false;
		})
		: allOperations;
	if (selectedIds && operations.length !== selectedIds.size) {
		const foundIds = new Set(
			operations
				.map((operation) => operation.operation_id)
				.filter((operationId): operationId is string => typeof operationId === "string"),
		);
		const result = {
			ok: false,
			error: "operation_not_found",
			proposal_id: options.proposalId,
			missing_operation_ids: [...selectedIds].filter((operationId) => !foundIds.has(operationId)),
			mutation_id: options.mutationId,
		};
		await storeMutationResult(redis, options.mutationId, result);
		return result;
	}
	if (operations.length === 0) {
		const result = {
			ok: true,
			proposal_id: options.proposalId,
			mutation_id: options.mutationId,
			applied_count: 0,
			results: [],
			no_op: true,
		};
		await storeMutationResult(redis, options.mutationId, result);
		return result;
	}

	if (options.requireGradePass !== false) {
		const grade = parseStoredObject(await redis.get(getDreamGradeKey(options.proposalId, options.gradeId))) ??
			parseStoredObject(await redis.get(getDreamGradeKey(options.proposalId)));
		if (!grade || grade.passed !== true || grade.status !== "passed") {
			const result = {
				ok: false,
				error: "grade_required",
				proposal_id: options.proposalId,
				mutation_id: options.mutationId,
				grade_status: grade?.status ?? null,
				next_action: "Run grade_dream_proposal and ensure it passes before applying mutating operations.",
			};
			await storeMutationResult(redis, options.mutationId, result);
			return result;
		}
		const gradedOperationIds = new Set(toStringArray(grade.operation_ids));
		const ungradedOperationIds = operations
			.map((operation) => operation.operation_id)
			.filter((operationId): operationId is string => typeof operationId === "string")
			.filter((operationId) => !gradedOperationIds.has(operationId));
		if (ungradedOperationIds.length > 0) {
			const result = {
				ok: false,
				error: "operation_not_graded",
				proposal_id: options.proposalId,
				mutation_id: options.mutationId,
				operation_ids: ungradedOperationIds,
			};
			await storeMutationResult(redis, options.mutationId, result);
			return result;
		}
	}

	const touchedIds = [...new Set(operations.flatMap(getOperationTouchedIds))];
	const entriesById = await loadTouchedEntries(redis, touchedIds);
	for (const operation of operations) {
		const validationError = validateOperationRevisions(operation, entriesById);
		if (validationError) {
			const result = {
				...validationError,
				proposal_id: options.proposalId,
				mutation_id: options.mutationId,
			};
			await storeMutationResult(redis, options.mutationId, result);
			return result;
		}
	}

	const phase9Enabled = options.phase9OutcomeGate === true;
	const phase9AutoRollback = options.phase9AutoRollback === true;
	const phase9WriteValidationLedger = options.phase9WriteValidationLedger === true;
	const phase9Probes = phase9Enabled
		? await loadPhase9OutcomeProbes(redis, options)
		: [];
	let phase9PreReport: Phase9OutcomeEvalReport | null = null;
	if (phase9Enabled) {
		if (phase9Probes.length === 0) {
			const result = {
				ok: false,
				error: "phase9_outcome_probes_not_configured",
				proposal_id: options.proposalId,
				mutation_id: options.mutationId,
				probe_set_key: options.phase9ProbeSetKey ?? PHASE9_DEFAULT_PROBE_SET_KEY,
				next_action: "Configure bounded Phase 9 outcome probes or disable phase9_outcome_gate for this apply.",
			};
			await storeMutationResult(redis, options.mutationId, result);
			return result;
		}
		phase9PreReport = await evaluateStoredPhase9OutcomeProbes(redis, phase9Probes);
		if (!phase9PreReport.passed) {
			const result = {
				ok: false,
				error: "phase9_pre_outcome_baseline_failed",
				proposal_id: options.proposalId,
				mutation_id: options.mutationId,
				phase9_outcome_gate: {
					schema_version: 1,
					status: "pre_baseline_failed",
					pre_report: phase9PreReport,
					probe_count: phase9Probes.length,
				},
				next_action: "Fix the configured outcome probes or current retrieval state before applying Dream mutations.",
			};
			await storePhase9OutcomeAudit(redis, options.proposalId, options.mutationId, result);
			await storeMutationResult(redis, options.mutationId, result);
			return result;
		}
	}

	const timestamp = new Date().toISOString();
	const applyRunId = `apply_${options.proposalId}_${timestamp.replace(/[:.]/g, "-")}`;
	const beforeRevisions = Object.fromEntries(
		touchedIds.map((entryId) => [entryId, getExpectedRevision(entriesById.get(entryId)!)]),
	);
	const beforeSnapshots = Object.fromEntries(
		touchedIds.map((entryId) => [
			entryId,
			JSON.parse(JSON.stringify(entriesById.get(entryId)!.entry)),
		]),
	);
	const operationResults: Array<Record<string, unknown>> = [];

	for (const operation of operations) {
		operationResults.push(
			await applyDreamProposalOperation(
				redis,
				vector,
				operation,
				entriesById,
				applyRunId,
				timestamp,
			),
		);
	}

	if (operationResults.some((result) => result.ok === false)) {
		const failedResult = {
			ok: false,
			error: "operation_failed",
			proposal_id: options.proposalId,
			mutation_id: options.mutationId,
			results: operationResults,
		};
		await storeMutationResult(redis, options.mutationId, failedResult);
		return failedResult;
	}

	if (operationResults.length > 0) {
		await rebuildThinIndexSafely(redis, applyRunId);
	}

	const refreshedEntriesById = await loadTouchedEntries(redis, touchedIds);
	const afterRevisions = Object.fromEntries(
		touchedIds.map((entryId) => [
			entryId,
			refreshedEntriesById.has(entryId)
				? getExpectedRevision(refreshedEntriesById.get(entryId)!)
				: null,
		]),
	);
	let result: Record<string, unknown> = {
		ok: true,
		proposal_id: options.proposalId,
		apply_run_id: applyRunId,
		mutation_id: options.mutationId,
		applied_at: timestamp,
		applied_by: options.actorId,
		applied_count: operationResults.length,
		operation_ids: operations.map((operation) => operation.operation_id),
		results: operationResults,
		before_revisions: beforeRevisions,
		after_revisions: afterRevisions,
		before_snapshots: beforeSnapshots,
		side_effects: {
			index: "rebuilt",
		},
	};

	await redis.set(`${DREAM_RUN_PREFIX}${options.proposalId}:apply:${options.mutationId}`, JSON.stringify(result));
	await redis.set(`${DREAM_RUN_PREFIX}${options.proposalId}:events`, JSON.stringify([
		{
			ts: timestamp,
			event: "proposal_applied",
			mutation_id: options.mutationId,
			actor_id: options.actorId,
			reason: options.reason,
			operation_ids: operations.map((operation) => operation.operation_id),
			ids_affected: touchedIds,
		},
	]));
	await appendMutationLog(redis, {
		ts: timestamp,
		mutation_id: options.mutationId,
		tool: "apply_dream_proposal",
		client: "mcp",
		actor_id: options.actorId,
		request_id: options.mutationId,
		ids_affected: touchedIds,
		before_revisions: beforeRevisions,
		after_revisions: afterRevisions,
		reason: options.reason,
		proposal_id: options.proposalId,
	});

	if (phase9Enabled && phase9PreReport) {
		const phase9PostReport = await evaluateStoredPhase9OutcomeProbes(redis, phase9Probes);
		const phase9GateReport = evaluatePhase9OutcomeGate(phase9PreReport, phase9PostReport, {
			generatedAt: new Date().toISOString(),
			proposalId: options.proposalId,
			applyMutationId: options.mutationId,
		});
		const phase9RollbackRecommendation = buildPhase9RollbackRecommendation(phase9GateReport, {
			operationIds: operations
				.map((operation) => operation.operation_id)
				.filter((operationId): operationId is string => typeof operationId === "string"),
		});
		let phase9ValidationLedgerRecord: Record<string, unknown> | null = null;
		if (phase9WriteValidationLedger) {
			phase9ValidationLedgerRecord = await writePhase9ValidationGate(redis, phase9GateReport);
		}

		let phase9RollbackResult: Record<string, unknown> | null = null;
		if (
			phase9GateReport.rollback_required &&
			phase9AutoRollback &&
			phase9RollbackRecommendation.ready &&
			phase9RollbackRecommendation.rollback_mutation_id
		) {
			phase9RollbackResult = await rollbackDreamApply(env, {
				proposalId: options.proposalId,
				applyMutationId: options.mutationId,
				rollbackMutationId: phase9RollbackRecommendation.rollback_mutation_id,
				actorId: options.actorId,
				reason: String(phase9RollbackRecommendation.reason ?? "phase9 outcome regression").slice(0, 500),
				operationIds: phase9RollbackRecommendation.operation_ids,
			});
		}

		const phase9Payload = {
			schema_version: 1,
			status: phase9GateReport.passed
				? "passed"
				: phase9RollbackResult?.ok === true
					? "regression_rolled_back"
					: "regression_detected",
			pre_report: phase9PreReport,
			post_report: phase9PostReport,
			gate_report: phase9GateReport,
			rollback_recommendation: phase9RollbackRecommendation,
			rollback_result: phase9RollbackResult,
			validation_ledger_record: phase9ValidationLedgerRecord,
		};
		result = {
			...result,
			ok: phase9GateReport.passed,
			...(phase9GateReport.passed
				? {}
				: {
					error: phase9RollbackResult?.ok === true
						? "phase9_outcome_regression_rolled_back"
						: phase9RollbackResult
							? "phase9_outcome_regression_rollback_failed"
							: "phase9_outcome_regression",
					rolled_back: phase9RollbackResult?.ok === true,
				}),
			phase9_outcome_gate: phase9Payload,
		};
		await storePhase9OutcomeAudit(redis, options.proposalId, options.mutationId, result);
	}
	await storeMutationResult(redis, options.mutationId, result);
	return result;
}

function getRollbackSupportedOperationTypes(): Set<string> {
	return new Set(["duplicate_merge", "mark_contested", "promote_context_type", "archive_entry"]);
}

function getApplyAuditKey(proposalId: string, applyMutationId: string): string {
	return `${DREAM_RUN_PREFIX}${proposalId}:apply:${applyMutationId}`;
}

function getRollbackAuditKey(proposalId: string, rollbackMutationId: string): string {
	return `${DREAM_RUN_PREFIX}${proposalId}:rollback:${rollbackMutationId}`;
}

function getApplyAfterRevision(applyRecord: Record<string, unknown>, entryId: string): number | null {
	const afterRevisions = parseStoredObject(applyRecord.after_revisions);
	if (!afterRevisions) return null;
	return toOptionalInteger(afterRevisions[entryId]);
}

function getApplyBeforeSnapshot(
	applyRecord: Record<string, unknown>,
	entryId: string,
): Record<string, unknown> | null {
	const beforeSnapshots = parseStoredObject(applyRecord.before_snapshots);
	if (!beforeSnapshots) return null;
	return parseStoredObject(beforeSnapshots[entryId]);
}

async function validateRollbackCurrentRevisions(
	redis: Redis,
	applyRecord: Record<string, unknown>,
	operations: Record<string, unknown>[],
): Promise<{ entriesById: Map<string, LoadedEntry>; error: Record<string, unknown> | null }> {
	const touchedIds = [...new Set(operations.flatMap(getOperationTouchedIds))];
	const entriesById = await loadTouchedEntries(redis, touchedIds);
	for (const entryId of touchedIds) {
		const entry = entriesById.get(entryId);
		if (!entry) {
			return {
				entriesById,
				error: {
					ok: false,
					error: "entry_not_found",
					id: entryId,
				},
			};
		}
		const expectedCurrentRevision = getApplyAfterRevision(applyRecord, entryId);
		if (expectedCurrentRevision === null) {
			return {
				entriesById,
				error: {
					ok: false,
					error: "rollback_revision_missing",
					id: entryId,
				},
			};
		}
		if (!getApplyBeforeSnapshot(applyRecord, entryId)) {
			return {
				entriesById,
				error: {
					ok: false,
					error: "rollback_snapshot_missing",
					id: entryId,
				},
			};
		}
		const actualRevision = getExpectedRevision(entry);
		if (actualRevision !== expectedCurrentRevision) {
			return {
				entriesById,
				error: {
					ok: false,
					error: "conflict",
					id: entryId,
					expected_revision: expectedCurrentRevision,
					actual_revision: actualRevision,
				},
			};
		}
	}
	return { entriesById, error: null };
}

async function restoreEntryFromApplySnapshot(
	env: Env,
	redis: Redis,
	vector: Index,
	applyRecord: Record<string, unknown>,
	entryId: string,
	rollbackRunId: string,
	timestamp: string,
): Promise<Record<string, unknown>> {
	const snapshot = getApplyBeforeSnapshot(applyRecord, entryId);
	if (!snapshot) {
		return { ok: false, error: "rollback_snapshot_missing", id: entryId };
	}
	const entryType = getEntryTypeFromId(entryId);
	const restoredEntry = normalizeEntry(snapshot, entryType);
	if (!restoredEntry) {
		return { ok: false, error: "rollback_snapshot_invalid", id: entryId };
	}
	const currentEntry = normalizeEntry(await redis.get(getEntryKey(entryType, entryId)), entryType);
	const currentRevision = toOptionalInteger((currentEntry?.metadata as Record<string, unknown> | undefined)?.revision) ?? 0;
	const metadata = (restoredEntry.metadata as Record<string, unknown> | undefined) ?? {};
	const restoredArchived = metadata.archived === true;
	metadata.updated_at = timestamp;
	metadata.updated_by = {
		actor_id: "dream_rollback",
		tool: "rollback_dream_apply",
	};
	metadata.revision = currentRevision + 1;
	metadata.last_consolidated = timestamp;
	appendConsolidationNote(
		metadata,
		formatConsolidationNote({
			timestamp,
			source: "operator",
			action: "rollback_dream_apply",
			detail: `restored snapshot for ${entryId} (${rollbackRunId})`,
		}),
	);
	restoredEntry.metadata = metadata;
	let restoredLoadedEntry = buildLoadedEntry(entryId, entryType, restoredEntry);

	if (entryType === "knowledge") {
		const currentState =
			currentEntry && typeof currentEntry.state === "string" ? currentEntry.state : null;
		if (currentState) {
			await redis.srem(`by_state:${currentState}`, entryId);
		}
		await redis.srem("by_state:archived", entryId);
		if (restoredArchived) {
			await redis.sadd("by_state:archived", entryId);
		} else {
			const restoredState =
				typeof restoredEntry.state === "string" ? restoredEntry.state : "active";
			await redis.sadd(`by_state:${restoredState}`, entryId);
		}
	}

	await syncEntryAccessSignals(redis, restoredLoadedEntry);
	restoredLoadedEntry = buildLoadedEntry(entryId, entryType, restoredEntry);
	if (restoredArchived) {
		await persistEntry(redis, vector, restoredLoadedEntry, { skipVector: true });
		await deleteVectorEntry(vector, entryId);
	} else {
		const embedding = await getEmbedding(env, buildEntryEmbeddingText(restoredLoadedEntry));
		await persistEntry(redis, vector, restoredLoadedEntry, { embedding });
	}
	await patchThinIndexEntry(redis, restoredLoadedEntry, timestamp);

	return {
		ok: true,
		id: entryId,
		type: entryType,
		archived: restoredArchived,
		revision: metadata.revision,
		context_type: restoredLoadedEntry.contextType,
		injection_tier: restoredLoadedEntry.injectionTier,
	};
}

async function rollbackSnapshotOperation(
	env: Env,
	redis: Redis,
	vector: Index,
	applyRecord: Record<string, unknown>,
	operation: Record<string, unknown>,
	rollbackRunId: string,
	timestamp: string,
): Promise<Record<string, unknown>> {
	const results: Array<Record<string, unknown>> = [];
	for (const entryId of getOperationTouchedIds(operation)) {
		results.push(
			await restoreEntryFromApplySnapshot(
				env,
				redis,
				vector,
				applyRecord,
				entryId,
				rollbackRunId,
				timestamp,
			),
		);
	}
	return {
		ok: results.every((result) => result.ok === true),
		type: operation.type ?? "unknown",
		results,
	};
}

async function rollbackDreamOperation(
	env: Env,
	redis: Redis,
	vector: Index,
	applyRecord: Record<string, unknown>,
	operation: Record<string, unknown>,
	rollbackRunId: string,
	timestamp: string,
): Promise<Record<string, unknown>> {
	const operationId = typeof operation.operation_id === "string" ? operation.operation_id : "unknown_operation";
	const operationType = typeof operation.type === "string" ? operation.type : "unknown";
	if (getRollbackSupportedOperationTypes().has(operationType)) {
		return {
			operation_id: operationId,
			...(await rollbackSnapshotOperation(env, redis, vector, applyRecord, operation, rollbackRunId, timestamp)),
		};
	}
	return {
		ok: false,
		operation_id: operationId,
		error: "unsupported_rollback_operation",
		type: operationType,
		message: "This operation cannot be fully rolled back from the current apply audit.",
	};
}

export async function rollbackDreamApply(
	env: Env,
	options: RollbackDreamApplyOptions,
): Promise<Record<string, unknown>> {
	const redis = createRedisClient(env);
	const vector = createVectorClient(env);
	const storedMutation = parseStoredObject(await redis.get(getMutationResultKey(options.rollbackMutationId)));
	if (storedMutation) {
		return storedMutation;
	}

	const applyRecord = parseStoredObject(await redis.get(getApplyAuditKey(options.proposalId, options.applyMutationId)));
	if (!applyRecord || applyRecord.ok !== true) {
		const result = {
			ok: false,
			error: "apply_record_not_found",
			proposal_id: options.proposalId,
			apply_mutation_id: options.applyMutationId,
			mutation_id: options.rollbackMutationId,
		};
		await storeMutationResult(redis, options.rollbackMutationId, result);
		return result;
	}

	const proposal = parseStoredObject(await redis.get(`${DREAM_RUN_PREFIX}${options.proposalId}:proposal`)) ??
		parseStoredObject(await redis.get(`${DREAM_RUN_PREFIX}${options.proposalId}`));
	if (!proposal) {
		const result = {
			ok: false,
			error: "proposal_not_found",
			proposal_id: options.proposalId,
			mutation_id: options.rollbackMutationId,
		};
		await storeMutationResult(redis, options.rollbackMutationId, result);
		return result;
	}

	const appliedIds = new Set(toStringArray(applyRecord.operation_ids));
	const requestedIds = options.operationIds && options.operationIds.length > 0
		? new Set(options.operationIds)
		: appliedIds;
	const operationsById = new Map<string, Record<string, unknown>>();
	for (const operation of toObjectArray(proposal.operations)) {
		const operationId = typeof operation.operation_id === "string" ? operation.operation_id : "";
		if (operationId.length > 0) {
			operationsById.set(operationId, operation);
		}
	}
	const operations = [...requestedIds].map((operationId) => operationsById.get(operationId)).filter(
		(operation): operation is Record<string, unknown> => Boolean(operation),
	);
	if (operations.length !== requestedIds.size) {
		const foundIds = new Set(
			operations.map((operation) => String(operation.operation_id ?? "")),
		);
		const result = {
			ok: false,
			error: "operation_not_found",
			missing_operation_ids: [...requestedIds].filter((operationId) => !foundIds.has(operationId)),
			proposal_id: options.proposalId,
			mutation_id: options.rollbackMutationId,
		};
		await storeMutationResult(redis, options.rollbackMutationId, result);
		return result;
	}
	const notAppliedIds = [...requestedIds].filter((operationId) => !appliedIds.has(operationId));
	if (notAppliedIds.length > 0) {
		const result = {
			ok: false,
			error: "operation_not_applied",
			operation_ids: notAppliedIds,
			proposal_id: options.proposalId,
			mutation_id: options.rollbackMutationId,
		};
		await storeMutationResult(redis, options.rollbackMutationId, result);
		return result;
	}

	const unsupportedOperations = operations.filter((operation) => {
		const operationType = typeof operation.type === "string" ? operation.type : "unknown";
		return !getRollbackSupportedOperationTypes().has(operationType);
	});
	if (unsupportedOperations.length > 0) {
		const result = {
			ok: false,
			error: "unsupported_rollback_operation",
			proposal_id: options.proposalId,
			mutation_id: options.rollbackMutationId,
			unsupported_operations: unsupportedOperations.map((operation) => ({
				operation_id: operation.operation_id ?? null,
				type: operation.type ?? null,
			})),
		};
		await storeMutationResult(redis, options.rollbackMutationId, result);
		return result;
	}

	const { entriesById, error } = await validateRollbackCurrentRevisions(redis, applyRecord, operations);
	if (error) {
		const result = {
			...error,
			proposal_id: options.proposalId,
			apply_mutation_id: options.applyMutationId,
			mutation_id: options.rollbackMutationId,
		};
		await storeMutationResult(redis, options.rollbackMutationId, result);
		return result;
	}

	const timestamp = new Date().toISOString();
	const rollbackRunId = `rollback_${options.proposalId}_${timestamp.replace(/[:.]/g, "-")}`;
	const touchedIds = [...new Set(operations.flatMap(getOperationTouchedIds))];
	const beforeRevisions = Object.fromEntries(
		touchedIds.map((entryId) => [entryId, getExpectedRevision(entriesById.get(entryId)!)]),
	);
	const rollbackResults: Array<Record<string, unknown>> = [];
	for (const operation of [...operations].reverse()) {
		rollbackResults.push(
			await rollbackDreamOperation(env, redis, vector, applyRecord, operation, rollbackRunId, timestamp),
		);
	}

	if (rollbackResults.some((result) => result.ok === false)) {
		const failedResult = {
			ok: false,
			error: "rollback_failed",
			proposal_id: options.proposalId,
			apply_mutation_id: options.applyMutationId,
			mutation_id: options.rollbackMutationId,
			results: rollbackResults,
		};
		await storeMutationResult(redis, options.rollbackMutationId, failedResult);
		return failedResult;
	}

	await rebuildThinIndexSafely(redis, rollbackRunId);
	const refreshedEntriesById = await loadTouchedEntries(redis, touchedIds);
	const afterRevisions = Object.fromEntries(
		touchedIds.map((entryId) => [
			entryId,
			refreshedEntriesById.has(entryId)
				? getExpectedRevision(refreshedEntriesById.get(entryId)!)
				: null,
		]),
	);
	const result = {
		ok: true,
		proposal_id: options.proposalId,
		apply_mutation_id: options.applyMutationId,
		rollback_run_id: rollbackRunId,
		mutation_id: options.rollbackMutationId,
		rolled_back_at: timestamp,
		rolled_back_by: options.actorId,
		rolled_back_count: rollbackResults.length,
		operation_ids: operations.map((operation) => operation.operation_id),
		results: rollbackResults,
		before_revisions: beforeRevisions,
		after_revisions: afterRevisions,
		side_effects: {
			index: "rebuilt",
		},
	};

	await redis.set(getRollbackAuditKey(options.proposalId, options.rollbackMutationId), JSON.stringify(result));
	await redis.set(`${DREAM_RUN_PREFIX}${options.proposalId}:events`, JSON.stringify([
		{
			ts: timestamp,
			event: "proposal_rollback",
			mutation_id: options.rollbackMutationId,
			apply_mutation_id: options.applyMutationId,
			actor_id: options.actorId,
			reason: options.reason,
			operation_ids: result.operation_ids,
			ids_affected: touchedIds,
		},
	]));
	await appendMutationLog(redis, {
		ts: timestamp,
		mutation_id: options.rollbackMutationId,
		tool: "rollback_dream_apply",
		client: "mcp",
		actor_id: options.actorId,
		request_id: options.rollbackMutationId,
		ids_affected: touchedIds,
		before_revisions: beforeRevisions,
		after_revisions: afterRevisions,
		reason: options.reason,
		proposal_id: options.proposalId,
		apply_mutation_id: options.applyMutationId,
	});
	await storeMutationResult(redis, options.rollbackMutationId, result);
	return result;
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

// ---------------------------------------------------------------------------
// Layer 2 helpers — quarantine + demote (Dream + forgetting design v5).
// Pure metadata transformations. The Stage 3 cycle invokes these and
// persists the entry. Each helper returns a brief audit record describing
// the change for inclusion in the apply log.
// ---------------------------------------------------------------------------

/**
 * Mark an entry as injection-quarantined. The retrieval search layer
 * (Layer 0 / search tool) should treat quarantined entries as not eligible
 * for auto-injection while still being returnable to direct queries.
 * Tier is NOT changed — quarantine is reversible by access reinforcement.
 */
export function quarantineEntryMetadata(
	metadata: Record<string, unknown>,
	timestamp: string,
): { changed: boolean; previous: boolean } {
	const previous = Boolean(metadata.injection_quarantine);
	if (previous) {
		return { changed: false, previous };
	}
	metadata.injection_quarantine = true;
	metadata.quarantined_at = timestamp;
	return { changed: true, previous };
}

/**
 * Clear quarantine. Called whenever an entry is retrieved (any access
 * reinforces the entry — the rule is "reversible by retrieval").
 */
export function liftQuarantineMetadata(
	metadata: Record<string, unknown>,
): { changed: boolean } {
	if (!metadata.injection_quarantine) {
		return { changed: false };
	}
	metadata.injection_quarantine = false;
	metadata.quarantined_at = null;
	metadata.quarantine_streak_nights = 0;
	return { changed: true };
}

/**
 * Demote a tier by exactly one step (1→2 or 2→3). Tier 3 cannot be
 * demoted further by this helper (eligible-for-archive is handled
 * separately by Layer 1).
 * Returns the new tier (unchanged if already 3).
 */
export function demoteTierMetadata(
	metadata: Record<string, unknown>,
	timestamp: string,
): { changed: boolean; from: 1 | 2 | 3; to: 1 | 2 | 3 } {
	const currentTier = resolveStoredInjectionTier(metadata);
	if (currentTier === 3) {
		return { changed: false, from: 3, to: 3 };
	}
	const newTier: 1 | 2 | 3 = currentTier === 1 ? 2 : 3;
	metadata.injection_tier = newTier;
	metadata.last_consolidated = timestamp;
	// Demotion clears the quarantine flag (the entry has moved tier; the
	// streak counter resets so it can earn its way back via reinforcement).
	metadata.injection_quarantine = false;
	metadata.quarantined_at = null;
	metadata.quarantine_streak_nights = 0;
	return { changed: true, from: currentTier, to: newTier };
}

// =============================================================================
// Layer 2 — quarantine + demote (Dream + forgetting design)
// =============================================================================
// Thresholds chosen per the design doc. Kept in code (not policy.json) for
// now — easy to move to shared/memory_policy.json once tuned.
// =============================================================================

/** Salience floor below which the entry starts a "below-threshold" streak.
 *  Phase 4 (R4.3): sourced from policy (dream_thresholds.layer2_quarantine_salience). */
export const LAYER2_QUARANTINE_SALIENCE_THRESHOLD =
	typeof (MEMORY_POLICY.dream_thresholds as Record<string, unknown>).layer2_quarantine_salience === "number"
		? ((MEMORY_POLICY.dream_thresholds as Record<string, unknown>).layer2_quarantine_salience as number)
		: 0.15;
/** Consecutive nights below threshold before quarantine fires. */
export const LAYER2_QUARANTINE_AFTER_NIGHTS = 3;
/** Total consecutive nights below threshold before tier demotion fires. */
export const LAYER2_DEMOTE_AFTER_NIGHTS = 10; // = 3 quarantine + 7 demote
/** Hard cap on Layer 2 mutations per cycle run (quarantine + demote combined). */
export const LAYER2_PER_RUN_CAP = 100;
/**
 * Hard cap on percentile re-tier persists per cycle run (R3.3). After the
 * corpus converges, only a handful of entries change tier each night; this cap
 * bounds the first post-deploy run (which absorbs accumulated drift) so the
 * cycle never blows the Worker subrequest budget. Remaining changes reconcile
 * over subsequent nights. Sorted by largest tier mismatch first.
 */
export const RETIER_PER_RUN_CAP = 400;

/**
 * Run the Layer 2 quarantine + tier-demotion phase across all entries.
 *
 * Rule per entry (tier-1 and tier-2 only — tier 3 is already at the floor):
 *   - If salience >= threshold     → streak resets to 0.
 *   - If salience < threshold      → streak increments by 1.
 *     - streak == LAYER2_QUARANTINE_AFTER_NIGHTS and not quarantined → quarantine.
 *     - streak >= LAYER2_DEMOTE_AFTER_NIGHTS and quarantined         → demote tier.
 *     - else                                                          → streak only.
 *
 * Quarantine is NOT lifted by this phase — the search path lifts it on
 * retrieval reinforcement (see reconsolidateEntry in index.ts).
 *
 * Returns a summary suitable for inclusion in the Dream run record.
 */
export async function applyLayer2QuarantineAndDemote(
	redis: Redis,
	vector: Index,
	entries: LoadedEntry[],
	timestamp: string,
): Promise<{
	quarantined: Array<Record<string, unknown>>;
	demoted: Array<Record<string, unknown>>;
	streak_reset: number;
	streak_increment: number;
	processed: number;
	cap_hit: boolean;
}> {
	const quarantined: Array<Record<string, unknown>> = [];
	const demoted: Array<Record<string, unknown>> = [];
	let streakReset = 0;
	let streakIncrement = 0;
	let processed = 0;
	let capHit = false;

	for (const entry of entries) {
		if (processed >= LAYER2_PER_RUN_CAP) {
			capHit = true;
			break;
		}
		const tier = resolveStoredInjectionTier(entry.metadata);
		if (tier === 3) continue; // already at floor; archive path handles further decay

		const salience =
			typeof entry.salienceScore === "number" ? entry.salienceScore : computeSalience(entry.entry);
		const currentStreak = toOptionalInteger(entry.metadata.quarantine_streak_nights) ?? 0;
		const wasQuarantined = Boolean(entry.metadata.injection_quarantine);

		if (salience >= LAYER2_QUARANTINE_SALIENCE_THRESHOLD) {
			// Salience recovered — reset streak only if non-zero (avoid pointless writes).
			if (currentStreak > 0) {
				entry.metadata.quarantine_streak_nights = 0;
				await persistEntry(redis, vector, entry);
				streakReset += 1;
				processed += 1;
			}
			continue;
		}

		// Below threshold — increment streak and act on the new value.
		const newStreak = currentStreak + 1;
		entry.metadata.quarantine_streak_nights = newStreak;

		if (newStreak >= LAYER2_DEMOTE_AFTER_NIGHTS && wasQuarantined) {
			const result = demoteTierMetadata(entry.metadata, timestamp);
			if (result.changed) {
				appendConsolidationNote(
					entry.metadata,
					formatConsolidationNote({
						timestamp,
						source: "dream",
						action: "demote_tier",
						detail: `tier ${result.from} -> ${result.to} after ${newStreak} nights below ${LAYER2_QUARANTINE_SALIENCE_THRESHOLD}`,
					}),
				);
				await persistEntry(redis, vector, entry);
				demoted.push({
					id: entry.id,
					type: entry.type,
					label: entry.label,
					from_tier: result.from,
					to_tier: result.to,
					salience,
					streak_nights: newStreak,
				});
				processed += 1;
			}
		} else if (newStreak >= LAYER2_QUARANTINE_AFTER_NIGHTS && !wasQuarantined) {
			const result = quarantineEntryMetadata(entry.metadata, timestamp);
			if (result.changed) {
				appendConsolidationNote(
					entry.metadata,
					formatConsolidationNote({
						timestamp,
						source: "dream",
						action: "quarantine_entry",
						detail: `tier ${tier} after ${newStreak} nights below ${LAYER2_QUARANTINE_SALIENCE_THRESHOLD}`,
					}),
				);
				await persistEntry(redis, vector, entry);
				quarantined.push({
					id: entry.id,
					type: entry.type,
					label: entry.label,
					tier,
					salience,
					streak_nights: newStreak,
				});
				processed += 1;
			}
		} else {
			// Just persist streak increment — no tier/quarantine change yet.
			await persistEntry(redis, vector, entry);
			streakIncrement += 1;
			processed += 1;
		}
	}

	return {
		quarantined,
		demoted,
		streak_reset: streakReset,
		streak_increment: streakIncrement,
		processed,
		cap_hit: capHit,
	};
}

/**
 * Phase 3 (R3.3) — recompute injection_tier for the whole active corpus from
 * the salience percentile, then persist the entries whose tier changed.
 *
 * Operates on the cycle's current in-memory entry snapshot (post
 * merge/promote/archive/demote), recomputes salience, and calls
 * assignTierByPercentile over EVERY active entry — the cutoffs are only correct
 * over the full set, which is why this never runs on a candidate-filtered or
 * dry-run cycle. Only entries whose tier actually changes are written, sorted
 * by largest tier mismatch first and bounded by RETIER_PER_RUN_CAP so the first
 * post-deploy run can't exceed the Worker subrequest budget. Per-entry vector
 * failures (e.g. transient Upstash 503s) are counted and skipped, not fatal —
 * the next night reconciles them.
 */
export async function applyPercentileRetier(
	redis: Redis,
	vector: Index,
	entries: LoadedEntry[],
): Promise<{
	evaluated: number;
	changed: number;
	failed: number;
	cap_hit: boolean;
	tier_counts: Record<1 | 2 | 3, number>;
}> {
	const activeEntries = entries.filter((entry) => entry.metadata.archived !== true);

	const salienceById: Record<string, number> = {};
	const contextTypeById: Record<string, string | null> = {};
	for (const entry of activeEntries) {
		const salience = computeSalience(entry.entry);
		entry.metadata.salience_score = salience;
		entry.salienceScore = salience;
		salienceById[entry.id] = salience;
		contextTypeById[entry.id] =
			typeof entry.metadata.context_type === "string" ? entry.metadata.context_type : null;
	}

	const targetTiers = assignTierByPercentile(salienceById, contextTypeById);
	const tierCounts: Record<1 | 2 | 3, number> = { 1: 0, 2: 0, 3: 0 };
	const changes: Array<{ entry: LoadedEntry; target: 1 | 2 | 3; delta: number }> = [];
	for (const entry of activeEntries) {
		const target = targetTiers[entry.id];
		if (!target) continue;
		tierCounts[target] += 1;
		const current = resolveStoredInjectionTier(entry.metadata);
		if (current !== target) {
			changes.push({ entry, target, delta: Math.abs(current - target) });
		}
	}
	// Largest tier mismatch first so a capped run fixes the worst drift.
	changes.sort((a, b) => b.delta - a.delta);

	const capHit = changes.length > RETIER_PER_RUN_CAP;
	const toApply = capHit ? changes.slice(0, RETIER_PER_RUN_CAP) : changes;

	let changed = 0;
	let failed = 0;
	for (const { entry, target } of toApply) {
		entry.metadata.injection_tier = target;
		entry.injectionTier = target;
		try {
			await persistEntry(redis, vector, entry);
			changed += 1;
		} catch (_error) {
			failed += 1;
		}
	}

	return {
		evaluated: activeEntries.length,
		changed,
		failed,
		cap_hit: capHit,
		tier_counts: tierCounts,
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

export async function archiveEntry(
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

	// Mark the ORIGINAL in-memory LoadedEntry archived. archiveEntry above only
	// mutates a freshly-loaded copy (latestEntry); the object passed in still
	// lives in the cycle's `allEntries` snapshot. Later same-run phases that
	// operate on that snapshot (Layer 2 demote, percentile re-tier) filter on
	// entry.metadata.archived — without this, a just-archived entry would pass
	// the filter, get persisted back as active, and have its vector re-created
	// (resurrection), and would also skew the re-tier percentile population.
	entry.metadata.archived = true;
	(entry.entry as Record<string, unknown>).metadata = entry.metadata;
	entry.injectionTier = archivedEntry.injectionTier;

	// Anomaly tripwire: bump the daily destructive-action counter (best-effort).
	await recordDestructiveAction(redis);

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
	await rebuildThinIndexSafely(redis, runId);

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
	await rebuildThinIndexSafely(redis, `restore_${params.entryId}_${timestamp.replace(/[:.]/g, "-")}`);

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
			await redis.del(getEntryAccessKey(duplicate.id), getEntryLastAccessedKey(duplicate.id));

			afterRevisions[duplicate.id] = duplicateMetadata.revision as number;
			archivedResults.push({
				id: duplicate.id,
				archived: true,
				revision: duplicateMetadata.revision,
				snapshot_key: snapshotKey,
			});
		}
		await rebuildThinIndexWithHeldLock(redis, rebuildRunId);
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

		if (contextTypeChanged) {
			await rebuildThinIndexWithHeldLock(redis, rebuildRunId);
		} else {
			await patchThinIndexEntry(redis, loadedEntry, timestamp);
		}
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
	const startedAtMs = Date.parse(startedAt);
	const lockResult = await acquireDreamLock(redis, lockPayload, startedAtMs);
	if (!lockResult.acquired) {
		const blockedBy =
			lockResult.existingLock && typeof lockResult.existingLock.run_id === "string"
				? lockResult.existingLock.run_id
				: null;
		const skippedRecord = {
			...baseRunRecord,
			status: "skipped_locked",
			completed_at: new Date().toISOString(),
			next_action: blockedBy
				? `Wait for the active Dream run (${blockedBy}) to finish before starting another.`
				: "Wait for the active Dream run to finish before starting another.",
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
		// Phase 1: semantic entity resolution (same builder as the proposal path).
		// Gated like the proposal path: targeted (candidate_ids) or explicit
		// options.semantic only. The unfiltered nightly cycle stays lexical-only
		// during ramp; semantic merges run on operator-targeted clusters.
		const cycleRunSemantic = Boolean(candidateIdFilter) || options.semantic === true;
		const cycleSemanticConfig = readSemanticDedupConfig();
		const cycleNeighborFn = cycleRunSemantic
			? makeVectorNeighborFn(vector, cycleSemanticConfig, new Map<string, number[] | null>())
			: null;
		const { duplicatePlans, contradictionPlans: replayContradictionPlans } =
			await buildReplayPlansWithSemantic(replayEntries, cycleNeighborFn, cycleSemanticConfig);
		const replayEntriesById = new Map(replayEntries.map((entry) => [entry.id, entry]));
		const replayContestedIds = new Set(replayContradictionPlans.flatMap((plan) => plan.entryIds));
		const correctionContestPlans = await loadCorrectionContestPlans(redis, replayEntriesById, replayContestedIds);
		const contradictionPlans = [...replayContradictionPlans, ...correctionContestPlans];
		const mergedEntries: Array<Record<string, unknown>> = [];
		const contradictionEntries: Array<Record<string, unknown>> = [];
		const judgeQueueSummary = {
			enqueued: [] as Array<Record<string, unknown>>,
			verdicts_applied: [] as Array<Record<string, unknown>>,
			verdicts_skipped: [] as Array<Record<string, unknown>>,
			opus_mode: "on",
			deferred: 0,
		};

		// Layer 3/4 — read any pending verdicts from the offline judge (Mac
		// script) and act on them. This is always enabled for live cycles so
		// judged items cannot sit forever because a mode flag was unset.
		if (!options.dryRun) {
			try {
				const pendingVerdicts: PendingVerdict[] = await readPendingVerdicts(redis);
				for (const { item, verdict } of pendingVerdicts) {
					if (verdict.verdict === "apply") {
						// For now the only supported borderline op is duplicate_merge.
						// Future op types (promote/demote/hard_delete) plug in here.
						if (item.op_type === "duplicate_merge_borderline") {
							// Re-load the live entries by ID. The queue payload is a
							// trimmed snapshot from enqueue time; applyDuplicateMergePlan
							// needs full LoadedEntry objects with current metadata.
							try {
								if (!item.target_entry_ids || item.target_entry_ids.length < 2) {
									throw new Error("invalid_target_entry_ids");
								}
								const loadedMap = await loadTouchedEntries(redis, item.target_entry_ids);
								const canonicalId = item.target_entry_ids[0];
								const duplicateIds = item.target_entry_ids.slice(1);
								const canonical = loadedMap.get(canonicalId);
								const duplicates = duplicateIds
									.map((id) => loadedMap.get(id))
									.filter((e): e is LoadedEntry => e !== undefined);
								if (!canonical || duplicates.length === 0) {
									throw new Error("entries_no_longer_available");
								}
								const payloadObj = item.payload as Record<string, unknown> | undefined;
								const fingerprint =
									typeof payloadObj?.fingerprint === "string"
										? (payloadObj.fingerprint as string)
										: `judge:${canonicalId}`;
								const plan: DuplicateMergePlan = { fingerprint, canonical, duplicates };
								const result = await applyDuplicateMergePlan(redis, vector, plan, runId, startedAt);
								mergedEntries.push({ ...result, judge_op_id: item.op_id });
								await settleJudgeItem(redis, item.op_id, "applied", { verdict, run_id: runId });
								judgeQueueSummary.verdicts_applied.push({
									op_id: item.op_id,
									op_type: item.op_type,
									verdict_reason: verdict.reason,
								});
							} catch (e) {
								await settleJudgeItem(redis, item.op_id, "stale", {
									verdict,
									error: e instanceof Error ? e.message : String(e),
								});
							}
						} else {
							// Unsupported op type — settle as stale.
							await settleJudgeItem(redis, item.op_id, "stale", {
								verdict,
								reason: "unsupported_op_type_in_this_worker_version",
							});
						}
					} else {
						await settleJudgeItem(redis, item.op_id, "skipped", { verdict });
						judgeQueueSummary.verdicts_skipped.push({
							op_id: item.op_id,
							op_type: item.op_type,
							verdict_reason: verdict.reason,
						});
					}
				}
			} catch (e) {
				console.error("[judge_queue] verdict consumption failed", e);
			}
		}

		if (!options.dryRun) {
			for (const plan of duplicatePlans) {
				// Layer 3 split: borderline routes to judge; bright-line auto-applies.
				// Borderline if any member was retrieved (access > 0) OR the plan is
				// semantic-only (formed by embedding similarity, not exact-title
				// agreement) — Phase 1 R1.6 keeps semantic matches operator/judge
				// gated during ramp.
				const canonicalAccess = plan.canonical.accessCount ?? 0;
				const dupAccess = plan.duplicates.map((d) => d.accessCount ?? 0);
				const borderline = Boolean(plan.semanticOnly) || isDuplicateMergeBorderline({
					canonicalAccessCount: canonicalAccess,
					duplicateAccessCounts: dupAccess,
				});
				// R1.6: semantic-only or access-borderline merges never auto-apply;
				// they always route to the judge queue during live cycles.
				if (borderline) {
					// Enqueue for Mac-side judge to decide on next run.
					const opId = `op_${runId}_dup_${plan.canonical.id}`;
					const item: JudgeQueueItem = {
						op_id: opId,
						op_type: "duplicate_merge_borderline",
						proposal_run_id: runId,
						enqueued_at: startedAt,
						target_entry_ids: [plan.canonical.id, ...plan.duplicates.map((d) => d.id)],
						rubric: buildJudgeRubric("duplicate_merge_borderline"),
						payload: {
							fingerprint: plan.fingerprint,
							semantic_only: Boolean(plan.semanticOnly),
							max_cosine: plan.maxCosine ?? null,
							canonical: {
								id: plan.canonical.id,
								type: plan.canonical.type,
								label: plan.canonical.label,
								access_count: plan.canonical.accessCount,
								salience: plan.canonical.salienceScore,
								current_view:
									typeof plan.canonical.entry.current_view === "string"
										? plan.canonical.entry.current_view
										: null,
							},
							duplicates: plan.duplicates.map((d) => ({
								id: d.id,
								type: d.type,
								label: d.label,
								access_count: d.accessCount,
								salience: d.salienceScore,
								current_view:
									typeof d.entry.current_view === "string" ? d.entry.current_view : null,
							})),
						},
					};
					const enqueued = await enqueueJudgeItem(redis, item);
					if (enqueued) {
						judgeQueueSummary.enqueued.push({
							op_id: opId,
							op_type: item.op_type,
							canonical_id: plan.canonical.id,
							duplicate_ids: plan.duplicates.map((d) => d.id),
						});
					}
					// Don't auto-apply this plan; the judge will decide later.
					continue;
				}
				// Bright-line: auto-apply.
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
		let layer2Summary: Awaited<ReturnType<typeof applyLayer2QuarantineAndDemote>> = {
			quarantined: [],
			demoted: [],
			streak_reset: 0,
			streak_increment: 0,
			processed: 0,
			cap_hit: false,
		};
		let retierSummary: Awaited<ReturnType<typeof applyPercentileRetier>> = {
			evaluated: 0,
			changed: 0,
			failed: 0,
			cap_hit: false,
			tier_counts: { 1: 0, 2: 0, 3: 0 },
		};
		if (!options.dryRun) {
			for (const entry of archiveCandidatesLimited) {
				archivedEntries.push(
					await archiveEntry(redis, vector, entry, runId, startedAt, archiveReason),
				);
			}

			// Layer 2 — quarantine + tier demote (synaptic weakening before pruning).
			// Runs over the same loaded entries snapshot. Note that entries archived
			// above are still in this array but already at tier 3 or with archived=true
			// (the helper skips tier 3 and resolveStoredInjectionTier wouldn't return
			// a demotable tier for an archived entry).
			layer2Summary = await applyLayer2QuarantineAndDemote(
				redis,
				vector,
				allEntries,
				startedAt,
			);

			// Phase 3 (R3.3) — percentile re-tier as the authoritative tier model.
			// Tier comes from the salience percentile of the WHOLE active corpus
			// (top tier_1_top_pct -> T1, next tier_2_next_pct -> T2, rest -> T3),
			// with the identity floor protecting durable context types. Without
			// this, new + reconsolidated entries keep their static
			// context-type default tier and Tier-1 share drifts upward every
			// night. Only runs on the full nightly set (percentiles are
			// meaningless over a candidate-filtered subset) and never in dry-run.
			if (!candidateIdFilter) {
				retierSummary = await applyPercentileRetier(redis, vector, allEntries);
			}

			if (
				mergedEntries.length > 0 ||
				contradictionEntries.length > 0 ||
				promotedEntries.length > 0 ||
				archivedEntries.length > 0 ||
				layer2Summary.quarantined.length > 0 ||
				layer2Summary.demoted.length > 0 ||
				retierSummary.changed > 0
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
					correction_contest_count: correctionContestPlans.length,
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
				layer2_quarantine_and_demote: {
					status: options.dryRun ? "skipped_dry_run" : "completed",
					quarantined_count: layer2Summary.quarantined.length,
					demoted_count: layer2Summary.demoted.length,
					streak_reset_count: layer2Summary.streak_reset,
					streak_increment_count: layer2Summary.streak_increment,
					per_run_cap: LAYER2_PER_RUN_CAP,
					cap_hit: layer2Summary.cap_hit,
					quarantined_entries: layer2Summary.quarantined,
					demoted_entries: layer2Summary.demoted,
				},
				judge_queue: {
					status: options.dryRun ? "skipped_dry_run" : "completed",
					opus_mode: judgeQueueSummary.opus_mode,
					enqueued_count: judgeQueueSummary.enqueued.length,
					verdicts_applied_count: judgeQueueSummary.verdicts_applied.length,
					verdicts_skipped_count: judgeQueueSummary.verdicts_skipped.length,
					enqueued: judgeQueueSummary.enqueued,
					verdicts_applied: judgeQueueSummary.verdicts_applied,
					verdicts_skipped: judgeQueueSummary.verdicts_skipped,
				},
				percentile_retier: {
					status: options.dryRun
						? "skipped_dry_run"
						: candidateIdFilter
							? "skipped_targeted_run"
							: "completed",
					evaluated_count: retierSummary.evaluated,
					changed_count: retierSummary.changed,
					failed_count: retierSummary.failed,
					per_run_cap: RETIER_PER_RUN_CAP,
					cap_hit: retierSummary.cap_hit,
					tier_counts: retierSummary.tier_counts,
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
				correction_contest_candidates: correctionContestPlans.length,
				entries_marked_contested: contradictionEntries.length,
				promotion_candidates: promotionCandidates.length,
				promoted: promotedEntries.length,
				promotion_limit: options.promotionLimit ?? null,
				archive_limit: options.archiveLimit ?? null,
				quarantined: layer2Summary.quarantined.length,
				demoted: layer2Summary.demoted.length,
				retier_evaluated: retierSummary.evaluated,
				retier_changed: retierSummary.changed,
				retier_failed: retierSummary.failed,
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
