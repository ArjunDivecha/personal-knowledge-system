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

		expect(computeSalience(baseEntry, now)).toBe(0.1575);
		expect(
			computeSalience(
				{
					...baseEntry,
					metadata: { ...baseEntry.metadata, signal_flags: ["explicit_save"] },
				},
				now,
			),
		).toBe(0.2363);
		expect(
			computeSalience(
				{
					...baseEntry,
					metadata: {
						...baseEntry.metadata,
						signal_flags: ["correction_derived"],
					},
				},
				now,
			),
		).toBe(0.2835);
		expect(
			computeSalience(
				{
					...baseEntry,
					metadata: {
						...baseEntry.metadata,
						signal_flags: ["explicit_save", "correction_derived"],
					},
				},
				now,
			),
		).toBe(0.4253);
	});
});
