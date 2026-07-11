// =============================================================================
// SCRIPT NAME: mergeGates.ts
// =============================================================================
// INPUT FILES: none (pure logic; operates on entry objects already loaded by
//   the caller). OUTPUT FILES: none — returns values, writes nothing.
//
// PURPOSE (PKS-SEMANTIC-CONSOLIDATION-001, "Ring 1" hard gates):
// Today's duplicate-merge apply path (dream.ts's mergeCanonicalEntry) unions
// evidence-bearing arrays and sums/min/max's metadata correctly *by
// construction*, but nothing VERIFIES that after the fact, and near-identical
// paraphrased insights survive as separate array entries forever (the
// flagship example, ke_4dbf732e757d, carries 9 near-duplicate insights from
// repeated merges) because the existing dedup (mergeObjectArraysUnique) only
// collapses byte-for-byte identical objects.
//
// This module adds two things, both deterministic (no LLM call — semantic
// *judgment* of entailment is explicitly deferred to a follow-on contract
// once the Mac judge pipeline is operational; see the contract's Context
// section):
//   1. collapseNearDuplicateInsights — a cheap, deterministic near-duplicate
//      collapse over normalized-text word overlap (Jaccard similarity),
//      returning both the collapsed list and a drop-to-retained mapping so
//      no insight ever disappears without a receipt (INV5).
//   2. validateMergeConservation — an independent, post-hoc HARD GATE that
//      recomputes what the merge *should* have produced from the parent
//      entries alone and compares it against what mergeCanonicalEntry
//      actually produced (plus the insight-collapse mapping). A merge that
//      fails this check must never be persisted (INV3).
//
// Called from dream.ts's applyDuplicateMergePlan, which is the single choke
// point for every duplicate_merge apply (both the governed nightly path via
// applyDreamProposalOperation and the legacy operator path both route
// through it) — see the Dream architecture map in this contract's ledger for
// the full call-graph citation.
// =============================================================================

type JsonRecord = Record<string, unknown>;

export interface InsightDrop {
	droppedIndex: number;
	retainedIndex: number;
	droppedInsight: string;
	retainedInsight: string;
	similarity: number;
	// The dropped insight's OWN evidence key (see toEvidenceKey below), or
	// null if it had no attributable evidence. validateMergeConservation uses
	// this for an EXACT identity match — "this specific missing evidence key
	// belongs to this specific receipted drop" — not merely a count
	// comparison, which could coincidentally forgive an unrelated real loss
	// that happens to share a count with a legitimate drop in the same merge.
	droppedEvidenceKey: string | null;
}

export interface CollapseResult {
	kept: JsonRecord[];
	drops: InsightDrop[];
}

const DEFAULT_NEAR_DUPLICATE_THRESHOLD = 0.85;

function normalizeInsightText(text: unknown): string {
	if (typeof text !== "string") return "";
	return text.toLowerCase().replace(/[^\w\s]/g, " ").trim();
}

function wordSet(text: string): Set<string> {
	return new Set(text.split(/\s+/).filter((w) => w.length > 0));
}

// Jaccard similarity over normalized word sets. Deterministic, symmetric,
// cheap — no embeddings, no LLM. 1.0 for identical text, 0.0 for disjoint or
// empty-vs-nonempty text.
export function insightTextSimilarity(a: unknown, b: unknown): number {
	const setA = wordSet(normalizeInsightText(a));
	const setB = wordSet(normalizeInsightText(b));
	if (setA.size === 0 || setB.size === 0) return 0;
	let intersection = 0;
	for (const word of setA) {
		if (setB.has(word)) intersection += 1;
	}
	const union = setA.size + setB.size - intersection;
	return union === 0 ? 0 : intersection / union;
}

function toEvidenceKey(evidence: unknown): string | null {
	if (!evidence || typeof evidence !== "object") return null;
	const ev = evidence as JsonRecord;
	const conversationId = typeof ev.conversation_id === "string" ? ev.conversation_id : "";
	const messageIds = Array.isArray(ev.message_ids)
		? [...(ev.message_ids as unknown[])].map(String).sort().join(",")
		: "";
	const snippet = typeof ev.snippet === "string" ? ev.snippet : "";
	if (!conversationId && !messageIds && !snippet) return null;
	return `${conversationId}::${messageIds}::${snippet}`;
}

// Collapses near-duplicate insights (by normalized word-overlap similarity)
// within a single already-unioned key_insights array (i.e. call this AFTER
// the existing exact-dedup union, on the merge result, not on each parent
// individually). Keeps the FIRST occurrence in array order (parents are
// unioned canonical-first, so the canonical entry's own insights are
// preferred as the retained ones when a tie exists) and maps every
// subsequent near-duplicate to it. Never mutates the input array.
export function collapseNearDuplicateInsights(
	insights: JsonRecord[],
	threshold: number = DEFAULT_NEAR_DUPLICATE_THRESHOLD,
): CollapseResult {
	const kept: JsonRecord[] = [];
	const keptOriginalIndex: number[] = [];
	const drops: InsightDrop[] = [];

	insights.forEach((candidate, index) => {
		const candidateText = candidate.insight;
		let matchedKeptIdx = -1;
		let matchedSimilarity = 0;
		for (let k = 0; k < kept.length; k += 1) {
			const sim = insightTextSimilarity(candidateText, kept[k].insight);
			if (sim >= threshold && sim > matchedSimilarity) {
				matchedKeptIdx = k;
				matchedSimilarity = sim;
			}
		}
		if (matchedKeptIdx === -1) {
			kept.push(candidate);
			keptOriginalIndex.push(index);
		} else {
			drops.push({
				droppedIndex: index,
				retainedIndex: keptOriginalIndex[matchedKeptIdx],
				droppedInsight: String(candidateText ?? ""),
				retainedInsight: String(kept[matchedKeptIdx].insight ?? ""),
				similarity: Math.round(matchedSimilarity * 10000) / 10000,
				droppedEvidenceKey: toEvidenceKey(candidate.evidence),
			});
		}
	});

	return { kept, drops };
}

function collectEvidenceKeys(entry: JsonRecord): Set<string> {
	const keys = new Set<string>();
	const blocks = ["positions", "key_insights", "knows_how_to", "open_questions", "evolution", "decisions_made"];
	for (const block of blocks) {
		const items = entry[block];
		if (!Array.isArray(items)) continue;
		for (const item of items) {
			if (!item || typeof item !== "object") continue;
			const key = toEvidenceKey((item as JsonRecord).evidence);
			if (key) keys.add(key);
		}
	}
	return keys;
}

function toOptionalInt(value: unknown): number | null {
	if (typeof value === "number" && Number.isFinite(value)) return Math.trunc(value);
	if (typeof value === "string" && value.trim() !== "") {
		const parsed = Number(value);
		return Number.isFinite(parsed) ? Math.trunc(parsed) : null;
	}
	return null;
}

function stringArray(value: unknown): string[] {
	return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

function minIso(values: string[]): string | null {
	let best: string | null = null;
	let bestTime = Number.POSITIVE_INFINITY;
	for (const v of values) {
		const t = new Date(v).getTime();
		if (!Number.isNaN(t) && t < bestTime) {
			bestTime = t;
			best = v;
		}
	}
	return best;
}

function maxIso(values: string[]): string | null {
	let best: string | null = null;
	let bestTime = Number.NEGATIVE_INFINITY;
	for (const v of values) {
		const t = new Date(v).getTime();
		if (!Number.isNaN(t) && t > bestTime) {
			bestTime = t;
			best = v;
		}
	}
	return best;
}

export interface MergeConservationInput {
	// Parent entries' `entry` + `metadata` BEFORE merge (the canonical entry's
	// pre-merge snapshot must be passed here too — mergeCanonicalEntry mutates
	// its `canonical` argument in place, so the caller must deep-clone it
	// beforehand; this function never assumes it can recover "before" state).
	parents: Array<{ entry: JsonRecord; metadata: JsonRecord }>;
	// The merged entry AFTER mergeCanonicalEntry + collapseNearDuplicateInsights.
	merged: { entry: JsonRecord; metadata: JsonRecord };
	// The drop mapping collapseNearDuplicateInsights produced for this merge's
	// key_insights (empty array if the entry type isn't "knowledge" or nothing
	// collapsed) — required so a legitimately-collapsed insight is not
	// mistaken for an evidence-loss violation.
	insightDrops: InsightDrop[];
}

export interface MergeConservationResult {
	ok: boolean;
	violations: string[];
}

// The hard gate (INV3): independently recomputes what conservation requires
// from the parents alone and checks the actual merged entry against it.
// Returns ok=false with every violated rule named if anything fails — the
// caller MUST NOT persist a merge this function rejects.
export function validateMergeConservation(input: MergeConservationInput): MergeConservationResult {
	const violations: string[] = [];
	const { parents, merged, insightDrops } = input;

	// --- Evidence conservation (H1) ---
	// Every distinct evidence key across all parents must survive somewhere in
	// the merged entry OR be named as the `droppedInsight` side of an
	// insightDrops mapping (a deliberate, receipted collapse, not a loss).
	const parentEvidenceKeys = new Set<string>();
	for (const parent of parents) {
		for (const key of collectEvidenceKeys(parent.entry)) parentEvidenceKeys.add(key);
	}
	const mergedEvidenceKeys = collectEvidenceKeys(merged.entry);
	// A missing evidence key is forgiven ONLY if it is EXACTLY the evidence
	// key of a specific receipted drop — an identity match, not a count
	// comparison. A count-based "N missing <= N drops" check would wrongly
	// forgive an unrelated real loss that happens to share a count with a
	// legitimate drop in the same merge; this does not.
	const explainedByDrops = new Set(
		insightDrops.map((d) => d.droppedEvidenceKey).filter((k): k is string => k !== null),
	);
	const missingKeys = [...parentEvidenceKeys].filter((k) => !mergedEvidenceKeys.has(k));
	const unexplainedMissingKeys = missingKeys.filter((k) => !explainedByDrops.has(k));
	if (unexplainedMissingKeys.length > 0) {
		violations.push(
			`evidence_conservation: ${unexplainedMissingKeys.length} evidence entr` +
				`${unexplainedMissingKeys.length === 1 ? "y" : "ies"} present in parents but absent from the merged ` +
				`entry, not attributable to any receipted insight drop`,
		);
	}

	// --- Metadata monotonicity (H2) ---
	const expectedSourceConversations = new Set<string>();
	const expectedSourceMessages = new Set<string>();
	const firstSeenCandidates: string[] = [];
	const lastSeenCandidates: string[] = [];
	let sumMentionCount = 0;
	for (const parent of parents) {
		for (const c of stringArray(parent.metadata.source_conversations)) expectedSourceConversations.add(c);
		for (const m of stringArray(parent.metadata.source_messages)) expectedSourceMessages.add(m);
		if (typeof parent.metadata.first_seen === "string") firstSeenCandidates.push(parent.metadata.first_seen);
		if (typeof parent.metadata.created_at === "string") firstSeenCandidates.push(parent.metadata.created_at);
		if (typeof parent.metadata.last_seen === "string") lastSeenCandidates.push(parent.metadata.last_seen);
		if (typeof parent.metadata.updated_at === "string") lastSeenCandidates.push(parent.metadata.updated_at);
		sumMentionCount += toOptionalInt(parent.metadata.mention_count) ?? 1;
	}

	const mergedSourceConversations = new Set(stringArray(merged.metadata.source_conversations));
	for (const c of expectedSourceConversations) {
		if (!mergedSourceConversations.has(c)) {
			violations.push(`metadata_monotonicity: source_conversations lost "${c}" (union of parents not preserved)`);
			break;
		}
	}
	const mergedSourceMessages = new Set(stringArray(merged.metadata.source_messages));
	for (const m of expectedSourceMessages) {
		if (!mergedSourceMessages.has(m)) {
			violations.push(`metadata_monotonicity: source_messages lost "${m}" (union of parents not preserved)`);
			break;
		}
	}

	// mention_count: mirrors mergeCanonicalEntry's own rule (source_conversations.length
	// when non-empty, else the parent sum) — this check proves the ACTUAL code
	// followed that rule, not a hardcoded re-derivation drifting from it.
	const expectedMentionCount =
		mergedSourceConversations.size > 0 ? mergedSourceConversations.size : sumMentionCount;
	const actualMentionCount = toOptionalInt(merged.metadata.mention_count) ?? 0;
	if (actualMentionCount !== expectedMentionCount) {
		violations.push(
			`metadata_monotonicity: mention_count is ${actualMentionCount}, expected ${expectedMentionCount}`,
		);
	}

	const expectedFirstSeen = minIso(firstSeenCandidates);
	const actualFirstSeen = typeof merged.metadata.first_seen === "string" ? merged.metadata.first_seen : null;
	if (expectedFirstSeen && actualFirstSeen && new Date(actualFirstSeen).getTime() > new Date(expectedFirstSeen).getTime()) {
		violations.push(
			`metadata_monotonicity: first_seen ${actualFirstSeen} is later than the earliest parent value ${expectedFirstSeen}`,
		);
	}

	const expectedLastSeen = maxIso(lastSeenCandidates);
	const actualLastSeen = typeof merged.metadata.last_seen === "string" ? merged.metadata.last_seen : null;
	if (expectedLastSeen && actualLastSeen && new Date(actualLastSeen).getTime() < new Date(expectedLastSeen).getTime()) {
		violations.push(
			`metadata_monotonicity: last_seen ${actualLastSeen} is earlier than the latest parent value ${expectedLastSeen}`,
		);
	}

	return { ok: violations.length === 0, violations };
}

// --- Protected-type exclusion (INV1) ---
export function isProtectedContextType(contextType: unknown, protectedTypes: string[]): boolean {
	return typeof contextType === "string" && protectedTypes.includes(contextType);
}
