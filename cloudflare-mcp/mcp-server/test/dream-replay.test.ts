import { beforeEach, describe, expect, it, vi } from "vitest";

const mockState = vi.hoisted(() => ({
	store: new Map<string, unknown>(),
	lists: new Map<string, string[]>(),
	vectorUpdates: [] as Array<Record<string, unknown>>,
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
	},
}));

vi.mock("@upstash/vector", () => ({
	Index: class MockIndex {
		async update(payload: Record<string, unknown>): Promise<void> {
			mockState.vectorUpdates.push(payload);
		}

		async delete(_id: string | string[]): Promise<void> {
			const ids = Array.isArray(_id) ? _id : [_id];
			mockState.vectorDeletes.push(...ids);
		}
	},
}));

import { applyDreamProposal, compactDreamRunRecordForStorage, runDreamCycle, runDreamProposal } from "../src/dream";

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
		mockState.vectorUpdates.length = 0;
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
				accessCount: 1,
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
		expect(canonicalMetadata.access_count).toBe(1);
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

	it("applies selected proposal operations after revision preflight", async () => {
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
		expect(duplicateOperation).toBeTruthy();

		const result = await applyDreamProposal(
			{
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
				UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
				UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
				UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
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
		const archivedDuplicate = getStoredObject("knowledge:ke_dup_secondary");
		expect((archivedDuplicate.metadata as Record<string, unknown>).archived).toBe(true);
		expect(mockState.vectorDeletes).toContain("ke_dup_secondary");
		expect(mockState.store.get(`dream:run:${String(proposal.run_id)}:apply:apply-proposal-test`)).toBeTruthy();
		expect(mockState.store.get("mutation_result:apply-proposal-test")).toBeTruthy();
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
