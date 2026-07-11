// =============================================================================
// SCRIPT NAME: semanticCursor.ts
// =============================================================================
// INPUT FILES: None. This module has no file I/O of any kind — it reads
//   persisted state from a single Redis key ("dream:semantic_cursor") via a
//   Redis client instance the caller constructs and passes in.
// OUTPUT FILES: None. This module has no file I/O of any kind — it writes
//   the same Redis key; all other results are returned to the caller as
//   plain values.
//
// PURPOSE (PKS-SEMANTIC-CONSOLIDATION-001, INV2 + INV4):
// The nightly Dream proposal's bounded semantic-dedup slice can only afford
// to examine at most 200 candidate entries and issue at most 400 vector
// queries per run (Worker subrequest budget). A single unbounded semantic
// sweep of the whole corpus would blow both limits, so instead the corpus is
// swept in contiguous 200-entry slices, one slice per scheduled night, with
// this module remembering where the previous night's slice ended so the next
// night resumes from there. This is what makes the sweep starvation-free
// (INV4: every active entry is visited within ceil(corpus_size/200)
// consecutive nights) without ever exceeding the per-night bound (INV2).
//
// The cursor is a single position into a list of entry ids that the CALLER
// sorts and re-derives fresh every run (this module has no entry-loading
// machinery and does not persist the id list itself, only the numeric
// position) — see the wiring in index.ts's runScheduledGovernedDream and
// scheduledDreamAsync.ts's executeScheduledGovernedDreamAsync, both of which
// sort candidate entries by (injectionTier ascending, id ascending) before
// calling selectCursorSlice. That sort order is itself the sweep's priority
// rule (contract Context: "tier-1 entries and largest known clusters first")
// — tier-1/tier-2 entries sort first, and the ascending-id tiebreak makes the
// slice boundaries deterministic and stable across nights as the corpus
// grows or shrinks by a small amount, so no entry is silently skipped or
// double-visited purely because Redis SCAN order changed.
//
// Persistence is deliberately crash-safe by construction rather than by
// explicit recovery logic: advanceSemanticCursor is only called AFTER a
// night's semantic pass has actually completed, so a run that crashes
// mid-sweep simply leaves the cursor unmoved and next night's
// selectCursorSlice call returns the exact same slice — the interrupted
// night's work is retried, never skipped (see semanticCursor.test.ts's
// resume-after-crash case).
// =============================================================================

import type { Redis } from "@upstash/redis/cloudflare";

export interface SemanticCursorState {
	position: number;
	cycle_started_at: string;
	total_swept_this_cycle: number;
}

const SEMANTIC_CURSOR_KEY = "dream:semantic_cursor";

function parseCursorState(raw: unknown): SemanticCursorState | null {
	let value: unknown = raw;
	if (typeof raw === "string") {
		try {
			value = JSON.parse(raw);
		} catch {
			return null;
		}
	}
	if (!value || typeof value !== "object") return null;
	const record = value as Record<string, unknown>;
	const position = typeof record.position === "number" ? record.position : null;
	const cycleStartedAt = typeof record.cycle_started_at === "string" ? record.cycle_started_at : null;
	const totalSweptThisCycle =
		typeof record.total_swept_this_cycle === "number" ? record.total_swept_this_cycle : null;
	if (position === null || cycleStartedAt === null || totalSweptThisCycle === null) return null;
	return { position, cycle_started_at: cycleStartedAt, total_swept_this_cycle: totalSweptThisCycle };
}

// Loads the persisted cursor, defaulting to a fresh cycle starting at
// position 0 if nothing has been persisted yet (first-ever run) or the
// stored value is malformed (defensive — never throws on bad state).
export async function loadSemanticCursor(redis: Redis): Promise<SemanticCursorState> {
	const raw = await redis.get(SEMANTIC_CURSOR_KEY);
	return (
		parseCursorState(raw) ?? {
			position: 0,
			cycle_started_at: new Date().toISOString(),
			total_swept_this_cycle: 0,
		}
	);
}

// Advances and persists the cursor after a night's slice has actually been
// swept. `sweptCount` is the number of entries that were in that slice
// (selectCursorSlice's returned length, NOT the corpus size). Wraps modulo
// the current corpus size; a wrap (or an empty corpus) resets the cycle
// bookkeeping so cycle_started_at/total_swept_this_cycle describe the current
// lap around the corpus, not a stale prior one.
export async function advanceSemanticCursor(
	redis: Redis,
	state: SemanticCursorState,
	sweptCount: number,
	totalCorpusSize: number,
): Promise<SemanticCursorState> {
	const now = new Date().toISOString();
	const nextPosition = totalCorpusSize > 0 ? (state.position + sweptCount) % totalCorpusSize : 0;
	const wrapped = totalCorpusSize === 0 || nextPosition <= state.position;
	const next: SemanticCursorState = wrapped
		? { position: nextPosition, cycle_started_at: now, total_swept_this_cycle: sweptCount }
		: {
			position: nextPosition,
			cycle_started_at: state.cycle_started_at,
			total_swept_this_cycle: state.total_swept_this_cycle + sweptCount,
		};
	await redis.set(SEMANTIC_CURSOR_KEY, JSON.stringify(next));
	return next;
}

// Returns a contiguous, wrapping slice of `sortedEntries` of length
// min(sliceSize, sortedEntries.length), starting at `position`. Pure —
// callers sort and fetch the entry list themselves. E.g. position=180,
// sliceSize=200, corpus=300 -> indices [180..299] followed by [0..79].
export function selectCursorSlice<T extends { id: string }>(
	sortedEntries: T[],
	position: number,
	sliceSize: number,
): T[] {
	const total = sortedEntries.length;
	if (total === 0 || sliceSize <= 0) return [];
	const size = Math.min(sliceSize, total);
	const start = ((position % total) + total) % total;
	const slice: T[] = [];
	for (let i = 0; i < size; i += 1) {
		slice.push(sortedEntries[(start + i) % total]);
	}
	return slice;
}
