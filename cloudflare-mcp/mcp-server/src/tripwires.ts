// =============================================================================
// ANOMALY TRIPWIRES — the one safety scaffold for Dream + forgetting
// =============================================================================
// Codex's non-negotiable safety rail (see design doc Section "Anomaly
// tripwires"). Two threshold-based machine guards plus a hard-delete cap.
// No human in the loop — when an anomaly fires, the relevant kill flag flips
// and the affected mode is forced off until the operator manually clears it.
//
// Why Redis flags (not env-var changes): Cloudflare Workers cannot modify
// their own env vars at runtime. So the "kill switches" are env vars set
// by the operator (the ON switch) AND a Redis flag set by the tripwire
// (the OFF override). The effective mode is the more restrictive of the
// two: env=on  AND  no kill flag  =>  on; otherwise off.
// =============================================================================

import type { Redis } from "@upstash/redis/cloudflare";
import { MEMORY_POLICY } from "./salience";

// ---------------------------------------------------------------------------
// Redis key shape
// ---------------------------------------------------------------------------
const COUNTER_PREFIX = "tripwire";

function destructiveCounterKey(yyyyMmDd: string): string {
	return `${COUNTER_PREFIX}:destructive:${yyyyMmDd}`;
}

function retrievalTotalKey(yyyyMmDd: string): string {
	return `${COUNTER_PREFIX}:retrieval:total:${yyyyMmDd}`;
}

function retrievalHitsKey(yyyyMmDd: string): string {
	return `${COUNTER_PREFIX}:retrieval:hits:${yyyyMmDd}`;
}

function hardDeleteCounterKey(yyyyMmDd: string): string {
	return `${COUNTER_PREFIX}:hard_delete:${yyyyMmDd}`;
}

export function killFlagKey(modeName: KillSwitchName): string {
	return `${COUNTER_PREFIX}:kill:${modeName}`;
}

export type KillSwitchName = "DREAM_AUTO_APPLY_MODE" | "RETRIEVAL_POLICY_MODE" | "RANKING_V2";

// ---------------------------------------------------------------------------
// Thresholds — chosen per the design doc (Section "Anomaly tripwires").
// ---------------------------------------------------------------------------
/** Daily count of destructive actions exceeding this × 14-day median fires. */
export const DESTRUCTIVE_SPIKE_MULTIPLIER = 3;
/** Daily retrieval-hit fraction dropping below 14-day median × this fires. */
export const RETRIEVAL_COLLAPSE_RATIO = 0.7;
/** Number of consecutive day breaches required before fallback fires. */
export const CONSECUTIVE_DAYS_REQUIRED = 2;
/** Hard cap on hard-delete operations per day (irreversible step). */
export const HARD_DELETE_DAILY_CAP_DEFAULT = 5;
/** Trailing window length (in days) used to compute the median baseline. */
export const BASELINE_WINDOW_DAYS = 14;
/**
 * Minimum daily destructive-action count that can trip the spike detector.
 *
 * Only archiveEntry records a destructive action, so the legitimate daily
 * envelope equals the scheduled archive cap (scheduled_archive_limit). With the
 * cap at 50 and a backlog draining at the cap every night, a fixed floor of 20
 * would flag normal autonomy as an anomaly and halt the drain. So we derive the
 * floor from the policy cap with headroom — it must sit ABOVE a maxed-out
 * normal run, catching only sustained excess beyond the operating envelope.
 * Stays in lockstep if the cap is later raised (policy note: toward 100).
 */
const SCHEDULED_ARCHIVE_LIMIT_FOR_FLOOR =
	Number((MEMORY_POLICY.dream_thresholds as Record<string, unknown>)?.scheduled_archive_limit) || 50;
export const DESTRUCTIVE_MIN_ACTIONABLE_FLOOR = Math.max(
	20,
	Math.ceil(SCHEDULED_ARCHIVE_LIMIT_FOR_FLOOR * 1.5),
);

// ---------------------------------------------------------------------------
// Date helpers (UTC, ISO yyyy-mm-dd)
// ---------------------------------------------------------------------------
export function isoDate(d: Date = new Date()): string {
	return d.toISOString().slice(0, 10);
}

function daysAgo(d: Date, n: number): string {
	const out = new Date(d.getTime());
	out.setUTCDate(out.getUTCDate() - n);
	return isoDate(out);
}

// ---------------------------------------------------------------------------
// Counter writes (called from hot paths)
// ---------------------------------------------------------------------------

/**
 * Record one destructive action. Called from archiveEntry, soft-delete,
 * hard-delete. Best-effort — errors are swallowed because counting failures
 * should never break the underlying operation.
 */
export async function recordDestructiveAction(
	redis: Redis,
	when: Date = new Date(),
): Promise<void> {
	try {
		await redis.incr(destructiveCounterKey(isoDate(when)));
	} catch {
		// best-effort; not critical
	}
}

/**
 * Record one hard-delete (separate from destructive total because hard-delete
 * has its own cap and 3-day escalation).
 */
export async function recordHardDelete(
	redis: Redis,
	when: Date = new Date(),
): Promise<number> {
	try {
		const updated = await redis.incr(hardDeleteCounterKey(isoDate(when)));
		return typeof updated === "number" ? updated : 0;
	} catch {
		return 0;
	}
}

/**
 * Check whether today's hard-delete cap has been reached. Returns true if
 * another hard-delete would exceed the cap and therefore should be deferred.
 * Caller should call recordHardDelete _after_ this check.
 */
export async function isHardDeleteCapReached(
	redis: Redis,
	cap: number = HARD_DELETE_DAILY_CAP_DEFAULT,
	when: Date = new Date(),
): Promise<boolean> {
	try {
		const v = await redis.get(hardDeleteCounterKey(isoDate(when)));
		const count = typeof v === "number" ? v : v ? Number(v) : 0;
		return count >= cap;
	} catch {
		// On error, be conservative — don't block.
		return false;
	}
}

/**
 * Record one search query and whether it produced any "hits" (≥1 result
 * above the auto-injection-eligible threshold). Called from the search tool.
 */
export async function recordSearchQuery(
	redis: Redis,
	hit: boolean,
	when: Date = new Date(),
): Promise<void> {
	try {
		const date = isoDate(when);
		await redis.incr(retrievalTotalKey(date));
		if (hit) {
			await redis.incr(retrievalHitsKey(date));
		}
	} catch {
		// best-effort
	}
}

// ---------------------------------------------------------------------------
// Tripwire check (called at cycle start)
// ---------------------------------------------------------------------------

async function readCounter(redis: Redis, key: string): Promise<number> {
	try {
		const v = await redis.get(key);
		if (typeof v === "number") return v;
		if (typeof v === "string") return Number(v) || 0;
		return 0;
	} catch {
		return 0;
	}
}

function median(values: number[]): number {
	if (values.length === 0) return 0;
	const sorted = [...values].sort((a, b) => a - b);
	const mid = Math.floor(sorted.length / 2);
	if (sorted.length % 2) return sorted[mid] ?? 0;
	return ((sorted[mid - 1] ?? 0) + (sorted[mid] ?? 0)) / 2;
}

export interface DestructiveCheckResult {
	tripped: boolean;
	day_counts: Array<{ date: string; count: number }>;
	baseline_median: number;
	threshold: number;
	consecutive_breaches: number;
	reason: string | null;
}

/**
 * Compute whether the destructive-action volume tripwire should fire.
 *
 * Inspects the BASELINE_WINDOW_DAYS days *before* the most recent
 * CONSECUTIVE_DAYS_REQUIRED days, computes the median, then checks
 * whether the most recent N days all exceed DESTRUCTIVE_SPIKE_MULTIPLIER ×
 * that median.
 */
export async function checkDestructiveTripwire(
	redis: Redis,
	now: Date = new Date(),
): Promise<DestructiveCheckResult> {
	// Most recent CONSECUTIVE_DAYS_REQUIRED days — these are the "current" days
	// being evaluated for breach. Day 0 is yesterday (today is in progress).
	const evalDays: Array<{ date: string; count: number }> = [];
	for (let i = 1; i <= CONSECUTIVE_DAYS_REQUIRED; i += 1) {
		const date = daysAgo(now, i);
		evalDays.push({ date, count: await readCounter(redis, destructiveCounterKey(date)) });
	}
	// Baseline window: the next BASELINE_WINDOW_DAYS days before the eval window.
	const baseline: number[] = [];
	for (let i = CONSECUTIVE_DAYS_REQUIRED + 1; i <= CONSECUTIVE_DAYS_REQUIRED + BASELINE_WINDOW_DAYS; i += 1) {
		const date = daysAgo(now, i);
		baseline.push(await readCounter(redis, destructiveCounterKey(date)));
	}
	const baselineMedian = median(baseline);
	const threshold = baselineMedian * DESTRUCTIVE_SPIKE_MULTIPLIER;
	// Need a minimum baseline signal — if median is 0 or tiny, normal governed
	// auto-apply would otherwise look like a spike every active night.
	const effectiveThreshold = Math.max(threshold, DESTRUCTIVE_MIN_ACTIONABLE_FLOOR);
	const consecutiveBreaches = evalDays.filter((d) => d.count > effectiveThreshold).length;
	const tripped = consecutiveBreaches >= CONSECUTIVE_DAYS_REQUIRED;
	return {
		tripped,
		day_counts: evalDays,
		baseline_median: baselineMedian,
		threshold: effectiveThreshold,
		consecutive_breaches: consecutiveBreaches,
		reason: tripped
			? `destructive-action count breached threshold ${effectiveThreshold.toFixed(1)} for ${CONSECUTIVE_DAYS_REQUIRED} consecutive days`
			: null,
	};
}

export interface RetrievalCheckResult {
	tripped: boolean;
	day_ratios: Array<{ date: string; total: number; hits: number; hit_ratio: number }>;
	baseline_median_ratio: number;
	threshold_ratio: number;
	consecutive_breaches: number;
	reason: string | null;
}

/**
 * Compute whether the retrieval-hit collapse tripwire should fire.
 * For each day, hit_ratio = hits / max(total, 1). Days with <10 total
 * queries are ignored (insufficient sample).
 */
export async function checkRetrievalTripwire(
	redis: Redis,
	now: Date = new Date(),
): Promise<RetrievalCheckResult> {
	const MIN_SAMPLES = 10;
	const evalDays: Array<{ date: string; total: number; hits: number; hit_ratio: number }> = [];
	for (let i = 1; i <= CONSECUTIVE_DAYS_REQUIRED; i += 1) {
		const date = daysAgo(now, i);
		const total = await readCounter(redis, retrievalTotalKey(date));
		const hits = await readCounter(redis, retrievalHitsKey(date));
		const hit_ratio = total >= MIN_SAMPLES ? hits / total : -1;
		evalDays.push({ date, total, hits, hit_ratio });
	}
	const baselineRatios: number[] = [];
	for (let i = CONSECUTIVE_DAYS_REQUIRED + 1; i <= CONSECUTIVE_DAYS_REQUIRED + BASELINE_WINDOW_DAYS; i += 1) {
		const date = daysAgo(now, i);
		const total = await readCounter(redis, retrievalTotalKey(date));
		const hits = await readCounter(redis, retrievalHitsKey(date));
		if (total >= MIN_SAMPLES) {
			baselineRatios.push(hits / total);
		}
	}
	const baselineMedian = baselineRatios.length > 0 ? median(baselineRatios) : 0;
	const thresholdRatio = baselineMedian * RETRIEVAL_COLLAPSE_RATIO;
	// Only count breaches on days with enough samples.
	const consecutiveBreaches = evalDays.filter(
		(d) => d.hit_ratio >= 0 && d.hit_ratio < thresholdRatio,
	).length;
	// Don't trip if baseline itself is too small (cold start, low usage).
	const tripped =
		baselineMedian > 0.1 && consecutiveBreaches >= CONSECUTIVE_DAYS_REQUIRED;
	return {
		tripped,
		day_ratios: evalDays,
		baseline_median_ratio: baselineMedian,
		threshold_ratio: thresholdRatio,
		consecutive_breaches: consecutiveBreaches,
		reason: tripped
			? `retrieval hit-ratio fell below ${(thresholdRatio * 100).toFixed(1)}% for ${CONSECUTIVE_DAYS_REQUIRED} consecutive days (baseline median ${(baselineMedian * 100).toFixed(1)}%)`
			: null,
	};
}

// ---------------------------------------------------------------------------
// Kill flag management
// ---------------------------------------------------------------------------

export interface KillFlagRecord {
	tripped_at: string;
	reason: string;
	source_tripwire: "destructive_spike" | "retrieval_collapse" | "hard_delete_cap" | "manual";
}

export async function setKillFlag(
	redis: Redis,
	modeName: KillSwitchName,
	record: KillFlagRecord,
): Promise<void> {
	await redis.set(killFlagKey(modeName), JSON.stringify(record));
}

export async function clearKillFlag(
	redis: Redis,
	modeName: KillSwitchName,
): Promise<void> {
	try {
		await (redis as unknown as { del: (k: string) => Promise<unknown> }).del(
			killFlagKey(modeName),
		);
	} catch {
		// best-effort
	}
}

export async function readKillFlag(
	redis: Redis,
	modeName: KillSwitchName,
): Promise<KillFlagRecord | null> {
	const v = await redis.get(killFlagKey(modeName));
	if (!v) return null;
	if (typeof v === "string") {
		try {
			return JSON.parse(v) as KillFlagRecord;
		} catch {
			return null;
		}
	}
	return v as KillFlagRecord;
}

export interface EffectiveMode {
	effective: "off" | "on" | "governed" | "full";
	env_value: string;
	tripped: boolean;
	trip_record: KillFlagRecord | null;
}

/**
 * Compute the effective mode by combining the env-var setting (operator
 * intent) with any active kill flag (machine override). Off wins.
 */
export async function getEffectiveMode(
	redis: Redis,
	envValue: string | undefined,
	modeName: KillSwitchName,
): Promise<EffectiveMode> {
	const envNormalized = (envValue ?? "off") as "off" | "on" | "governed" | "full";
	if (envNormalized === "off") {
		return { effective: "off", env_value: envNormalized, tripped: false, trip_record: null };
	}
	const trip = await readKillFlag(redis, modeName);
	if (trip) {
		return { effective: "off", env_value: envNormalized, tripped: true, trip_record: trip };
	}
	return { effective: envNormalized, env_value: envNormalized, tripped: false, trip_record: null };
}
