// =============================================================================
// SCRIPT NAME: precedence.test.ts
// =============================================================================
// INPUT FILES: shared/precedence_fixtures.json (via src/precedence.ts's import)
// OUTPUT FILES: None.
//
// Covers INV2 of contract PKS-CONTRADICTION-LIFECYCLE-001 for the TypeScript
// twin: compareClaims must agree with the Python implementation
// (distillation/utils/precedence.py) on every case in the SAME shared fixture
// table replayed by tests/python/test_precedence_lattice.py — that agreement,
// not either suite alone, is the lockstep proof the two implementations stay
// in sync (matching the existing salience.ts/salience.py pattern in
// test/salience.test.ts). Also covers deriveAssertedBy directly, mirroring
// tests/python/test_provenance_capture.py's DeriveAssertedByTests.
// =============================================================================

import { describe, expect, it } from "vitest";

import {
	compareClaims,
	deriveAssertedBy,
	PRECEDENCE_FIXTURES,
} from "../src/precedence";

describe("shared precedence lattice fixtures", () => {
	it("has adequate coverage", () => {
		expect((PRECEDENCE_FIXTURES as unknown[]).length).toBeGreaterThanOrEqual(20);
	});

	it("matches the shared fixture contract for every case", () => {
		for (const fixture of PRECEDENCE_FIXTURES as Array<{
			name: string;
			a: Record<string, unknown>;
			b: Record<string, unknown>;
			expected: "a_wins" | "b_wins" | "escalate";
		}>) {
			const actual = compareClaims(fixture.a, fixture.b);
			expect(actual, `fixture "${fixture.name}"`).toBe(fixture.expected);
		}
	});
});

describe("precedence lattice — direct assertions", () => {
	it("a march user decision beats a yesterday assistant fact, regardless of recency", () => {
		const marchDecision = { asserted_by: "user", assertion_kind: "decision", behavioral: false, as_of: "2026-03-01T00:00:00Z" };
		const yesterdayFact = { asserted_by: "assistant", assertion_kind: "fact", behavioral: false, as_of: "2026-07-08T00:00:00Z" };
		expect(compareClaims(marchDecision, yesterdayFact)).toBe("a_wins");
		expect(compareClaims(yesterdayFact, marchDecision)).toBe("b_wins");
	});

	it("a behavioral-vs-stated user conflict always escalates, in both orders", () => {
		const stated = { asserted_by: "user", assertion_kind: "preference", behavioral: false, as_of: "2026-01-01T00:00:00Z" };
		const behavioral = { asserted_by: "behavioral", assertion_kind: "fact", behavioral: false, as_of: "2026-07-01T00:00:00Z" };
		expect(compareClaims(stated, behavioral)).toBe("escalate");
		expect(compareClaims(behavioral, stated)).toBe("escalate");
	});

	it("never consults recency when authority differs", () => {
		const oldUser = { asserted_by: "user", assertion_kind: "fact", behavioral: false, as_of: "2020-01-01T00:00:00Z" };
		const newAssistant = { asserted_by: "assistant", assertion_kind: "decision", behavioral: false, as_of: "2026-07-10T00:00:00Z" };
		expect(compareClaims(oldUser, newAssistant)).toBe("a_wins");
	});
});

describe("deriveAssertedBy", () => {
	it("maps a cited user message to user", () => {
		expect(deriveAssertedBy(["m1"], { m1: "user", m2: "assistant" })).toBe("user");
	});

	it("any cited user message wins even if others are assistant", () => {
		expect(deriveAssertedBy(["m1", "m2"], { m1: "assistant", m2: "user" })).toBe("user");
	});

	it("all-assistant cited messages yield assistant", () => {
		expect(deriveAssertedBy(["m1", "m2"], { m1: "assistant", m2: "assistant" })).toBe("assistant");
	});

	it("an unknown message id falls back to inferred", () => {
		expect(deriveAssertedBy(["m1", "m_unknown"], { m1: "assistant" })).toBe("inferred");
	});

	it("no role map yields inferred", () => {
		expect(deriveAssertedBy(["m1"], null)).toBe("inferred");
	});

	it("empty message ids yields inferred", () => {
		expect(deriveAssertedBy([], { m1: "user" })).toBe("inferred");
	});

	it("an unrecognized role is never invented as assistant (regression: adversarial review 2026-07-10)", () => {
		expect(deriveAssertedBy(["m1"], { m1: "system" })).toBe("inferred");
		expect(deriveAssertedBy(["m1"], { m1: "tool" })).toBe("inferred");
	});

	it("a mix of assistant and an unrecognized role yields inferred", () => {
		expect(deriveAssertedBy(["m1", "m2"], { m1: "assistant", m2: "system" })).toBe("inferred");
	});

	it("user still wins even alongside an unrecognized role", () => {
		expect(deriveAssertedBy(["m1", "m2"], { m1: "system", m2: "user" })).toBe("user");
	});
});
