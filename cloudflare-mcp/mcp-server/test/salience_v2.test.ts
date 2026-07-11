// =============================================================================
// SCRIPT NAME: salience_v2.test.ts
// =============================================================================
// INPUT FILES: shared/salience_v2_fixtures.json (via src/salience_v2.ts's import)
// OUTPUT FILES: None.
//
// Covers INV2 and INV3 of contract PKS-INJECTION-RANKING-002 for the
// TypeScript twin: computeSalienceV2 must agree with the Python
// implementation (distillation/utils/salience_v2.py) on every case in the
// SAME shared fixture table replayed by tests/python/test_salience_v2.py —
// that agreement, not either suite alone, is the lockstep proof the two
// implementations stay in sync (matching the existing salience.ts/salience.py
// pattern in test/salience.test.ts). Also covers compareByTiebreak (INV3)
// directly, mirroring test_salience_v2.py's TiebreakOrderingTests.
// =============================================================================

import { describe, expect, it } from "vitest";

import {
	compareByTiebreak,
	computeSalienceV2,
	SALIENCE_V2_FIXTURES,
} from "../src/salience_v2";

describe("shared salience_v2 fixtures", () => {
	it("has adequate coverage", () => {
		expect((SALIENCE_V2_FIXTURES as unknown[]).length).toBeGreaterThanOrEqual(8);
	});

	it("matches the shared fixture contract for every case", () => {
		for (const fixture of SALIENCE_V2_FIXTURES as Array<{
			name: string;
			now: string;
			entry: Record<string, unknown>;
			expected: { salience_v2: number; components: Record<string, number> };
		}>) {
			const now = new Date(fixture.now);
			const result = computeSalienceV2(fixture.entry, now);
			expect(result.score, `fixture "${fixture.name}" score`).toBe(fixture.expected.salience_v2);
			expect(result.components, `fixture "${fixture.name}" components`).toEqual(fixture.expected.components);
		}
	});
});

describe("salience_v2 — direct assertions", () => {
	it("active_project recency now decays at a 180-day half-life (distinct from v1's infinite half-life)", () => {
		const entry = {
			id: "ke_active_project_direct",
			metadata: {
				context_type: "active_project",
				mention_count: 1,
				last_seen: "2026-01-11T00:00:00Z",
				last_accessed: null,
				source_conversations: ["c1"],
			},
			key_insights: [],
		};
		const now = new Date("2026-07-10T00:00:00Z");
		const { components } = computeSalienceV2(entry, now);
		expect(components.recency).toBe(0.5);
	});

	it("explicit_save recency stays 1.0 regardless of age", () => {
		const entry = {
			id: "ke_explicit_save_direct",
			metadata: {
				context_type: "explicit_save",
				mention_count: 1,
				last_seen: "2020-01-01T00:00:00Z",
				last_accessed: null,
				source_conversations: ["c1"],
			},
			key_insights: [],
		};
		const now = new Date("2026-07-10T00:00:00Z");
		const { components } = computeSalienceV2(entry, now);
		expect(components.recency).toBe(1.0);
	});
});

describe("compareByTiebreak — INV3: (salience_v2 desc, last_seen desc, evidence_count desc, id asc)", () => {
	const entry = (id: string, lastSeen: string | null, nInsights: number, nPositions = 0, nKnows = 0) => ({
		id,
		metadata: lastSeen ? { last_seen: lastSeen } : {},
		key_insights: Array.from({ length: nInsights }, () => ({})),
		positions: Array.from({ length: nPositions }, () => ({})),
		knows_how_to: Array.from({ length: nKnows }, () => ({})),
	});

	it("higher salience_v2 wins regardless of other fields", () => {
		const a = { entry: entry("ke_a", "2020-01-01T00:00:00Z", 0), salienceV2: 0.9 };
		const b = { entry: entry("ke_b", "2026-01-01T00:00:00Z", 5), salienceV2: 0.1 };
		expect([b, a].sort(compareByTiebreak).map((x) => x.entry.id)).toEqual(["ke_a", "ke_b"]);
	});

	it("equal salience breaks on last_seen desc", () => {
		const older = { entry: entry("ke_older", "2026-01-01T00:00:00Z", 0), salienceV2: 0.5 };
		const newer = { entry: entry("ke_newer", "2026-06-01T00:00:00Z", 0), salienceV2: 0.5 };
		expect([older, newer].sort(compareByTiebreak).map((x) => x.entry.id)).toEqual(["ke_newer", "ke_older"]);
	});

	it("equal salience and last_seen breaks on evidence_count desc", () => {
		const thin = { entry: entry("ke_thin", "2026-01-01T00:00:00Z", 1), salienceV2: 0.5 };
		const rich = { entry: entry("ke_rich", "2026-01-01T00:00:00Z", 2, 3, 1), salienceV2: 0.5 };
		expect([thin, rich].sort(compareByTiebreak).map((x) => x.entry.id)).toEqual(["ke_rich", "ke_thin"]);
	});

	it("a full tie falls back to id ascending", () => {
		const z = { entry: entry("ke_z", "2026-01-01T00:00:00Z", 1), salienceV2: 0.5 };
		const a = { entry: entry("ke_a", "2026-01-01T00:00:00Z", 1), salienceV2: 0.5 };
		expect([z, a].sort(compareByTiebreak).map((x) => x.entry.id)).toEqual(["ke_a", "ke_z"]);
	});

	it("a missing last_seen sorts as oldest", () => {
		const missing = { entry: entry("ke_missing", null, 0), salienceV2: 0.5 };
		const present = { entry: entry("ke_present", "2020-01-01T00:00:00Z", 0), salienceV2: 0.5 };
		expect([missing, present].sort(compareByTiebreak).map((x) => x.entry.id)).toEqual(["ke_present", "ke_missing"]);
	});
});
