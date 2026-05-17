// Tests for Layer 2 metadata helpers (quarantine + demote tier).
// These are pure functions over metadata records; they don't touch Redis or
// Vector. The Stage 3 cycle code wraps them in persist/audit flow.

import { describe, expect, it } from "vitest";

import {
	quarantineEntryMetadata,
	liftQuarantineMetadata,
	demoteTierMetadata,
} from "../src/dream";

describe("quarantineEntryMetadata", () => {
	it("sets quarantine flag and timestamp on a fresh entry", () => {
		const meta: Record<string, unknown> = { injection_tier: 2 };
		const ts = "2026-05-17T07:00:00.000Z";

		const result = quarantineEntryMetadata(meta, ts);

		expect(result.changed).toBe(true);
		expect(result.previous).toBe(false);
		expect(meta.injection_quarantine).toBe(true);
		expect(meta.quarantined_at).toBe(ts);
		// Tier is NOT changed by quarantine.
		expect(meta.injection_tier).toBe(2);
	});

	it("is a no-op when quarantine is already set", () => {
		const meta: Record<string, unknown> = {
			injection_quarantine: true,
			quarantined_at: "2026-05-10T00:00:00.000Z",
			injection_tier: 2,
		};
		const newTs = "2026-05-17T07:00:00.000Z";

		const result = quarantineEntryMetadata(meta, newTs);

		expect(result.changed).toBe(false);
		expect(result.previous).toBe(true);
		// Existing timestamp preserved — quarantine isn't "renewed".
		expect(meta.quarantined_at).toBe("2026-05-10T00:00:00.000Z");
	});
});

describe("liftQuarantineMetadata", () => {
	it("clears quarantine and streak when set", () => {
		const meta: Record<string, unknown> = {
			injection_quarantine: true,
			quarantined_at: "2026-05-10T00:00:00.000Z",
			quarantine_streak_nights: 5,
		};

		const result = liftQuarantineMetadata(meta);

		expect(result.changed).toBe(true);
		expect(meta.injection_quarantine).toBe(false);
		expect(meta.quarantined_at).toBeNull();
		expect(meta.quarantine_streak_nights).toBe(0);
	});

	it("is a no-op when quarantine was not set", () => {
		const meta: Record<string, unknown> = { injection_quarantine: false };
		const result = liftQuarantineMetadata(meta);
		expect(result.changed).toBe(false);
	});
});

describe("demoteTierMetadata", () => {
	const ts = "2026-05-17T07:00:00.000Z";

	it("demotes 1 → 2 and resets quarantine", () => {
		const meta: Record<string, unknown> = {
			injection_tier: 1,
			injection_quarantine: true,
			quarantined_at: "2026-05-01T00:00:00.000Z",
			quarantine_streak_nights: 14,
		};

		const result = demoteTierMetadata(meta, ts);

		expect(result.changed).toBe(true);
		expect(result.from).toBe(1);
		expect(result.to).toBe(2);
		expect(meta.injection_tier).toBe(2);
		expect(meta.injection_quarantine).toBe(false);
		expect(meta.quarantined_at).toBeNull();
		expect(meta.quarantine_streak_nights).toBe(0);
		expect(meta.last_consolidated).toBe(ts);
	});

	it("demotes 2 → 3", () => {
		const meta: Record<string, unknown> = { injection_tier: 2 };
		const result = demoteTierMetadata(meta, ts);
		expect(result.changed).toBe(true);
		expect(result.from).toBe(2);
		expect(result.to).toBe(3);
		expect(meta.injection_tier).toBe(3);
	});

	it("does not demote below 3", () => {
		const meta: Record<string, unknown> = { injection_tier: 3 };
		const result = demoteTierMetadata(meta, ts);
		expect(result.changed).toBe(false);
		expect(result.from).toBe(3);
		expect(result.to).toBe(3);
		expect(meta.injection_tier).toBe(3);
	});

	it("defaults missing tier via context_type", () => {
		// No injection_tier; default by context_type should resolve to 3
		// for task_query (the safe fallback in resolveStoredInjectionTier).
		const meta: Record<string, unknown> = { context_type: "task_query" };
		const result = demoteTierMetadata(meta, ts);
		// task_query defaults to tier 3, so already at floor.
		expect(result.from).toBe(3);
		expect(result.to).toBe(3);
		expect(result.changed).toBe(false);
	});
});
