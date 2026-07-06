import { beforeEach, describe, expect, it, vi } from "vitest";

const mockState = vi.hoisted(() => ({
	store: new Map<string, unknown>(),
	lists: new Map<string, string[]>(),
	sets: new Map<string, Set<string>>(),
	vectorUpdates: [] as Array<Record<string, unknown>>,
	vectorUpserts: [] as Array<Record<string, unknown>>,
	vectorDeletes: [] as string[],
	maxRequestBytes: null as number | null,
	maxMgetKeys: null as number | null,
	mgetCallSizes: [] as number[],
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

		async set(
			key: string,
			value: unknown,
			options?: { nx?: boolean; ex?: number },
		): Promise<string | null> {
			if (typeof mockState.maxRequestBytes === "number") {
				const requestBytes = Buffer.byteLength(JSON.stringify(["set", key, value]), "utf8");
				if (requestBytes > mockState.maxRequestBytes) {
					throw new Error(
						`ERR max request size exceeded. Limit: ${mockState.maxRequestBytes} bytes, Actual: ${requestBytes} bytes.`,
					);
				}
			}
			if (options?.nx && mockState.store.has(key)) {
				return null;
			}
			mockState.store.set(key, value);
			return "OK";
		}

		async scan(
			cursor: string,
			options?: { match?: string; count?: number },
		): Promise<[string, string[]]> {
			if (cursor !== "0") {
				return ["0", []];
			}
			const matcher = options?.match ? globToRegex(options.match) : /.*/;
			const keys = [...mockState.store.keys()].filter((key) => matcher.test(key));
			return ["0", keys];
		}

		async mget<T>(keys: string[]): Promise<T[]> {
			mockState.mgetCallSizes.push(keys.length);
			if (typeof mockState.maxMgetKeys === "number" && keys.length > mockState.maxMgetKeys) {
				throw new Error(`ERR mget batch too large. Limit: ${mockState.maxMgetKeys}, Actual: ${keys.length}`);
			}
			return keys.map((key) => (mockState.store.get(key) ?? null) as T);
		}

		async del(...keys: string[]): Promise<number> {
			let deleted = 0;
			for (const key of keys) {
				if (mockState.store.delete(key)) {
					deleted += 1;
				}
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

		async delete(_id: string | string[]): Promise<void> {
			const ids = Array.isArray(_id) ? _id : [_id];
			mockState.vectorDeletes.push(...ids);
		}
	},
}));

vi.mock("openai", () => ({
	default: class MockOpenAI {
		embeddings = {
			create: async () => ({
				data: [{ embedding: [0.1, 0.2, 0.3] }],
			}),
		};
	},
}));

import { applyDreamProposal, compactDreamRunRecordForStorage, gradeDreamProposal, rollbackDreamApply, runDreamCycle, runDreamProposal } from "../src/dream";

function buildKnowledgeEntry(params: {
	id: string;
	domain: string;
	currentView: string;
	contextType?: string;
	injectionTier?: number;
	mentionCount?: number;
	accessCount?: number;
	sourceConversations?: string[];
	updatedAt?: string;
	confidence?: "high" | "medium" | "low";
	state?: "active" | "contested" | "stale" | "deprecated";
	positions?: Array<Record<string, unknown>>;
	salienceScore?: number;
}): Record<string, unknown> {
	const updatedAt = params.updatedAt ?? "2026-03-28T07:00:00.000Z";
	return {
		id: params.id,
		type: "knowledge",
		domain: params.domain,
		state: params.state ?? "active",
		detail_level: "full",
		current_view: params.currentView,
		confidence: params.confidence ?? "high",
		positions: params.positions ?? [],
		key_insights: [],
		knows_how_to: [],
		open_questions: [],
		related_repos: [],
		related_knowledge: [],
		evolution: [],
		metadata: {
			created_at: "2026-03-01T00:00:00.000Z",
			updated_at: updatedAt,
			source_conversations: params.sourceConversations ?? [],
			source_messages: [],
			access_count: params.accessCount ?? 0,
			last_accessed: null,
			schema_version: 2,
			classification_status: "classified",
			context_type: params.contextType ?? "recurring_pattern",
			mention_count: params.mentionCount ?? Math.max(1, (params.sourceConversations ?? []).length),
			first_seen: "2026-03-01T00:00:00.000Z",
			last_seen: updatedAt,
			auto_inferred: true,
			source_weights: {},
			injection_tier: params.injectionTier ?? 2,
			salience_score: params.salienceScore ?? 0.4,
			last_consolidated: null,
			consolidation_notes: [],
			archived: false,
		},
	};
}

function getStoredObject(key: string): Record<string, unknown> {
	const raw = mockState.store.get(key);
	if (typeof raw === "string") {
		return JSON.parse(raw) as Record<string, unknown>;
	}
	return raw as Record<string, unknown>;
}

describe("Dream replay logic", () => {
	beforeEach(() => {
		mockState.store.clear();
		mockState.lists.clear();
		mockState.sets.clear();
		mockState.vectorUpdates.length = 0;
		mockState.vectorUpserts.length = 0;
		mockState.vectorDeletes.length = 0;
		mockState.maxRequestBytes = null;
		mockState.maxMgetKeys = null;
		mockState.mgetCallSizes.length = 0;
		mockState.store.set("migration:backfill_complete", "2026-03-27T05:29:20+00:00");

		mockState.store.set(
			"knowledge:ke_dup_primary",
			buildKnowledgeEntry({
				id: "ke_dup_primary",
				domain: "Country equity rotation signals",
				currentView: "Use cross border GDELT sentiment and out of sample rank IC for country ETF rotation.",
				mentionCount: 2,
				accessCount: 0,
				sourceConversations: ["conv_a", "conv_b"],
				updatedAt: "2026-03-28T06:55:00.000Z",
			}),
		);
		mockState.store.set(
			"knowledge:ke_dup_secondary",
			buildKnowledgeEntry({
				id: "ke_dup_secondary",
				domain: "Country equity rotation signals",
				currentView: "Use cross border GDELT sentiment with out of sample rank IC to rank country ETFs.",
				mentionCount: 1,
				accessCount: 0,
				sourceConversations: ["conv_c"],
				updatedAt: "2026-03-28T06:57:00.000Z",
			}),
		);
		mockState.store.set(
			"knowledge:ke_conflict_a",
			buildKnowledgeEntry({
				id: "ke_conflict_a",
				domain: "Value factor outlook",
				currentView: "Value should outperform while rates are falling.",
				sourceConversations: ["conv_d"],
				updatedAt: "2026-03-28T06:58:00.000Z",
			}),
		);
		mockState.store.set(
			"knowledge:ke_conflict_b",
			buildKnowledgeEntry({
				id: "ke_conflict_b",
				domain: "Value factor outlook",
				currentView: "Value should underperform while rates are rising.",
				sourceConversations: ["conv_e"],
				updatedAt: "2026-03-28T06:59:00.000Z",
			}),
		);
	});

	it("merges deterministic duplicates and marks contradictions contested", async () => {
		const result = await runDreamCycle(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
				OPENAI_API_KEY: "test-openai-key",
			} as Env,
			{
				dryRun: false,
				trigger: "local_test",
				note: "dream replay unit test",
				setAsLatest: false,
			},
		);

		expect((result.counts as Record<string, unknown>).merged_duplicates).toBe(1);
		expect((result.counts as Record<string, unknown>).entries_marked_contested).toBe(2);
		expect((result.phases as Record<string, unknown>).replay).toMatchObject({
			duplicate_merge_count: 1,
			contradiction_count: 1,
		});

		const canonical = getStoredObject("knowledge:ke_dup_primary");
		const canonicalMetadata = canonical.metadata as Record<string, unknown>;
		expect(canonicalMetadata.archived).toBe(false);
		expect(canonicalMetadata.mention_count).toBe(3);
		expect(canonicalMetadata.access_count).toBe(0);
		expect(canonicalMetadata.source_conversations).toEqual(
			expect.arrayContaining(["conv_a", "conv_b", "conv_c"]),
		);
		expect((canonicalMetadata.consolidation_notes as string[]).join("\n")).toContain("merge_duplicate_entries");

		const archivedDuplicate = getStoredObject("knowledge:ke_dup_secondary");
		const archivedDuplicateMetadata = archivedDuplicate.metadata as Record<string, unknown>;
		expect(archivedDuplicateMetadata.archived).toBe(true);
		expect(String(archivedDuplicateMetadata.archived_reason)).toContain("merged duplicate into ke_dup_primary");
		expect(mockState.store.get("archived:knowledge:ke_dup_secondary:latest")).toBeTruthy();

		const contradictionA = getStoredObject("knowledge:ke_conflict_a");
		const contradictionB = getStoredObject("knowledge:ke_conflict_b");
		expect(contradictionA.state).toBe("contested");
		expect(contradictionB.state).toBe("contested");
		expect(contradictionA.related_knowledge).toEqual(
			expect.arrayContaining([
				expect.objectContaining({
					knowledge_id: "ke_conflict_b",
					relationship: "contradicts",
				}),
			]),
		);
		expect(contradictionB.related_knowledge).toEqual(
			expect.arrayContaining([
				expect.objectContaining({
					knowledge_id: "ke_conflict_a",
					relationship: "contradicts",
				}),
			]),
		);
		expect(((contradictionA.metadata as Record<string, unknown>).consolidation_notes as string[]).join("\n"))
			.toContain("mark_contested");

			expect(mockState.vectorUpdates).toEqual(
				expect.arrayContaining([
					expect.objectContaining({
						id: "ke_conflict_a",
						metadata: expect.objectContaining({ state: "contested" }),
					}),
				]),
			);
			expect(mockState.vectorDeletes).toContain("ke_dup_secondary");
		});

	it("generates a no-write proposal without mutating entries, vectors, or latest Dream state", async () => {
		const beforePrimary = JSON.stringify(getStoredObject("knowledge:ke_dup_primary"));
		const beforeSecondary = JSON.stringify(getStoredObject("knowledge:ke_dup_secondary"));

		const proposal = await runDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
				OPENAI_API_KEY: "test-openai-key",
			} as Env,
			{
				trigger: "local_test",
				actorId: "test-operator",
				note: "proposal unit test",
				archiveLimit: 0,
				promotionLimit: 0,
			},
		);

		expect(proposal.status).toBe("proposal_ready");
		expect(proposal.dry_run).toBe(true);
		expect(proposal.run_id).toMatch(/^dpr_/);
		expect((proposal.operations as Array<Record<string, unknown>>)).toEqual(
			expect.arrayContaining([
				expect.objectContaining({ type: "duplicate_merge", keep_id: "ke_dup_primary" }),
				expect.objectContaining({ type: "mark_contested" }),
			]),
		);
		expect(JSON.stringify(getStoredObject("knowledge:ke_dup_primary"))).toBe(beforePrimary);
		expect(JSON.stringify(getStoredObject("knowledge:ke_dup_secondary"))).toBe(beforeSecondary);
		expect(mockState.vectorUpdates).toEqual([]);
		expect(mockState.vectorDeletes).toEqual([]);
		expect(mockState.store.get("dream:last_run")).toBeUndefined();
		expect(mockState.store.get("archived:knowledge:ke_dup_secondary:latest")).toBeUndefined();
		expect(mockState.store.get(`dream:run:${String(proposal.run_id)}:proposal`)).toBeTruthy();
		expect(mockState.store.get("dream:proposal:last")).toBeTruthy();
	});

	it("turns correction contest hints into governed mark-contested operations", async () => {
		mockState.store.set(
			"dream:contest_hint:ce_fixture:ke_dup_primary",
			JSON.stringify({
				schema_version: 1,
				proposal_kind: "contest",
				source: "correction_event",
				status: "pending",
				event_id: "ce_fixture",
				conversation_id: "conv-correction",
				message_id: "msg-correction",
				target_entry_id: "ke_dup_primary",
				target_entry_type: "knowledge",
				corrected_belief: "Country equity rotation should only use GDELT sentiment.",
				new_belief: "Country equity rotation should use GDELT sentiment and rank IC together.",
				correction_confidence: 0.92,
				judge_confidence: 0.87,
				reason: "user correction supersedes the narrower prior memory",
				similarity: 0.9,
			}),
		);

		const proposal = await runDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
				OPENAI_API_KEY: "test-openai-key",
			} as Env,
			{
				trigger: "local_test",
				actorId: "test-operator",
				note: "correction contest hint test",
				archiveLimit: 0,
				promotionLimit: 0,
			},
		);
		const contestOperation = (proposal.operations as Array<Record<string, unknown>>)
			.find((operation) => operation.proposal_kind === "contest");

		expect(contestOperation).toMatchObject({
			type: "mark_contested",
			entry_ids: ["ke_dup_primary"],
			proposal_kind: "contest",
		});
		const contestOperationId = String(contestOperation?.operation_id);
		expect((proposal.counts as Record<string, unknown>).correction_contest_candidates).toBe(1);

		const grade = await gradeDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
			} as Env,
			{ proposalId: String(proposal.run_id), actorId: "test-operator" },
		);
		expect(grade.passed).toBe(true);

		const result = await applyDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
				OPENAI_API_KEY: "test-openai-key",
			} as Env,
			{
				proposalId: String(proposal.run_id),
				mutationId: "apply-correction-contest",
				actorId: "test-operator",
				reason: "approve correction contest",
				operationIds: [contestOperationId],
			},
		);

		expect(result.ok).toBe(true);
		expect(getStoredObject("knowledge:ke_dup_primary").state).toBe("contested");
		expect(getStoredObject("dream:contest_hint:ce_fixture:ke_dup_primary")).toMatchObject({
			status: "applied",
			operation_id: contestOperationId,
		});
		expect(String(getStoredObject("dream:contest_hint:ce_fixture:ke_dup_primary").applied_run_id))
			.toContain(String(proposal.run_id));
	});

	it("does not mark broad same-label memories contested just because narratives differ", async () => {
		mockState.store.clear();
		mockState.vectorUpdates.length = 0;
		mockState.vectorDeletes.length = 0;
		mockState.store.set("migration:backfill_complete", "2026-03-27T05:29:20+00:00");
		mockState.store.set(
			"knowledge:ke_broad_a",
			buildKnowledgeEntry({
				id: "ke_broad_a",
				domain: "documentation practices",
				currentView: "Keep release notes short and focused on user-visible changes.",
				sourceConversations: ["conv_doc_a"],
				updatedAt: "2026-03-28T06:55:00.000Z",
			}),
		);
		mockState.store.set(
			"knowledge:ke_broad_b",
			buildKnowledgeEntry({
				id: "ke_broad_b",
				domain: "documentation practices",
				currentView: "Architecture docs should record durable decisions and open questions.",
				sourceConversations: ["conv_doc_b"],
				updatedAt: "2026-03-28T06:56:00.000Z",
			}),
		);

		const proposal = await runDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
			} as Env,
			{
				trigger: "local_test",
				actorId: "test-operator",
				archiveLimit: 0,
				promotionLimit: 0,
			},
		);

		expect(proposal.counts).toEqual(
			expect.objectContaining({ contradictions_detected: 0 }),
		);
		expect(proposal.operations as Array<Record<string, unknown>>).not.toEqual(
			expect.arrayContaining([expect.objectContaining({ type: "mark_contested" })]),
		);
	});

	it("grades proposals with deterministic hard gates and stores the grade artifact", async () => {
		const proposal = await runDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
			} as Env,
			{
				trigger: "local_test",
				actorId: "test-operator",
				archiveLimit: 0,
				promotionLimit: 0,
			},
		);

		const grade = await gradeDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
			} as Env,
			{
				proposalId: String(proposal.run_id),
				actorId: "test-operator",
			},
		);

		expect(grade.status).toBe("passed");
		expect(grade.passed).toBe(true);
		expect(grade.hard_fail_count).toBe(0);
		expect(mockState.store.get(`dream:run:${String(proposal.run_id)}:grade`)).toBeTruthy();
		expect(mockState.store.get(`dream:run:${String(proposal.run_id)}:grade:${String(grade.grade_id)}`)).toBeTruthy();
	});

	it("fails deterministic grade when a proposal references entries outside the snapshot", async () => {
		const proposal = await runDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
			} as Env,
			{
				trigger: "local_test",
				actorId: "test-operator",
				archiveLimit: 0,
				promotionLimit: 0,
			},
		);
		const storedProposal = getStoredObject(`dream:run:${String(proposal.run_id)}:proposal`);
		(storedProposal.operations as Array<Record<string, unknown>>).push({
			operation_id: "dop_bad_external",
			type: "archive_entry",
			entry_id: "ke_outside_snapshot",
			expected_revision: 0,
			reason: "bad fixture",
			evidence: { source: "test" },
			rollback: { method: "restore_archived", entry_id: "ke_outside_snapshot" },
		});
		mockState.store.set(`dream:run:${String(proposal.run_id)}:proposal`, storedProposal);
		mockState.store.set(`dream:run:${String(proposal.run_id)}`, storedProposal);

		const grade = await gradeDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
			} as Env,
			{
				proposalId: String(proposal.run_id),
				actorId: "test-operator",
			},
		);

		expect(grade.status).toBe("failed");
		expect(grade.passed).toBe(false);
		expect(grade.issues as Array<Record<string, unknown>>).toEqual(
			expect.arrayContaining([
				expect.objectContaining({ code: "entry_outside_snapshot", operation_id: "dop_bad_external" }),
			]),
		);
	});

	it("applies selected proposal operations after revision preflight", async () => {
		const primaryBefore = getStoredObject("knowledge:ke_dup_primary");
		(primaryBefore.metadata as Record<string, unknown>).injection_tier = null;
		mockState.store.set("knowledge:ke_dup_primary", primaryBefore);

		const proposal = await runDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
				OPENAI_API_KEY: "test-openai-key",
			} as Env,
			{
				trigger: "local_test",
				actorId: "test-operator",
				archiveLimit: 0,
				promotionLimit: 0,
			},
		);
		const duplicateOperation = (proposal.operations as Array<Record<string, unknown>>)
			.find((operation) => operation.type === "duplicate_merge");
		expect(duplicateOperation).toBeTruthy();
		await gradeDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
			} as Env,
			{ proposalId: String(proposal.run_id), actorId: "test-operator" },
		);

		const result = await applyDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
				OPENAI_API_KEY: "test-openai-key",
			} as Env,
			{
				proposalId: String(proposal.run_id),
				mutationId: "apply-proposal-test",
				actorId: "test-operator",
				reason: "approve duplicate merge",
				operationIds: [String(duplicateOperation!.operation_id)],
			},
		);

		expect(result.ok).toBe(true);
		expect(result.applied_count).toBe(1);
		const canonical = getStoredObject("knowledge:ke_dup_primary");
		const canonicalMetadata = canonical.metadata as Record<string, unknown>;
		expect(canonicalMetadata.mention_count).toBe(3);
		expect(canonicalMetadata.injection_tier).toBe(2);
		expect(mockState.vectorUpdates).toEqual(
			expect.arrayContaining([
				expect.objectContaining({
					id: "ke_dup_primary",
					metadata: expect.objectContaining({ injection_tier: 2 }),
				}),
			]),
		);
		const archivedDuplicate = getStoredObject("knowledge:ke_dup_secondary");
		expect((archivedDuplicate.metadata as Record<string, unknown>).archived).toBe(true);
		expect(mockState.vectorDeletes).toContain("ke_dup_secondary");
		expect(mockState.store.get(`dream:run:${String(proposal.run_id)}:apply:apply-proposal-test`)).toBeTruthy();
		expect(mockState.store.get("mutation_result:apply-proposal-test")).toBeTruthy();
	});

	it("refuses mutating proposal apply until deterministic grade passes", async () => {
		const proposal = await runDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
			} as Env,
			{
				trigger: "local_test",
				actorId: "test-operator",
				archiveLimit: 0,
				promotionLimit: 0,
			},
		);
		const duplicateOperation = (proposal.operations as Array<Record<string, unknown>>)
			.find((operation) => operation.type === "duplicate_merge");

		const result = await applyDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
			} as Env,
			{
				proposalId: String(proposal.run_id),
				mutationId: "apply-ungraded-proposal-test",
				actorId: "test-operator",
				reason: "should fail without grade",
				operationIds: [String(duplicateOperation!.operation_id)],
			},
		);

		expect(result.ok).toBe(false);
		expect(result.error).toBe("grade_required");
		expect(getStoredObject("knowledge:ke_dup_secondary").metadata).toEqual(
			expect.objectContaining({ archived: false }),
		);
		expect(mockState.vectorDeletes).toEqual([]);
	});

	it("rejects stale proposal application before mutating anything", async () => {
		const proposal = await runDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
			} as Env,
			{
				trigger: "local_test",
				actorId: "test-operator",
				archiveLimit: 0,
				promotionLimit: 0,
			},
		);
		const duplicateOperation = (proposal.operations as Array<Record<string, unknown>>)
			.find((operation) => operation.type === "duplicate_merge");
		await gradeDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
			} as Env,
			{ proposalId: String(proposal.run_id), actorId: "test-operator" },
		);
		const primary = getStoredObject("knowledge:ke_dup_primary");
		(primary.metadata as Record<string, unknown>).revision = 1;
		mockState.store.set("knowledge:ke_dup_primary", primary);

		const result = await applyDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
			} as Env,
			{
				proposalId: String(proposal.run_id),
				mutationId: "apply-stale-proposal-test",
				actorId: "test-operator",
				reason: "approve stale duplicate merge",
				operationIds: [String(duplicateOperation!.operation_id)],
			},
		);

		expect(result.ok).toBe(false);
		expect(result.error).toBe("conflict");
		expect(getStoredObject("knowledge:ke_dup_secondary").metadata).toEqual(
			expect.objectContaining({ archived: false }),
		);
		expect(mockState.vectorDeletes).toEqual([]);
	});

	it("rolls back supported applied proposal operations with revision preflight", async () => {
		const proposal = await runDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
			} as Env,
			{
				trigger: "local_test",
				actorId: "test-operator",
				archiveLimit: 0,
				promotionLimit: 0,
			},
		);
		const contestedOperation = (proposal.operations as Array<Record<string, unknown>>)
			.find((operation) => operation.type === "mark_contested");
		expect(contestedOperation).toBeTruthy();
		await gradeDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
			} as Env,
			{ proposalId: String(proposal.run_id), actorId: "test-operator" },
		);

		const applyResult = await applyDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
			} as Env,
			{
				proposalId: String(proposal.run_id),
				mutationId: "apply-contested-test",
				actorId: "test-operator",
				reason: "approve contested marker",
				operationIds: [String(contestedOperation!.operation_id)],
			},
		);
		expect(applyResult.ok).toBe(true);
		expect(getStoredObject("knowledge:ke_conflict_a").state).toBe("contested");

		const rollbackResult = await rollbackDreamApply(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
				OPENAI_API_KEY: "test-openai-key",
			} as Env,
			{
				proposalId: String(proposal.run_id),
				applyMutationId: "apply-contested-test",
				rollbackMutationId: "rollback-contested-test",
				actorId: "test-operator",
				reason: "rollback contested marker",
				operationIds: [String(contestedOperation!.operation_id)],
			},
		);

		expect(rollbackResult.ok).toBe(true);
		expect(rollbackResult.rolled_back_count).toBe(1);
		const conflictA = getStoredObject("knowledge:ke_conflict_a");
		const conflictB = getStoredObject("knowledge:ke_conflict_b");
		expect(conflictA.state).toBe("active");
		expect(conflictB.state).toBe("active");
		expect(conflictA.related_knowledge).toEqual([]);
		expect(conflictB.related_knowledge).toEqual([]);
		// Revision is monotonic: the mark_contested apply bumps 0->1 and the rollback
		// is itself a forward write that bumps 1->2. Rollback restores content/state
		// from the before-snapshot but never rewinds the revision counter.
		expect((conflictA.metadata as Record<string, unknown>).revision).toBe(2);
		expect(mockState.store.get(`dream:run:${String(proposal.run_id)}:rollback:rollback-contested-test`)).toBeTruthy();
	});

	it("rolls back duplicate merge operations from apply before-snapshots", async () => {
		const proposal = await runDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
			} as Env,
			{
				trigger: "local_test",
				actorId: "test-operator",
				archiveLimit: 0,
				promotionLimit: 0,
			},
		);
		const duplicateOperation = (proposal.operations as Array<Record<string, unknown>>)
			.find((operation) => operation.type === "duplicate_merge");
		await gradeDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
			} as Env,
			{ proposalId: String(proposal.run_id), actorId: "test-operator" },
		);
		const applyResult = await applyDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
			} as Env,
			{
				proposalId: String(proposal.run_id),
				mutationId: "apply-duplicate-rollback-test",
				actorId: "test-operator",
				reason: "approve duplicate merge",
				operationIds: [String(duplicateOperation!.operation_id)],
			},
		);
		expect(applyResult.ok).toBe(true);
		expect(getStoredObject("knowledge:ke_dup_primary").metadata).toEqual(
			expect.objectContaining({ mention_count: 3 }),
		);
		expect(getStoredObject("knowledge:ke_dup_secondary").metadata).toEqual(
			expect.objectContaining({ archived: true }),
		);
		expect((getStoredObject("mutation_result:apply-duplicate-rollback-test").before_snapshots as Record<string, unknown>))
			.toBeTruthy();

		const result = await rollbackDreamApply(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
				OPENAI_API_KEY: "test-openai-key",
			} as Env,
			{
				proposalId: String(proposal.run_id),
				applyMutationId: "apply-duplicate-rollback-test",
				rollbackMutationId: "rollback-duplicate-test",
				actorId: "test-operator",
				reason: "rollback duplicate merge",
				operationIds: [String(duplicateOperation!.operation_id)],
			},
		);

		expect(result.ok).toBe(true);
		expect(result.rolled_back_count).toBe(1);
		expect(getStoredObject("knowledge:ke_dup_primary").metadata).toEqual(
			expect.objectContaining({ mention_count: 2 }),
		);
		expect(getStoredObject("knowledge:ke_dup_secondary").metadata).toEqual(
			expect.objectContaining({ archived: false }),
		);
		expect(mockState.vectorUpserts).toEqual(
			expect.arrayContaining([
				expect.objectContaining({ id: "ke_dup_primary" }),
				expect.objectContaining({ id: "ke_dup_secondary" }),
			]),
		);
	});

	it("blocks Phase 9 gated apply when the pre-outcome baseline already fails", async () => {
		const proposal = await runDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
			} as Env,
			{
				trigger: "local_test",
				actorId: "test-operator",
				archiveLimit: 0,
				promotionLimit: 0,
			},
		);
		const duplicateOperation = (proposal.operations as Array<Record<string, unknown>>)
			.find((operation) => operation.type === "duplicate_merge");
		await gradeDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
			} as Env,
			{ proposalId: String(proposal.run_id), actorId: "test-operator" },
		);

		const result = await applyDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
			} as Env,
			{
				proposalId: String(proposal.run_id),
				mutationId: "phase9-pre-fails",
				actorId: "test-operator",
				reason: "phase9 pre failure test",
				operationIds: [String(duplicateOperation!.operation_id)],
				phase9OutcomeGate: true,
				phase9Probes: [
					{
						id: "bad_pre_baseline",
						query: "country equity rotation signals",
						expected_top_entry_id: "ke_missing",
					},
				],
			},
		);

		expect(result.ok).toBe(false);
		expect(result.error).toBe("phase9_pre_outcome_baseline_failed");
		expect(getStoredObject("knowledge:ke_dup_secondary").metadata).toEqual(
			expect.objectContaining({ archived: false }),
		);
		expect(mockState.vectorDeletes).toEqual([]);
		expect(mockState.store.get(`dream:run:${String(proposal.run_id)}:phase9:phase9-pre-fails`)).toBeTruthy();
	});

	it("passes Phase 9 gated apply and writes the validation ledger when probes stay green", async () => {
		const proposal = await runDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
			} as Env,
			{
				trigger: "local_test",
				actorId: "test-operator",
				archiveLimit: 0,
				promotionLimit: 0,
			},
		);
		const duplicateOperation = (proposal.operations as Array<Record<string, unknown>>)
			.find((operation) => operation.type === "duplicate_merge");
		await gradeDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
			} as Env,
			{ proposalId: String(proposal.run_id), actorId: "test-operator" },
		);

		const result = await applyDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
				OPENAI_API_KEY: "test-openai-key",
			} as Env,
			{
				proposalId: String(proposal.run_id),
				mutationId: "phase9-gate-passes",
				actorId: "test-operator",
				reason: "phase9 pass test",
				operationIds: [String(duplicateOperation!.operation_id)],
				phase9OutcomeGate: true,
				phase9WriteValidationLedger: true,
				phase9Probes: [
					{
						id: "primary_recall_survives_merge",
						query: "cross border GDELT sentiment country ETF rotation",
						expected_entry_ids: ["ke_dup_primary"],
						top_k: 10,
					},
				],
			},
		);

		expect(result.ok).toBe(true);
		expect((result.phase9_outcome_gate as Record<string, unknown>).status).toBe("passed");
		const ledger = JSON.parse(String(mockState.store.get("validation:last"))) as Record<string, unknown>;
		expect(ledger).toMatchObject({
			gate: "dream_outcome_quality",
			status: "pass",
		});
		const gateStatus = JSON.parse(String(mockState.store.get("validation:gate_status"))) as Record<string, any>;
		expect(gateStatus.gates.dream_outcome_quality.passed).toBe(true);
	});

	it("auto-rolls back Phase 9 gated apply when post-outcome probes regress", async () => {
		const proposal = await runDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
			} as Env,
			{
				trigger: "local_test",
				actorId: "test-operator",
				archiveLimit: 0,
				promotionLimit: 0,
			},
		);
		const duplicateOperation = (proposal.operations as Array<Record<string, unknown>>)
			.find((operation) => operation.type === "duplicate_merge");
		await gradeDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
			} as Env,
			{ proposalId: String(proposal.run_id), actorId: "test-operator" },
		);

		const result = await applyDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
				OPENAI_API_KEY: "test-openai-key",
			} as Env,
			{
				proposalId: String(proposal.run_id),
				mutationId: "phase9-regresses",
				actorId: "test-operator",
				reason: "phase9 regression rollback test",
				operationIds: [String(duplicateOperation!.operation_id)],
				phase9OutcomeGate: true,
				phase9AutoRollback: true,
				phase9Probes: [
					{
						id: "secondary_recall_regresses",
						query: "rank country ETFs",
						expected_entry_ids: ["ke_dup_secondary"],
						top_k: 10,
					},
				],
			},
		);

		expect(result.ok).toBe(false);
		expect(result.error).toBe("phase9_outcome_regression_rolled_back");
		expect(result.rolled_back).toBe(true);
		const phase9 = result.phase9_outcome_gate as Record<string, any>;
		expect(phase9.status).toBe("regression_rolled_back");
		expect(phase9.gate_report.rollback_required).toBe(true);
		expect(phase9.rollback_result.ok).toBe(true);
		expect(getStoredObject("knowledge:ke_dup_primary").metadata).toEqual(
			expect.objectContaining({ mention_count: 2 }),
		);
		expect(getStoredObject("knowledge:ke_dup_secondary").metadata).toEqual(
			expect.objectContaining({ archived: false }),
		);
		expect(mockState.store.get(`dream:run:${String(proposal.run_id)}:phase9:phase9-regresses`)).toBeTruthy();
		expect(mockState.store.get(`dream:run:${String(proposal.run_id)}:rollback:${phase9.rollback_recommendation.rollback_mutation_id}`)).toBeTruthy();
	});

	it("compacts oversized stored Dream audits below the Redis payload budget", () => {
		const largeBlock = "x".repeat(1_200);
		const largeItems = Array.from({ length: 24 }, (_, index) => ({
			id: `ke_large_${index}`,
			type: "knowledge",
			label: `Large Dream item ${index}`,
			current_view: largeBlock,
			metadata: {
				evidence: largeBlock,
				source_conversations: [`conv_${index}`],
				source_messages: [`msg_${index}`],
			},
		}));

		const runRecord = {
			schema_version: 1,
			run_id: "dr_compaction_test",
			run_at: "2026-04-09T07:10:00.000Z",
			completed_at: "2026-04-09T07:11:00.000Z",
			status: "completed",
			dry_run: false,
			trigger: "scheduled",
			counts: {
				archive_candidates: largeItems.length,
				merged_duplicates: largeItems.length,
			},
			phases: {
				replay: {
					status: "completed",
					duplicate_merge_count: largeItems.length,
					merged_entries: largeItems,
					contradiction_entries: largeItems,
					promoted_entries: largeItems,
				},
				prune: {
					status: "completed",
					archive_candidate_count: largeItems.length,
				},
			},
			duplicate_plans: largeItems,
			contradiction_plans: largeItems,
			merged_entries: largeItems,
			contradiction_entries: largeItems,
			archive_candidates: largeItems,
			archived_entries: largeItems,
			promoted_entries: largeItems,
			archive_candidates_sample: largeItems,
			next_action: largeBlock,
		} satisfies Record<string, unknown>;

		const compacted = compactDreamRunRecordForStorage(runRecord, {
			maxBytes: 40_000,
			sampleLimit: 4,
			fallbackSampleLimit: 2,
		});
		const storageCompaction = compacted.storage_compaction as Record<string, unknown>;
		const sampledFields = storageCompaction.sampled_fields as Record<string, unknown>;
		const mergedEntriesSummary = sampledFields.merged_entries as Record<string, unknown>;

		const compactedSize = Buffer.byteLength(JSON.stringify(compacted), "utf8");
		expect(compactedSize).toBeLessThanOrEqual(40_000);
		expect(storageCompaction.mode).toBe("sampled");
		expect((compacted.merged_entries as unknown[]).length).toBe(4);
		expect(
			((compacted.phases as Record<string, unknown>).replay as Record<string, unknown>).merged_entries,
		).toBeUndefined();
		expect(mergedEntriesSummary.total_count).toBe(largeItems.length);
	});

	it("falls back to a minimal stored Dream audit when sampling alone is still too large", () => {
		const largeBlock = "y".repeat(2_000);
		const largeItems = Array.from({ length: 12 }, (_, index) => ({
			id: `ke_min_${index}`,
			label: `Minimal fallback ${index}`,
			detail: largeBlock,
		}));

		const runRecord = {
			schema_version: 1,
			run_id: "dr_minimal_compaction_test",
			run_at: "2026-04-09T07:10:00.000Z",
			completed_at: "2026-04-09T07:11:00.000Z",
			status: "completed",
			dry_run: true,
			trigger: "manual",
			counts: {
				archive_candidates: largeItems.length,
			},
			phases: {
				replay: {
					status: "dry_run",
					merged_entries: largeItems,
				},
			},
			archive_candidates: largeItems,
			archived_entries: largeItems,
			next_action: largeBlock,
		} satisfies Record<string, unknown>;

		const compacted = compactDreamRunRecordForStorage(runRecord, {
			maxBytes: 1_500,
			sampleLimit: 3,
			fallbackSampleLimit: 1,
		});

		const compactedSize = Buffer.byteLength(JSON.stringify(compacted), "utf8");
		expect(compactedSize).toBeLessThanOrEqual(1_500);
		expect((compacted.storage_compaction as Record<string, unknown>).mode).toBe("minimal");
		expect((compacted.archive_candidates as unknown[]).length).toBe(1);
		expect((compacted.archived_entries as unknown[]).length).toBe(1);
	});

	it("batches Dream mget calls so large corpora stay under Redis request limits", async () => {
		mockState.maxMgetKeys = 25;

		for (let index = 0; index < 80; index += 1) {
			mockState.store.set(
				`knowledge:ke_batch_${index}`,
				buildKnowledgeEntry({
					id: `ke_batch_${index}`,
					domain: `Batched load ${index}`,
					currentView: `Corpus item ${index}`,
					contextType: "task_query",
					injectionTier: 3,
					mentionCount: 1,
					accessCount: 0,
					sourceConversations: [`conv_batch_${index}`],
					updatedAt: "2026-03-28T06:00:00.000Z",
				}),
			);
		}

		const result = await runDreamCycle(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
			} as Env,
			{
				dryRun: true,
				trigger: "local_test",
				note: "batched mget test",
				setAsLatest: false,
			},
		);

		expect((result.counts as Record<string, unknown>).knowledge_entries).toBeGreaterThanOrEqual(84);
		expect(Math.max(...mockState.mgetCallSizes)).toBeLessThanOrEqual(25);
	});

	it("reclaims stale Dream locks before starting a new run", async () => {
		mockState.store.set(
			"dream:lock",
			JSON.stringify({
				run_id: "dr_stale_lock",
				run_at: "2026-04-09T00:00:00.000Z",
				trigger: "scheduled",
				dry_run: false,
			}),
		);

		const result = await runDreamCycle(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
			} as Env,
			{
				dryRun: true,
				trigger: "local_test",
				note: "stale lock recovery test",
				setAsLatest: false,
			},
		);

		expect(result.status).toBe("completed");
		expect(mockState.store.get("dream:lock")).toBeUndefined();
	});
});
