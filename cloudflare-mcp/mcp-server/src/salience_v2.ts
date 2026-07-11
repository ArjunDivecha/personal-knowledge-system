// =============================================================================
// SALIENCE V2 — five-component additive score (shadow phase, TypeScript twin)
// =============================================================================
// TypeScript twin of distillation/utils/salience_v2.py. Implements salience_v2
// for contract PKS-INJECTION-RANKING-002
// (/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/contracts/injection-ranking-v2.spec.md),
// Phase A (shadow):
//
//   salience_v2 = 0.30*usage + 0.25*evidence + 0.20*recency
//               + 0.15*authority + 0.10*corroboration
//
// This file MUST stay semantically identical to the Python module; the
// shared fixture table shared/salience_v2_fixtures.json is the lockstep
// proof (the vitest suite and the Python unittest both replay it).
//
// Shadow phase only: this module computes and returns values. It is wired
// into the nightly Dream pass (dream.ts's loadEntryBatchByType) as an
// additive shadow write and is otherwise not consulted by live ranking or
// tiering until the RANKING_V2 flag is on (Phase B; see mmr.ts and the
// RANKING_V2 wiring in index.ts's search tool handler).
//
// DESIGN DECISION — distinct_days_seen: see the matching, more detailed
// comment in distillation/utils/salience_v2.py's module docstring. Summary:
// no per-observation-day field exists, so distinct_days_seen is a deliberate
// LOWER-BOUND PROXY built from the unique calendar dates (UTC) in
// metadata.first_seen, metadata.last_seen, and the leading ISO timestamp of
// every metadata.consolidation_notes entry (format verified against
// consolidation.ts's formatConsolidationNote: "<iso> | source=... |
// action=... | detail=...", not the "[iso] ..." bracket format an earlier
// draft assumed).
//
// INPUT FILES:
// - shared/memory_policy.json (imported below; the "salience_v2" block)
// - shared/salience_v2_fixtures.json (imported below)
// OUTPUT FILES:
// - None (pure logic; returns values, writes nothing)
// =============================================================================

import memoryPolicy from "../../../shared/memory_policy.json";
import salienceV2Fixtures from "../../../shared/salience_v2_fixtures.json";
import { AUTHORITY_RANK, authorityOf, authorityRollup } from "./precedence";

type JsonRecord = Record<string, unknown>;

const POLICY = memoryPolicy as unknown as JsonRecord;
export const SALIENCE_V2_FIXTURES = salienceV2Fixtures;

// 3.5 (mirrored from salience.ts's computeSalience) — when a required
// timestamp is absent, fall back to a conservative 90-day-old value rather
// than "now". Must stay in lockstep with salience_v2.py.
const MISSING_TIMESTAMP_FALLBACK_DAYS = 90;

const MAX_AUTHORITY_RANK = Math.max(...Object.values(AUTHORITY_RANK)); // 4

export interface SalienceV2Components {
	usage: number;
	evidence: number;
	recency: number;
	authority: number;
	corroboration: number;
}

export interface SalienceV2Result {
	score: number;
	components: SalienceV2Components;
}

function toDate(value: unknown): Date | null {
	if (typeof value !== "string" || value.length === 0) {
		return null;
	}
	const parsed = new Date(value);
	return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function toStringArray(value: unknown): string[] {
	if (!Array.isArray(value)) {
		return [];
	}
	return value.filter((item): item is string => typeof item === "string");
}

function clamp01(value: number): number {
	return Math.max(0, Math.min(1, value));
}

// sat(x, cap) = min(x, cap) / cap. Linear saturation (deliberately NOT the
// log1p saturation salience.ts uses for v1's richness lever — salience_v2's
// evidence/corroboration components use a plain linear ramp per the locked
// contract formula).
function sat(x: number, cap: number): number {
	if (cap <= 0) return 0;
	return Math.max(0, Math.min(x, cap)) / cap;
}

function round4(value: number): number {
	return Math.round(value * 10000) / 10000;
}

function dateOnlyIso(d: Date): string {
	return d.toISOString().slice(0, 10);
}

function distinctDaysSeen(metadata: JsonRecord): number {
	const days = new Set<string>();
	for (const key of ["first_seen", "last_seen"]) {
		const d = toDate(metadata[key]);
		if (d) days.add(dateOnlyIso(d));
	}
	for (const note of toStringArray(metadata.consolidation_notes)) {
		const timestampCandidate = note.split(" | ", 1)[0]?.trim();
		const d = toDate(timestampCandidate);
		if (d) days.add(dateOnlyIso(d));
	}
	return days.size;
}

function effectiveLastSeen(metadata: JsonRecord, now: Date): Date {
	const raw = metadata.last_seen ?? metadata.updated_at;
	return toDate(raw) ?? new Date(now.getTime() - MISSING_TIMESTAMP_FALLBACK_DAYS * 86400000);
}

export function computeSalienceV2(entry: JsonRecord, now: Date = new Date()): SalienceV2Result {
	const cfg = POLICY.salience_v2 as JsonRecord | undefined;
	if (!cfg) {
		throw new Error("shared/memory_policy.json is missing the required 'salience_v2' block");
	}
	const metadata = (entry.metadata as JsonRecord | undefined) ?? {};

	const weights = cfg.weights as Record<string, number>;
	const usageHalfLifeDays = Number(cfg.usage_half_life_days);
	const recencyHalfLives = cfg.recency_half_lives_days as Record<string, number | "infinity">;
	const evidenceSatCaps = cfg.evidence_saturation as Record<string, number>;
	const evidenceComponentWeights = cfg.evidence_component_weights as Record<string, number>;
	const corroborationCap = Number(cfg.corroboration_saturation_mention_count);

	// --- usage: 0.5^(days_since_last_accessed / usage_half_life), 0 if never accessed ---
	const lastAccessed = toDate(metadata.last_accessed);
	let usage = 0;
	if (lastAccessed) {
		const daysSinceAccess = Math.max(0, (now.getTime() - lastAccessed.getTime()) / 86400000);
		usage = 0.5 ** (daysSinceAccess / usageHalfLifeDays);
	}

	// --- evidence: saturating blend of source breadth, key insights, distinct days seen ---
	const nSourceConversations = new Set(toStringArray(metadata.source_conversations)).size;
	const nKeyInsights = Array.isArray(entry.key_insights) ? entry.key_insights.length : 0;
	const distinctDays = distinctDaysSeen(metadata);
	const evidence =
		sat(nSourceConversations, evidenceSatCaps.source_conversations) * evidenceComponentWeights.source_conversations +
		sat(nKeyInsights, evidenceSatCaps.key_insights) * evidenceComponentWeights.key_insights +
		sat(distinctDays, evidenceSatCaps.distinct_days_seen) * evidenceComponentWeights.distinct_days_seen;

	// --- recency: 0.5^(days_since_last_seen / half_life[context_type]); "infinity" -> 1.0 ---
	const contextType = typeof metadata.context_type === "string" ? metadata.context_type : "task_query";
	const halfLifeRaw = recencyHalfLives[contextType] ?? recencyHalfLives.task_query;
	let recency: number;
	if (halfLifeRaw === "infinity") {
		recency = 1.0;
	} else {
		const lastSeen = effectiveLastSeen(metadata, now);
		const daysSinceSeen = Math.max(0, (now.getTime() - lastSeen.getTime()) / 86400000);
		recency = 0.5 ** (daysSinceSeen / Number(halfLifeRaw));
	}

	// --- authority: authorityOf(assertedBy, behavioral=false) / max rank (4) ---
	const [assertedBy] = authorityRollup(entry);
	const authority = authorityOf(assertedBy, false) / MAX_AUTHORITY_RANK;

	// --- corroboration: sat(mention_count, cap) ---
	const mentionCountRaw = typeof metadata.mention_count === "number" ? metadata.mention_count : 0;
	const mentionCount = Math.max(0, Math.trunc(mentionCountRaw));
	const corroboration = sat(mentionCount, corroborationCap);

	const usageC = clamp01(usage);
	const evidenceC = clamp01(evidence);
	const recencyC = clamp01(recency);
	const authorityC = clamp01(authority);
	const corroborationC = clamp01(corroboration);

	const score = round4(
		usageC * weights.usage +
			evidenceC * weights.evidence +
			recencyC * weights.recency +
			authorityC * weights.authority +
			corroborationC * weights.corroboration,
	);

	return {
		score,
		components: {
			usage: round4(usageC),
			evidence: round4(evidenceC),
			recency: round4(recencyC),
			authority: round4(authorityC),
			corroboration: round4(corroborationC),
		},
	};
}

// evidence_count = len(key_insights) + len(positions) + len(knows_how_to),
// used only by compareByTiebreak (INV3).
export function evidenceCountOf(entry: JsonRecord): number {
	let total = 0;
	for (const block of ["key_insights", "positions", "knows_how_to"]) {
		const items = entry[block];
		if (Array.isArray(items)) total += items.length;
	}
	return total;
}

function tiebreakLastSeenEpoch(entry: JsonRecord): number {
	const metadata = (entry.metadata as JsonRecord | undefined) ?? {};
	const raw = metadata.last_seen ?? metadata.updated_at;
	const d = toDate(raw);
	// A pure ordering key must not depend on wall-clock time at call time (a
	// "now"-based fallback here would make the same two entries compare
	// differently across sort() invocations); missing timestamps sort as
	// oldest (epoch 0), matching salience_v2.py's tiebreak_key.
	return d ? d.getTime() : 0;
}

// INV3: ordering ties break by (salience_v2 desc, last_seen desc,
// evidence_count desc, id asc) — entry-id order decides only when every
// preceding key is equal. Standard comparator for Array.prototype.sort.
export function compareByTiebreak(
	a: { entry: JsonRecord; salienceV2: number },
	b: { entry: JsonRecord; salienceV2: number },
): number {
	if (a.salienceV2 !== b.salienceV2) return b.salienceV2 - a.salienceV2;
	const aLastSeen = tiebreakLastSeenEpoch(a.entry);
	const bLastSeen = tiebreakLastSeenEpoch(b.entry);
	if (aLastSeen !== bLastSeen) return bLastSeen - aLastSeen;
	const aEvidence = evidenceCountOf(a.entry);
	const bEvidence = evidenceCountOf(b.entry);
	if (aEvidence !== bEvidence) return bEvidence - aEvidence;
	const aId = String(a.entry.id ?? "");
	const bId = String(b.entry.id ?? "");
	return aId < bId ? -1 : aId > bId ? 1 : 0;
}
