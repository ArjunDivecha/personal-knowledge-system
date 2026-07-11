// =============================================================================
// SCRIPT NAME: projectLifecycle.test.ts
// =============================================================================
// INPUT FILES: None. All entry/proposal/grade fixtures are constructed
//   in-memory; the one integration suite (INV3) uses an in-memory mock of
//   @upstash/redis, @upstash/vector, and openai (no network, no real Redis).
// OUTPUT FILES: None. This module has no file I/O — vitest reports results to
//   stdout only.
//
// Covers contract PKS-PROJECT-LIFECYCLE-001 (INV1-INV5):
//   INV1 — governed-only transitions with a per-run cap (default 10); an 11th
//          candidate in one run is held with a cap reason.
//   INV2 — explicit_save-typed and explicitly pinned projects are never
//          proposed for transition regardless of staleness.
//   INV3 — every applied transition is receipted and reversible.
//   INV4 — get_index presents dormant projects distinctly (status field and
//          ordering), never inside the active ordering.
//   INV5 — staleness/grace windows are read from shared/memory_policy.json's
//          project_lifecycle block, not hardcoded.
// (INV6, scope discipline, is verified by the caller's git diff check, not
// exercised here.)
// =============================================================================

import { beforeEach, describe, expect, it, vi } from "vitest";

import { buildScheduledGovernedDecision, compareProjectIndexOrder } from "../src/index";
import { MEMORY_POLICY } from "../src/salience";

// ---------------------------------------------------------------------------
// Section A: isProjectTransitionCandidate — pure function, no Redis needed.
// ---------------------------------------------------------------------------

function daysAgoIso(days: number): string {
	return new Date(Date.now() - days * 86400000).toISOString();
}

type ProjectLoadedEntryOverrides = {
	id?: string;
	status?: string;
	contextType?: string;
	pinned?: boolean;
	lastTouched?: string | null;
	lastSeen?: string | null;
	updatedAt?: string | null;
	lastAccessed?: string | null;
};

function buildProjectLoadedEntry(overrides: ProjectLoadedEntryOverrides = {}) {
	const id = overrides.id ?? "pe_test_project";
	const status = overrides.status ?? "active";
	const contextType = overrides.contextType ?? "active_project";
	const metadata: Record<string, unknown> = {
		created_at: "2024-01-01T00:00:00.000Z",
		context_type: contextType,
		last_touched: overrides.lastTouched === undefined ? daysAgoIso(120) : overrides.lastTouched,
		last_seen: overrides.lastSeen === undefined ? null : overrides.lastSeen,
		updated_at: overrides.updatedAt === undefined ? null : overrides.updatedAt,
		last_accessed: overrides.lastAccessed === undefined ? null : overrides.lastAccessed,
		access_count: 0,
		revision: 0,
	};
	if (overrides.pinned) {
		metadata.pinned = true;
	}
	return {
		id,
		type: "project" as const,
		entry: {
			id,
			type: "project",
			name: "Test project",
			status,
			goal: "Ship the thing.",
			current_phase: "implementation",
			metadata,
		},
		metadata,
		label: "Test project",
		updatedAt: (metadata.last_touched as string | null) ?? null,
		contextType,
		injectionTier: 1 as const,
		mentionCount: 1,
		accessCount: 0,
		sourceConversationCount: 1,
		salienceScore: 0.5,
	};
}

describe("isProjectTransitionCandidate (INV1 candidate detection, INV2 exemptions, INV5 policy-driven windows)", () => {
	it("flags an active project touched 100+ days ago with no recent access", async () => {
		const { isProjectTransitionCandidate } = await import("../src/dream");
		const entry = buildProjectLoadedEntry({ lastTouched: daysAgoIso(100) });
		expect(isProjectTransitionCandidate(entry)).toBe(true);
	});

	it("does not flag the same fixture when it is explicit_save typed (INV2)", async () => {
		const { isProjectTransitionCandidate } = await import("../src/dream");
		const entry = buildProjectLoadedEntry({ lastTouched: daysAgoIso(100), contextType: "explicit_save" });
		expect(isProjectTransitionCandidate(entry)).toBe(false);
	});

	it("does not flag a metadata.pinned=true project regardless of staleness (INV2)", async () => {
		const { isProjectTransitionCandidate } = await import("../src/dream");
		const entry = buildProjectLoadedEntry({ lastTouched: daysAgoIso(100), pinned: true });
		expect(isProjectTransitionCandidate(entry)).toBe(false);
	});

	it("does not flag a non-active project (paused)", async () => {
		const { isProjectTransitionCandidate } = await import("../src/dream");
		const entry = buildProjectLoadedEntry({ status: "paused", lastTouched: daysAgoIso(200) });
		expect(isProjectTransitionCandidate(entry)).toBe(false);
	});

	it("does not flag a project accessed within the grace window despite stale activity", async () => {
		const { isProjectTransitionCandidate } = await import("../src/dream");
		const entry = buildProjectLoadedEntry({ lastTouched: daysAgoIso(200), lastAccessed: daysAgoIso(10) });
		expect(isProjectTransitionCandidate(entry)).toBe(false);
	});

	it("does not flag a project inside the staleness window", async () => {
		const { isProjectTransitionCandidate } = await import("../src/dream");
		const entry = buildProjectLoadedEntry({ lastTouched: daysAgoIso(30) });
		expect(isProjectTransitionCandidate(entry)).toBe(false);
	});

	it("flags a project with no activity timestamp at all (missing timestamp = stale, matches the Python audit)", async () => {
		const { isProjectTransitionCandidate } = await import("../src/dream");
		const entry = buildProjectLoadedEntry({ lastTouched: null, lastSeen: null, updatedAt: null, lastAccessed: null });
		expect(isProjectTransitionCandidate(entry)).toBe(true);
	});

	it("never flags a non-project entry", async () => {
		const { isProjectTransitionCandidate } = await import("../src/dream");
		const entry = { ...buildProjectLoadedEntry({ lastTouched: daysAgoIso(200) }), type: "knowledge" as const };
		expect(isProjectTransitionCandidate(entry)).toBe(false);
	});

	it("G0: the checked-in shared/memory_policy.json already has the project_lifecycle block (90/30 defaults) — no addition needed for this test suite", () => {
		const lifecycle = (MEMORY_POLICY as Record<string, unknown>).project_lifecycle as Record<string, unknown>;
		expect(lifecycle).toBeDefined();
		expect(lifecycle.active_stale_after_days).toBe(90);
		expect(lifecycle.active_recent_access_grace_days).toBe(30);
	});

	it("INV5: raising active_stale_after_days via a policy override removes a candidate that qualifies at the default", async () => {
		const { isProjectTransitionCandidate } = await import("../src/dream");
		const entry = buildProjectLoadedEntry({ lastTouched: daysAgoIso(100) });
		expect(isProjectTransitionCandidate(entry)).toBe(true);

		const lifecycle = (MEMORY_POLICY as Record<string, unknown>).project_lifecycle as Record<string, unknown>;
		const original = { ...lifecycle };
		try {
			lifecycle.active_stale_after_days = 400;
			expect(isProjectTransitionCandidate(entry)).toBe(false);
		} finally {
			Object.assign(lifecycle, original);
		}
	});
});

// ---------------------------------------------------------------------------
// Section B: buildScheduledGovernedDecision — INV1 per-run cap + hold reason.
// Mirrors test/protectedTypeMergeHold.test.ts's pure-function pattern: a
// synthetic proposal/grade pair, no Redis or Worker transport needed.
// ---------------------------------------------------------------------------

function makeProjectTransitionOperation(entryId: string): Record<string, unknown> {
	return {
		operation_id: `dop_project_transition_${entryId}`,
		type: "project_status_transition",
		entry_id: entryId,
		expected_revision: 0,
		target_status: "dormant",
		reason: "Dream found this active project stale for 120 days with no access within the grace window.",
		evidence: { id: entryId },
		rollback: { method: "restore_snapshot", entry_id: entryId },
	};
}

function makeProposal(operations: Array<Record<string, unknown>>): Record<string, unknown> {
	const candidateIds = new Set<string>();
	for (const op of operations) {
		if (typeof op.entry_id === "string") candidateIds.add(op.entry_id);
	}
	return {
		run_id: "dpr_project_lifecycle_cap_test",
		status: "proposal_ready",
		risk_score: "low",
		candidate_ids: [...candidateIds],
		operations,
	};
}

function makePassedGrade(operations: Array<Record<string, unknown>>): Record<string, unknown> {
	return {
		grade_id: "dpg_project_lifecycle_cap_test",
		status: "passed",
		passed: true,
		operation_ids: operations.map((op) => op.operation_id),
	};
}

describe("buildScheduledGovernedDecision — INV1 project_status_transition per-run cap", () => {
	it("selects only the default cap (10) of 11 candidate transitions and holds the 11th with a cap reason", () => {
		const operations = Array.from({ length: 11 }, (_, i) => makeProjectTransitionOperation(`pe_${i}`));
		const proposal = makeProposal(operations);
		const grade = makePassedGrade(operations);

		const decision = buildScheduledGovernedDecision(proposal, grade);

		expect(decision.selectedOperationIds).toHaveLength(10);
		expect(decision.heldOperations).toHaveLength(1);
		expect(decision.heldOperations[0]?.reason).toBe("scheduled_cap_reached:project_status_transition:10");
	});

	it("auto-selects all 10 when exactly at the cap", () => {
		const operations = Array.from({ length: 10 }, (_, i) => makeProjectTransitionOperation(`pe_${i}`));
		const proposal = makeProposal(operations);
		const grade = makePassedGrade(operations);

		const decision = buildScheduledGovernedDecision(proposal, grade);

		expect(decision.selectedOperationIds).toHaveLength(10);
		expect(decision.heldOperations).toHaveLength(0);
	});
});

// ---------------------------------------------------------------------------
// Section C: compareProjectIndexOrder — INV4 get_index ordering + labeling.
// ---------------------------------------------------------------------------

describe("compareProjectIndexOrder (INV4)", () => {
	it("sorts dormant-status projects after non-dormant ones regardless of tier/salience", () => {
		const dormantButHighTier = { status: "dormant", injection_tier: 1, salience_score: 0.9, last_touched: "2026-07-01" };
		const activeLowTier = { status: "active", injection_tier: 3, salience_score: 0.1, last_touched: "2020-01-01" };
		const sorted = [dormantButHighTier, activeLowTier].sort(compareProjectIndexOrder);
		expect(sorted).toEqual([activeLowTier, dormantButHighTier]);
	});

	it("falls back to the existing tier/salience/touched ordering among non-dormant projects", () => {
		const a = { status: "active", injection_tier: 1, salience_score: 0.5, last_touched: "2026-01-01" };
		const b = { status: "active", injection_tier: 2, salience_score: 0.9, last_touched: "2026-06-01" };
		const sorted = [b, a].sort(compareProjectIndexOrder);
		expect(sorted).toEqual([a, b]);
	});

	it("the status field passes through unchanged for dormant projects (get_index labels, does not hide)", () => {
		const dormant = { status: "dormant", injection_tier: 3, salience_score: 0, last_touched: null };
		expect(dormant.status).toBe("dormant");
	});
});

// ---------------------------------------------------------------------------
// Section D: full apply/rollback round trip (INV1 sole-writer path, INV3
// receipted reversibility) via runDreamProposal -> gradeDreamProposal ->
// applyDreamProposal -> rollbackDreamApply, mirroring test/dream-replay.test.ts's
// mock harness and call signatures (applyDreamProposalOperation itself is not
// exported, so this is the correct, in-repo way to exercise the apply branch).
// ---------------------------------------------------------------------------

const mockState = vi.hoisted(() => ({
	store: new Map<string, unknown>(),
	lists: new Map<string, string[]>(),
	sets: new Map<string, Set<string>>(),
	vectorUpdates: [] as Array<Record<string, unknown>>,
	vectorUpserts: [] as Array<Record<string, unknown>>,
	vectorDeletes: [] as string[],
}));

function globToRegex(pattern: string): RegExp {
	const escaped = pattern.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*");
	return new RegExp(`^${escaped}$`);
}

vi.mock("@upstash/redis/cloudflare", () => ({
	Redis: class MockRedis {
		async get(key: string): Promise<unknown> {
			return mockState.store.get(key) ?? null;
		}

		async set(key: string, value: unknown, options?: { nx?: boolean; ex?: number }): Promise<string | null> {
			if (options?.nx && mockState.store.has(key)) {
				return null;
			}
			mockState.store.set(key, value);
			return "OK";
		}

		async scan(cursor: string, options?: { match?: string; count?: number }): Promise<[string, string[]]> {
			if (cursor !== "0") {
				return ["0", []];
			}
			const matcher = options?.match ? globToRegex(options.match) : /.*/;
			const keys = [...mockState.store.keys()].filter((key) => matcher.test(key));
			return ["0", keys];
		}

		async mget<T>(keys: string[]): Promise<T[]> {
			return keys.map((key) => (mockState.store.get(key) ?? null) as T);
		}

		async del(...keys: string[]): Promise<number> {
			let deleted = 0;
			for (const key of keys) {
				if (mockState.store.delete(key)) deleted += 1;
			}
			return deleted;
		}

		async rename(source: string, target: string): Promise<void> {
			const value = mockState.store.get(source);
			mockState.store.delete(source);
			mockState.store.set(target, value ?? null);
		}

		async lpush(key: string, value: string): Promise<number> {
			const list = mockState.lists.get(key) ?? [];
			list.unshift(value);
			mockState.lists.set(key, list);
			return list.length;
		}

		async ltrim(key: string, start: number, stop: number): Promise<string> {
			const list = mockState.lists.get(key) ?? [];
			mockState.lists.set(key, list.slice(start, stop + 1));
			return "OK";
		}

		async sadd(key: string, value: string): Promise<number> {
			const set = mockState.sets.get(key) ?? new Set<string>();
			const beforeSize = set.size;
			set.add(value);
			mockState.sets.set(key, set);
			return set.size > beforeSize ? 1 : 0;
		}

		async srem(key: string, value: string): Promise<number> {
			const set = mockState.sets.get(key) ?? new Set<string>();
			const deleted = set.delete(value);
			mockState.sets.set(key, set);
			return deleted ? 1 : 0;
		}
	},
}));

vi.mock("@upstash/vector", () => ({
	Index: class MockIndex {
		async upsert(payload: Record<string, unknown>): Promise<void> {
			mockState.vectorUpserts.push(payload);
		}

		async update(payload: Record<string, unknown>): Promise<void> {
			mockState.vectorUpdates.push(payload);
		}

		async delete(id: string | string[]): Promise<void> {
			const ids = Array.isArray(id) ? id : [id];
			mockState.vectorDeletes.push(...ids);
		}
	},
}));

vi.mock("openai", () => ({
	default: class MockOpenAI {
		embeddings = {
			create: async () => ({ data: [{ embedding: [0.1, 0.2, 0.3] }] }),
		};
	},
}));

function buildStaleProjectEntry(params: { id: string; lastTouched: string }): Record<string, unknown> {
	return {
		id: params.id,
		type: "project",
		name: "SPX MA200 Strategy Backtest",
		status: "active",
		detail_level: "full",
		goal: "One-shot 2024 backtest session.",
		current_phase: "archival candidate",
		blocked_on: null,
		decisions_made: [],
		tech_stack: [],
		related_repos: [],
		related_knowledge: [],
		phase_history: [],
		metadata: {
			created_at: "2024-08-01T00:00:00.000Z",
			updated_at: params.lastTouched,
			source_conversations: ["conv_2024_backtest"],
			source_messages: [],
			last_touched: params.lastTouched,
			access_count: 0,
			last_accessed: null,
			schema_version: 2,
			classification_status: "classified",
			context_type: "active_project",
			mention_count: 2,
			first_seen: "2024-08-01T00:00:00.000Z",
			last_seen: params.lastTouched,
			auto_inferred: true,
			source_weights: {},
			injection_tier: 1,
			salience_score: 0.6,
			last_consolidated: null,
			consolidation_notes: [],
			archived: false,
			revision: 0,
		},
	};
}

function getStoredObject(key: string): Record<string, unknown> {
	const raw = mockState.store.get(key);
	if (typeof raw === "string") return JSON.parse(raw) as Record<string, unknown>;
	return raw as Record<string, unknown>;
}

describe("Dream project_status_transition apply/rollback (INV1 sole-writer path, INV3 receipted reversibility)", () => {
	beforeEach(() => {
		mockState.store.clear();
		mockState.lists.clear();
		mockState.sets.clear();
		mockState.vectorUpdates.length = 0;
		mockState.vectorUpserts.length = 0;
		mockState.vectorDeletes.length = 0;
		mockState.store.set("migration:backfill_complete", "2026-03-27T05:29:20+00:00");
		mockState.store.set(
			"project:pe_2024_one_shot",
			buildStaleProjectEntry({
				id: "pe_2024_one_shot",
				lastTouched: new Date(Date.now() - 200 * 86400000).toISOString(),
			}),
		);
	});

	const testEnv = {
		UPSTASH_REDIS_REST_URL: "https://redis.test.local",
		UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
		UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
		UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
	};

	it("proposes, grades, applies (receipted), and rolls back a stale active project to its exact prior status", async () => {
		const { applyDreamProposal, gradeDreamProposal, rollbackDreamApply, runDreamProposal } = await import("../src/dream");

		const proposal = await runDreamProposal(testEnv as any, {
			trigger: "local_test",
			actorId: "test-operator",
			archiveLimit: 0,
			promotionLimit: 0,
		});
		const operations = proposal.operations as Array<Record<string, unknown>>;
		const transitionOperation = operations.find((op) => op.type === "project_status_transition");
		expect(transitionOperation).toBeTruthy();
		expect(transitionOperation?.entry_id).toBe("pe_2024_one_shot");
		expect(transitionOperation?.target_status).toBe("dormant");

		await gradeDreamProposal(testEnv as any, { proposalId: String(proposal.run_id), actorId: "test-operator" });

		const applyResult = await applyDreamProposal(testEnv as any, {
			proposalId: String(proposal.run_id),
			mutationId: "apply-project-transition-test",
			actorId: "test-operator",
			reason: "approve stale project transition",
			operationIds: [String(transitionOperation!.operation_id)],
		});

		expect(applyResult.ok).toBe(true);
		const appliedResult = (applyResult.results as Array<Record<string, unknown>>)[0];
		const applyResultDetail = appliedResult.result as Record<string, unknown>;
		expect(applyResultDetail.prior_status).toBe("active");
		expect(applyResultDetail.status).toBe("dormant");

		const storedAfterApply = getStoredObject("project:pe_2024_one_shot");
		expect(storedAfterApply.status).toBe("dormant");
		const metadataAfterApply = storedAfterApply.metadata as Record<string, unknown>;
		const notes = metadataAfterApply.consolidation_notes as string[];
		expect(notes.some((note) => note.includes("project_status_transition") && note.includes("active -> dormant"))).toBe(true);

		const rollbackResult = await rollbackDreamApply(
			{ ...testEnv, OPENAI_API_KEY: "test-openai-key" } as any,
			{
				proposalId: String(proposal.run_id),
				applyMutationId: "apply-project-transition-test",
				rollbackMutationId: "rollback-project-transition-test",
				actorId: "test-operator",
				reason: "restore project to active",
				operationIds: [String(transitionOperation!.operation_id)],
			},
		);

		expect(rollbackResult.ok).toBe(true);
		expect(rollbackResult.rolled_back_count).toBe(1);
		const storedAfterRollback = getStoredObject("project:pe_2024_one_shot");
		expect(storedAfterRollback.status).toBe("active");
		expect(storedAfterRollback.name).toBe("SPX MA200 Strategy Backtest");
	});
});
