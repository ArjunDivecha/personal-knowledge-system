import { beforeEach, describe, expect, it, vi } from "vitest";

const mockState = vi.hoisted(() => ({
	store: new Map<string, unknown>(),
	vectorUpdates: [] as Array<Record<string, unknown>>,
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
	},
}));

vi.mock("@upstash/vector", () => ({
	Index: class MockIndex {
		async update(payload: Record<string, unknown>): Promise<void> {
			mockState.vectorUpdates.push(payload);
		}
	},
}));

import { runDreamCycle } from "../src/dream";

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
			salience_score: 0.4,
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
		mockState.vectorUpdates.length = 0;
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
				expect.objectContaining({
					id: "ke_dup_secondary",
					metadata: expect.objectContaining({ archived: true }),
				}),
			]),
		);
	});
});
