import { describe, expect, it } from "vitest";

import {
	computeSalience,
	resolveStoredInjectionTier,
	SALIENCE_FIXTURES,
} from "../src/salience";

describe("shared salience policy", () => {
	it("matches the shared fixture contract", () => {
		for (const fixture of SALIENCE_FIXTURES) {
			if (!("entry" in fixture)) continue;
			const now = new Date(fixture.now);
			expect({
				salience_score: computeSalience(fixture.entry, now),
				stored_tier: resolveStoredInjectionTier(fixture.entry.metadata),
			}).toEqual(fixture.expected);
		}
	});

	it("multiplies signal flags with the base type multiplier", () => {
		const now = new Date("2026-03-28T00:00:00Z");
		const baseEntry = {
			confidence: "high",
			metadata: {
				context_type: "task_query",
				mention_count: 10,
				updated_at: "2026-03-28T00:00:00Z",
				last_seen: "2026-03-28T00:00:00Z",
				last_accessed: null,
			},
		};

		// Signal flags multiply the base type multiplier; assert the ordering
		// the multipliers imply (robust to the Phase 2 continuous lever, which
		// scales all four cases by the same richness factor).
		const score = (flags?: string[]) =>
			computeSalience(
				flags
					? { ...baseEntry, metadata: { ...baseEntry.metadata, signal_flags: flags } }
					: baseEntry,
				now,
			);
		const base = score();
		const explicit = score(["explicit_save"]);
		const correction = score(["correction_derived"]);
		const both = score(["explicit_save", "correction_derived"]);

		expect(explicit).toBeGreaterThan(base);
		expect(correction).toBeGreaterThan(explicit);
		expect(both).toBeGreaterThan(correction);
		// explicit_save = 1.5x, correction_derived = 1.8x on the type multiplier;
		// the richness factor cancels in the ratio.
		expect(explicit / base).toBeCloseTo(1.5, 2);
		expect(correction / base).toBeCloseTo(1.8, 2);
	});
});
