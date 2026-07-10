// =============================================================================
// SHARED PRECEDENCE LATTICE — authority-then-durability claim comparator (TS)
// =============================================================================
// TypeScript twin of distillation/utils/precedence.py. Decides which of two
// conflicting claims wins using an authority-then-durability lattice, with
// recency only as the final tiebreak (never naive recency). A user assertion
// always outranks an assistant assertion regardless of date; a user-stated vs
// behavioral (repeated-observation) conflict is never auto-resolved and returns
// "escalate".
//
// This file MUST stay semantically identical to the Python module; the shared
// fixture table shared/precedence_fixtures.json is the lockstep proof (the
// vitest suite and the Python unittest both replay it).
//
// INPUT FILES:
// - shared/precedence_fixtures.json (imported below; labeled claim-pair cases)
// OUTPUT FILES:
// - None (pure logic; returns values, writes nothing)
// =============================================================================

import precedenceFixtures from "../../../shared/precedence_fixtures.json";

type JsonRecord = Record<string, unknown>;

export const PRECEDENCE_FIXTURES = precedenceFixtures;

export type Verdict = "a_wins" | "b_wins" | "escalate";

export interface Claim {
	asserted_by?: string | null;
	assertion_kind?: string | null;
	behavioral?: boolean | null;
	as_of?: string | null;
}

// Authority ranks (higher wins). user = arjun_explicit; behavioral = repeated
// observed behavior; assistant = assistant-asserted; inferred = extractor
// generalization / unknown provenance.
export const AUTHORITY_RANK: Record<string, number> = {
	user: 4,
	behavioral: 3,
	assistant: 2,
	inferred: 1,
};

// Durability ranks (higher wins) within equal authority.
export const DURABILITY_RANK: Record<string, number> = {
	decision: 4,
	correction: 4,
	preference: 3,
	fact: 2,
	hypothesis: 1,
};

function toDate(value: unknown): Date | null {
	if (typeof value !== "string" || value.length === 0) {
		return null;
	}
	const parsed = new Date(value);
	return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function deriveAssertedBy(
	messageIds: string[],
	rolesById: Record<string, string> | null | undefined,
): string {
	// roles_by_id None or message_ids empty -> "inferred"; any cited user
	// message -> "user"; any missing id OR a role that is neither "user" nor
	// "assistant" (e.g. "system", "tool") -> "inferred" (never invent an
	// authority level for an unrecognized role); else all-assistant ->
	// "assistant". Mirrors derive_asserted_by in precedence.py.
	if (!rolesById || messageIds.length === 0) {
		return "inferred";
	}
	let sawNonAssistant = false;
	for (const messageId of messageIds) {
		const role = rolesById[messageId];
		if (role === "user") {
			return "user";
		}
		if (role !== "assistant") {
			sawNonAssistant = true;
		}
	}
	return sawNonAssistant ? "inferred" : "assistant";
}

export function authorityOf(
	assertedBy: string | null | undefined,
	behavioral: boolean,
): number {
	// None -> inferred. behavioral=true upgrades ONLY inferred to rank 3; it
	// never downgrades a user or assistant claim.
	const key = assertedBy ?? "inferred";
	const rank = AUTHORITY_RANK[key] ?? AUTHORITY_RANK.inferred;
	if (behavioral && key === "inferred") {
		return AUTHORITY_RANK.behavioral;
	}
	return rank;
}

function durabilityOf(assertionKind: string | null | undefined): number {
	const key = assertionKind ?? "hypothesis";
	return DURABILITY_RANK[key] ?? DURABILITY_RANK.hypothesis;
}

export function compareClaims(a: Claim, b: Claim): Verdict {
	const aAuth = authorityOf(a.asserted_by, Boolean(a.behavioral));
	const bAuth = authorityOf(b.asserted_by, Boolean(b.behavioral));

	// Rule 1: user-stated (4) vs behavioral (3) is never auto-resolved.
	const authSet = new Set([aAuth, bAuth]);
	if (authSet.size === 2 && authSet.has(4) && authSet.has(3)) {
		return "escalate";
	}

	// Rule 2: unequal authority -> higher wins, recency irrelevant.
	if (aAuth !== bAuth) {
		return aAuth > bAuth ? "a_wins" : "b_wins";
	}

	// Rule 3: equal authority -> durability decides.
	const aDur = durabilityOf(a.assertion_kind);
	const bDur = durabilityOf(b.assertion_kind);
	if (aDur !== bDur) {
		return aDur > bDur ? "a_wins" : "b_wins";
	}

	// Rule 4: recency as final tiebreak.
	const aDate = toDate(a.as_of);
	const bDate = toDate(b.as_of);
	if (aDate !== null && bDate !== null) {
		if (aDate.getTime() > bDate.getTime()) {
			return "a_wins";
		}
		if (bDate.getTime() > aDate.getTime()) {
			return "b_wins";
		}
		return "escalate"; // identical timestamps -> fully tied
	}
	if (aDate !== null) {
		return "a_wins"; // present as_of beats missing
	}
	if (bDate !== null) {
		return "b_wins";
	}
	return "escalate"; // Rule 5: both missing / fully tied
}

export function authorityRollup(entry: JsonRecord): [string, string] {
	// Entry-level provenance rollup: the (asserted_by, assertion_kind) of the
	// single strongest evidence reachable in the entry, max by (authorityOf with
	// behavioral=false, durability). Empty/no evidence -> ["inferred",
	// "hypothesis"]; missing fields on the winner normalized the same way.
	let bestKey: [number, number] | null = null;
	let bestPair: [string, string] = ["inferred", "hypothesis"];

	const blocks = ["positions", "key_insights", "knows_how_to", "open_questions"];
	for (const block of blocks) {
		const items = entry[block];
		if (!Array.isArray(items)) {
			continue;
		}
		for (const item of items) {
			if (!item || typeof item !== "object") {
				continue;
			}
			const evidence = (item as JsonRecord).evidence;
			if (!evidence || typeof evidence !== "object" || Array.isArray(evidence)) {
				continue;
			}
			const assertedBy = (evidence as JsonRecord).asserted_by;
			const assertionKind = (evidence as JsonRecord).assertion_kind;
			const assertedByStr = typeof assertedBy === "string" ? assertedBy : null;
			const assertionKindStr = typeof assertionKind === "string" ? assertionKind : null;
			const rank: [number, number] = [
				authorityOf(assertedByStr, false),
				durabilityOf(assertionKindStr),
			];
			if (
				bestKey === null ||
				rank[0] > bestKey[0] ||
				(rank[0] === bestKey[0] && rank[1] > bestKey[1])
			) {
				bestKey = rank;
				bestPair = [assertedByStr ?? "inferred", assertionKindStr ?? "hypothesis"];
			}
		}
	}

	return bestPair;
}
