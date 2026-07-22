// Tests for Dream insight synthesis — CMA-dreaming parity.
// See docs/pks-dream-insight-synthesis-prd-2026-07-02.md.
//
// Covers: pure cluster detection (band, size, domain diversity, determinism),
// eligibility filtering, the detection+enqueue phase (seen-fingerprint dedup,
// per-run cap, fail-open on a dead vector store), synthesis-block validation,
// and content-bearing verdict application (append via addInsight, create via
// createEntry, stale paths, idempotency).

import { beforeEach, describe, expect, it, vi } from "vitest";

const mockState = vi.hoisted(() => ({
	store: new Map<string, unknown>(),
	lists: new Map<string, string[]>(),
	sets: new Map<string, Set<string>>(),
	vectorUpserts: [] as Array<Record<string, unknown>>,
	vectorDeletes: [] as string[],
}));

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

		async setnx(key: string, value: unknown): Promise<number> {
			if (mockState.store.has(key)) {
				return 0;
			}
			mockState.store.set(key, value);
			return 1;
		}

		async del(...keys: string[]): Promise<number> {
			let removed = 0;
			for (const key of keys) {
				if (mockState.store.delete(key)) removed += 1;
				mockState.sets.delete(key);
			}
			return removed;
		}

		async mget<T>(keys: string[]): Promise<T[]> {
			return keys.map((key) => (mockState.store.get(key) ?? null) as T);
		}

		async scan(
			cursor: string,
			options?: { match?: string; count?: number },
		): Promise<[string, string[]]> {
			if (cursor !== "0") return ["0", []];
			const pattern = options?.match
				? new RegExp(`^${options.match.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*")}$`)
				: /.*/;
			return ["0", [...mockState.store.keys()].filter((key) => pattern.test(key))];
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

		async lrange(key: string, start: number, stop: number): Promise<string[]> {
			const list = mockState.lists.get(key) ?? [];
			return list.slice(start, stop === -1 ? undefined : stop + 1);
		}

		async sadd(key: string, value: string): Promise<number> {
			const set = mockState.sets.get(key) ?? new Set<string>();
			const had = set.has(value);
			set.add(value);
			mockState.sets.set(key, set);
			return had ? 0 : 1;
		}

		async srem(key: string, value: string): Promise<number> {
			const set = mockState.sets.get(key);
			if (!set) return 0;
			return set.delete(value) ? 1 : 0;
		}

		async smembers(key: string): Promise<string[]> {
			return [...(mockState.sets.get(key) ?? new Set<string>())];
		}

		async rename(source: string, target: string): Promise<void> {
			if (mockState.store.has(source)) {
				mockState.store.set(target, mockState.store.get(source));
				mockState.store.delete(source);
			}
		}
	},
}));

vi.mock("@upstash/vector", () => ({
	Index: class MockIndex {
		async upsert(payload: Record<string, unknown>): Promise<void> {
			mockState.vectorUpserts.push(payload);
		}

		async update(payload: Record<string, unknown>): Promise<void> {
			mockState.vectorUpserts.push(payload);
		}

		async delete(id: string | string[]): Promise<void> {
			const ids = Array.isArray(id) ? id : [id];
			mockState.vectorDeletes.push(...ids);
		}

		async fetch(): Promise<Array<Record<string, unknown>>> {
			return [];
		}

		async query(): Promise<Array<Record<string, unknown>>> {
			return [];
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

import { Redis } from "@upstash/redis/cloudflare";
import type { JudgeQueueItem, JudgeVerdict } from "../src/judgeQueue";
import { judgeItemKey, QUEUE_PENDING_SET } from "../src/judgeQueue";
import {
	applyInsightSynthesisVerdict,
	buildInsightClusters,
	INSIGHT_SEEN_PREFIX,
	INSIGHT_SYNTHESIS_ACTOR,
	insightClusterFingerprint,
	isInsightClusterEligible,
	readInsightSynthesisConfig,
	runInsightSynthesisPhase,
	validateInsightSynthesis,
} from "../src/dream";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

type Loaded = Parameters<typeof isInsightClusterEligible>[0];

function makeLoaded(params: {
	id: string;
	domain?: string;
	type?: "knowledge" | "project";
	state?: string;
	archived?: boolean;
	quarantined?: boolean;
	salience?: number;
	accessCount?: number;
	currentView?: string;
}): Loaded {
	const metadata: Record<string, unknown> = {
		archived: params.archived ?? false,
		revision: 3,
		access_count: params.accessCount ?? 0,
		context_type: "recurring_pattern",
	};
	if (params.quarantined) metadata.injection_quarantine = true;
	return {
		id: params.id,
		type: params.type ?? "knowledge",
		entry: {
			id: params.id,
			domain: params.domain ?? `domain ${params.id}`,
			state: params.state ?? "active",
			current_view: params.currentView ?? `view for ${params.id}`,
			key_insights: [],
			metadata,
		},
		metadata,
		label: (params.domain ?? `domain ${params.id}`).toLowerCase(),
		updatedAt: "2026-07-01T00:00:00.000Z",
		contextType: "recurring_pattern",
		injectionTier: 2,
		mentionCount: 1,
		accessCount: params.accessCount ?? 0,
		sourceConversationCount: 1,
		salienceScore: params.salience ?? 0.4,
	} as Loaded;
}

/** Fake vector store where entry i's vector is [i] and query([i]) returns a
 *  configured neighbor list — enough for makeVectorNeighborFn's fetch+query. */
function makeFakeVector(
	entryIds: string[],
	neighbors: Record<string, Array<{ id: string; score: number }>>,
	options: { failQuery?: boolean } = {},
) {
	const indexById = new Map(entryIds.map((id, i) => [id, i]));
	return {
		async fetch(ids: string[], _opts: Record<string, unknown>) {
			return ids.map((id) => ({ id, vector: [indexById.get(id) ?? -1] }));
		},
		async query(payload: { vector: number[] }) {
			if (options.failQuery) throw new Error("vector store unavailable");
			const queryId = entryIds[payload.vector[0]];
			return (neighbors[queryId] ?? []).map((hit) => ({ id: hit.id, score: hit.score }));
		},
		async upsert() {},
		async update() {},
		async delete() {},
	};
}

function makeEnv(): Env {
	return {
		UPSTASH_REDIS_REST_URL: "https://redis.test.local",
		UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
		UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
		UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
		OPENAI_API_KEY: "test-openai-key",
	} as unknown as Env;
}

function makeItem(targetIds: string[], opId = "op_test_insight_1"): JudgeQueueItem {
	return {
		op_id: opId,
		op_type: "insight_synthesis",
		proposal_run_id: "dr_test",
		enqueued_at: "2026-07-02T00:00:00.000Z",
		target_entry_ids: targetIds,
		rubric: "test rubric",
		payload: {},
	};
}

function makeVerdict(
	synthesis: JudgeVerdict["synthesis"],
	verdict: "apply" | "skip" = "apply",
): JudgeVerdict {
	return {
		op_id: "op_test_insight_1",
		verdict,
		reason: "test reason",
		judged_at: "2026-07-02T01:00:00.000Z",
		judge_model: "claude-opus-4-6",
		judge_source: "claude_cli",
		synthesis,
	};
}

function getStored(key: string): Record<string, unknown> {
	const raw = mockState.store.get(key);
	return typeof raw === "string" ? (JSON.parse(raw) as Record<string, unknown>) : (raw as Record<string, unknown>);
}

beforeEach(() => {
	mockState.store.clear();
	mockState.lists.clear();
	mockState.sets.clear();
	mockState.vectorUpserts.length = 0;
	mockState.vectorDeletes.length = 0;
});

// ---------------------------------------------------------------------------
// Pure detection logic
// ---------------------------------------------------------------------------

describe("isInsightClusterEligible", () => {
	it("accepts an active knowledge entry", () => {
		expect(isInsightClusterEligible(makeLoaded({ id: "ke_a" }))).toBe(true);
	});

	it("rejects project entries, archived, contested, and quarantined entries", () => {
		expect(isInsightClusterEligible(makeLoaded({ id: "pe_a", type: "project" }))).toBe(false);
		expect(isInsightClusterEligible(makeLoaded({ id: "ke_b", archived: true }))).toBe(false);
		expect(isInsightClusterEligible(makeLoaded({ id: "ke_c", state: "contested" }))).toBe(false);
		expect(isInsightClusterEligible(makeLoaded({ id: "ke_d", quarantined: true }))).toBe(false);
	});
});

describe("buildInsightClusters", () => {
	const config = { minClusterSize: 3, maxClusterSize: 6 };

	it("forms a cluster from band edges spanning two domains", () => {
		const entries = [
			makeLoaded({ id: "ke_a", domain: "quant factor research" }),
			makeLoaded({ id: "ke_b", domain: "portfolio construction" }),
			makeLoaded({ id: "ke_c", domain: "quant factor research" }),
		];
		const edges = [
			{ a: "ke_a", b: "ke_b", cosine: 0.85 },
			{ a: "ke_b", b: "ke_c", cosine: 0.83 },
		];
		const clusters = buildInsightClusters(entries, edges, config);
		expect(clusters).toHaveLength(1);
		expect(clusters[0].member_ids).toEqual(["ke_a", "ke_b", "ke_c"]);
		expect(clusters[0].domains).toEqual(["portfolio construction", "quant factor research"]);
		expect(clusters[0].fingerprint).toBe(insightClusterFingerprint(["ke_c", "ke_a", "ke_b"]));
		expect(clusters[0].min_cosine).toBe(0.83);
		expect(clusters[0].max_cosine).toBe(0.85);
	});

	it("drops clusters below min size, above max size, or with one domain", () => {
		const pairOnly = buildInsightClusters(
			[makeLoaded({ id: "ke_a", domain: "x" }), makeLoaded({ id: "ke_b", domain: "y" })],
			[{ a: "ke_a", b: "ke_b", cosine: 0.85 }],
			config,
		);
		expect(pairOnly).toHaveLength(0);

		const singleDomain = buildInsightClusters(
			[
				makeLoaded({ id: "ke_a", domain: "same" }),
				makeLoaded({ id: "ke_b", domain: "same" }),
				makeLoaded({ id: "ke_c", domain: "same" }),
			],
			[
				{ a: "ke_a", b: "ke_b", cosine: 0.85 },
				{ a: "ke_b", b: "ke_c", cosine: 0.85 },
			],
			config,
		);
		expect(singleDomain).toHaveLength(0);

		const oversizedIds = ["ke_1", "ke_2", "ke_3", "ke_4", "ke_5", "ke_6", "ke_7"];
		const oversized = buildInsightClusters(
			oversizedIds.map((id, i) => makeLoaded({ id, domain: `domain ${i}` })),
			oversizedIds.slice(1).map((id, i) => ({ a: oversizedIds[i], b: id, cosine: 0.85 })),
			config,
		);
		expect(oversized).toHaveLength(0);
	});

	it("orders clusters deterministically by fingerprint", () => {
		const entries = [
			makeLoaded({ id: "ke_z1", domain: "a" }),
			makeLoaded({ id: "ke_z2", domain: "b" }),
			makeLoaded({ id: "ke_z3", domain: "c" }),
			makeLoaded({ id: "ke_a1", domain: "d" }),
			makeLoaded({ id: "ke_a2", domain: "e" }),
			makeLoaded({ id: "ke_a3", domain: "f" }),
		];
		const edges = [
			{ a: "ke_z1", b: "ke_z2", cosine: 0.85 },
			{ a: "ke_z2", b: "ke_z3", cosine: 0.85 },
			{ a: "ke_a1", b: "ke_a2", cosine: 0.85 },
			{ a: "ke_a2", b: "ke_a3", cosine: 0.85 },
		];
		const clusters = buildInsightClusters(entries, edges, config);
		expect(clusters.map((c) => c.member_ids[0])).toEqual(["ke_a1", "ke_z1"]);
	});
});

describe("validateInsightSynthesis", () => {
	const targets = ["ke_a", "ke_b", "ke_c"];

	it("accepts valid append and create blocks", () => {
		expect(
			validateInsightSynthesis(
				{ insight_text: "A durable pattern.", placement: "append", anchor_entry_id: "ke_b", support_entry_ids: targets },
				targets,
				500,
			),
		).toBeNull();
		expect(
			validateInsightSynthesis(
				{ insight_text: "A durable pattern.", placement: "create", domain: "cross-domain synthesis", support_entry_ids: targets },
				targets,
				500,
			),
		).toBeNull();
	});

	it("rejects every malformed shape", () => {
		expect(validateInsightSynthesis(undefined, targets, 500)).toBe("missing_synthesis_block");
		expect(
			validateInsightSynthesis({ insight_text: "  ", placement: "append", anchor_entry_id: "ke_a", support_entry_ids: targets }, targets, 500),
		).toBe("empty_insight_text");
		expect(
			validateInsightSynthesis({ insight_text: "x".repeat(501), placement: "create", domain: "d", support_entry_ids: targets }, targets, 500),
		).toBe("insight_text_too_long");
		expect(validateInsightSynthesis({ insight_text: "ok", placement: "append", support_entry_ids: targets }, targets, 500)).toBe(
			"missing_anchor_entry_id",
		);
		expect(
			validateInsightSynthesis(
				{ insight_text: "ok", placement: "append", anchor_entry_id: "ke_outside", support_entry_ids: targets },
				targets,
				500,
			),
		).toBe("anchor_outside_cluster");
		expect(validateInsightSynthesis({ insight_text: "ok", placement: "create", support_entry_ids: targets }, targets, 500)).toBe(
			"missing_domain",
		);
		expect(
			validateInsightSynthesis(
				{ insight_text: "ok", placement: "replace" as never, anchor_entry_id: "ke_a", support_entry_ids: targets },
				targets,
				500,
			),
		).toBe("invalid_placement");
		expect(validateInsightSynthesis({ insight_text: "ok", placement: "create", domain: "d" }, targets, 500)).toBe(
			"missing_support_entry_ids",
		);
		expect(
			validateInsightSynthesis(
				{ insight_text: "ok", placement: "create", domain: "d", support_entry_ids: ["ke_a", "ke_b", 3 as never] },
				targets,
				500,
			),
		).toBe("invalid_support_entry_id");
		expect(
			validateInsightSynthesis(
				{ insight_text: "ok", placement: "create", domain: "d", support_entry_ids: ["ke_a", "ke_b"] },
				targets,
				500,
			),
		).toBe("insufficient_support_entries");
		expect(
			validateInsightSynthesis(
				{ insight_text: "ok", placement: "create", domain: "d", support_entry_ids: ["ke_a", "ke_b", "ke_z"] },
				targets,
				500,
			),
		).toBe("support_entry_outside_cluster");
		expect(
			validateInsightSynthesis(
				{ insight_text: "ok", placement: "append", anchor_entry_id: "ke_c", support_entry_ids: ["ke_a", "ke_b", "ke_a"] },
				targets,
				500,
			),
		).toBe("insufficient_support_entries");
	});
});

// ---------------------------------------------------------------------------
// Detection + enqueue phase
// ---------------------------------------------------------------------------

describe("runInsightSynthesisPhase", () => {
	function makeRedis() {
		return new Redis({} as never);
	}

	it("detects a band cluster, enqueues one judge item, and marks the fingerprint seen", async () => {
		const redis = makeRedis();
		const entries = [
			makeLoaded({ id: "ke_a", domain: "signals", salience: 0.9, currentView: "GDELT sentiment ranks countries." }),
			makeLoaded({ id: "ke_b", domain: "portfolio", salience: 0.8 }),
			makeLoaded({ id: "ke_c", domain: "signals", salience: 0.7 }),
			// Duplicate-territory pair (0.96) must NOT produce insight edges.
			makeLoaded({ id: "ke_dup1", domain: "misc", salience: 0.6 }),
			makeLoaded({ id: "ke_dup2", domain: "other", salience: 0.5 }),
		];
		const ids = entries.map((e) => e.id);
		const vector = makeFakeVector(ids, {
			ke_a: [
				{ id: "ke_b", score: 0.85 },
				{ id: "ke_c", score: 0.83 },
			],
			ke_b: [{ id: "ke_c", score: 0.84 }],
			ke_dup1: [{ id: "ke_dup2", score: 0.96 }],
		});
		const summary = await runInsightSynthesisPhase(
			redis as never,
			vector as never,
			entries,
			"dr_test_run",
			"2026-07-02T05:00:00.000Z",
		);
		expect(summary.status).toBe("completed");
		expect(summary.clusters_detected).toBe(1);
		expect(summary.enqueued).toHaveLength(1);
		expect(summary.cap_hit).toBe(false);

		const enqueuedOpId = summary.enqueued[0].op_id as string;
		expect(mockState.sets.get(QUEUE_PENDING_SET)?.has(enqueuedOpId)).toBe(true);
		const item = getStored(judgeItemKey(enqueuedOpId)) as unknown as JudgeQueueItem;
		expect(item.op_type).toBe("insight_synthesis");
		expect(item.target_entry_ids).toEqual(["ke_a", "ke_b", "ke_c"]);
		expect(item.rubric).toContain("cross-cutting insight");
		const members = (item.payload as { members: Array<{ id: string; current_view: string | null }> }).members;
		expect(members.map((m) => m.id)).toEqual(["ke_a", "ke_b", "ke_c"]);
		expect(members[0].current_view).toContain("GDELT");

		const fingerprint = insightClusterFingerprint(["ke_a", "ke_b", "ke_c"]);
		expect(mockState.store.get(`${INSIGHT_SEEN_PREFIX}${fingerprint}`)).toBeTruthy();
	});

	it("skips clusters whose fingerprint was already seen", async () => {
		const redis = makeRedis();
		const entries = [
			makeLoaded({ id: "ke_a", domain: "signals" }),
			makeLoaded({ id: "ke_b", domain: "portfolio" }),
			makeLoaded({ id: "ke_c", domain: "signals" }),
		];
		const fingerprint = insightClusterFingerprint(["ke_a", "ke_b", "ke_c"]);
		mockState.store.set(`${INSIGHT_SEEN_PREFIX}${fingerprint}`, "2026-06-30T00:00:00.000Z");
		const vector = makeFakeVector(entries.map((e) => e.id), {
			ke_a: [
				{ id: "ke_b", score: 0.85 },
				{ id: "ke_c", score: 0.83 },
			],
		});
		const summary = await runInsightSynthesisPhase(
			redis as never,
			vector as never,
			entries,
			"dr_test_run",
			"2026-07-02T05:00:00.000Z",
		);
		expect(summary.clusters_detected).toBe(1);
		expect(summary.skipped_seen).toBe(1);
		expect(summary.enqueued).toHaveLength(0);
	});

	it("enforces the per-run enqueue cap", async () => {
		const redis = makeRedis();
		const cap = readInsightSynthesisConfig().perRunEnqueueCap;
		const clusterCount = cap + 2;
		const entries: Loaded[] = [];
		const neighbors: Record<string, Array<{ id: string; score: number }>> = {};
		for (let i = 0; i < clusterCount; i += 1) {
			const a = `ke_c${i}_a`;
			const b = `ke_c${i}_b`;
			const c = `ke_c${i}_c`;
			entries.push(
				makeLoaded({ id: a, domain: `alpha ${i}` }),
				makeLoaded({ id: b, domain: `beta ${i}` }),
				makeLoaded({ id: c, domain: `alpha ${i}` }),
			);
			neighbors[a] = [
				{ id: b, score: 0.85 },
				{ id: c, score: 0.84 },
			];
		}
		const vector = makeFakeVector(entries.map((e) => e.id), neighbors);
		const summary = await runInsightSynthesisPhase(
			redis as never,
			vector as never,
			entries,
			"dr_test_run",
			"2026-07-02T05:00:00.000Z",
		);
		expect(summary.clusters_detected).toBe(clusterCount);
		expect(summary.enqueued).toHaveLength(cap);
		expect(summary.cap_hit).toBe(true);
	});

	it("fails open when the vector store is unavailable", async () => {
		const redis = makeRedis();
		const entries = [
			makeLoaded({ id: "ke_a", domain: "signals" }),
			makeLoaded({ id: "ke_b", domain: "portfolio" }),
			makeLoaded({ id: "ke_c", domain: "signals" }),
		];
		const vector = makeFakeVector(entries.map((e) => e.id), {}, { failQuery: true });
		const summary = await runInsightSynthesisPhase(
			redis as never,
			vector as never,
			entries,
			"dr_test_run",
			"2026-07-02T05:00:00.000Z",
		);
		expect(summary.status).toBe("disabled_vector_unavailable");
		expect(summary.enqueued).toHaveLength(0);
	});
});

// ---------------------------------------------------------------------------
// Content-bearing verdict application
// ---------------------------------------------------------------------------

describe("applyInsightSynthesisVerdict", () => {
	function seedAnchor(id = "ke_anchor", archived = false): void {
		mockState.store.set(`knowledge:${id}`, {
			id,
			type: "knowledge",
			domain: "signals",
			state: "active",
			current_view: "Anchor entry view.",
			key_insights: [],
			evolution: [],
			metadata: {
				created_at: "2026-06-01T00:00:00.000Z",
				updated_at: "2026-06-01T00:00:00.000Z",
				context_type: "recurring_pattern",
				injection_tier: 2,
				mention_count: 1,
				access_count: 0,
				revision: 4,
				archived,
			},
		});
	}

	const targets = ["ke_anchor", "ke_b", "ke_c"];

	it("settles skip verdicts as skipped without touching the store", async () => {
		const redis = new Redis({} as never);
		const outcome = await applyInsightSynthesisVerdict(
			makeEnv(),
			redis as never,
			makeItem(targets),
			makeVerdict(undefined, "skip"),
			"dr_run",
		);
		expect(outcome.outcome).toBe("skipped");
	});

	it("returns stale on a missing or invalid synthesis block", async () => {
		const redis = new Redis({} as never);
		const missing = await applyInsightSynthesisVerdict(
			makeEnv(),
			redis as never,
			makeItem(targets),
			makeVerdict(undefined),
			"dr_run",
		);
		expect(missing.outcome).toBe("stale");
		expect(missing.detail.error).toBe("missing_synthesis_block");

		const outside = await applyInsightSynthesisVerdict(
			makeEnv(),
			redis as never,
			makeItem(targets),
			makeVerdict({ insight_text: "ok", placement: "append", anchor_entry_id: "ke_elsewhere", support_entry_ids: targets }),
			"dr_run",
		);
		expect(outside.outcome).toBe("stale");
		expect(outside.detail.error).toBe("anchor_outside_cluster");
	});

	it("appends the insight to the anchor entry via addInsight", async () => {
		seedAnchor();
		const redis = new Redis({} as never);
		const outcome = await applyInsightSynthesisVerdict(
			makeEnv(),
			redis as never,
			makeItem(targets),
			makeVerdict({
				insight_text: "Sentiment breadth and portfolio churn move together.",
				placement: "append",
				anchor_entry_id: "ke_anchor",
				support_entry_ids: targets,
			}),
			"dr_run",
		);
		expect(outcome.outcome).toBe("applied");
		expect(outcome.detail.placement).toBe("append");

		const entry = getStored("knowledge:ke_anchor");
		const insights = entry.key_insights as Array<{ insight: string; evidence: { snippet: string; support_entry_ids: string[] } }>;
		expect(insights).toHaveLength(1);
		expect(insights[0].insight).toBe("Sentiment breadth and portfolio churn move together.");
		expect(insights[0].evidence.snippet).toContain("ke_b");
		expect(insights[0].evidence.support_entry_ids).toEqual(targets);
		expect(outcome.detail.support_entry_ids).toEqual(targets);
		const metadata = entry.metadata as Record<string, unknown>;
		expect(metadata.revision).toBe(5);
		expect((metadata.updated_by as Record<string, unknown>).actor_id).toBe(INSIGHT_SYNTHESIS_ACTOR);
	});

	it("is idempotent: a retried apply returns the stored result without double-appending", async () => {
		seedAnchor();
		const redis = new Redis({} as never);
		const verdict = makeVerdict({
			insight_text: "Sentiment breadth and portfolio churn move together.",
			placement: "append",
			anchor_entry_id: "ke_anchor",
			support_entry_ids: targets,
		});
		const first = await applyInsightSynthesisVerdict(makeEnv(), redis as never, makeItem(targets), verdict, "dr_run");
		expect(first.outcome).toBe("applied");
		const second = await applyInsightSynthesisVerdict(makeEnv(), redis as never, makeItem(targets), verdict, "dr_run");
		expect(second.outcome).toBe("applied");
		const entry = getStored("knowledge:ke_anchor");
		expect(entry.key_insights as unknown[]).toHaveLength(1);
	});

	it("returns stale when the anchor is archived or missing", async () => {
		seedAnchor("ke_anchor", true);
		const redis = new Redis({} as never);
		const archived = await applyInsightSynthesisVerdict(
			makeEnv(),
			redis as never,
			makeItem(targets),
			makeVerdict({ insight_text: "ok", placement: "append", anchor_entry_id: "ke_anchor", support_entry_ids: targets }),
			"dr_run",
		);
		expect(archived.outcome).toBe("stale");
		expect(archived.detail.error).toBe("anchor_archived");

		const missing = await applyInsightSynthesisVerdict(
			makeEnv(),
			redis as never,
			makeItem(["ke_gone", "ke_b", "ke_c"], "op_test_insight_2"),
			makeVerdict({ insight_text: "ok", placement: "append", anchor_entry_id: "ke_gone", support_entry_ids: ["ke_gone", "ke_b", "ke_c"] }),
			"dr_run",
		);
		expect(missing.outcome).toBe("stale");
		expect(missing.detail.error).toBe("anchor_not_found");
	});

	it("creates a recurring_pattern entry for placement create with cluster provenance", async () => {
		const redis = new Redis({} as never);
		const outcome = await applyInsightSynthesisVerdict(
			makeEnv(),
			redis as never,
			makeItem(targets),
			makeVerdict({
				insight_text: "Across signals and portfolio work, turnover limits dominate alpha capture.",
				placement: "create",
				domain: "turnover-aware alpha capture",
				support_entry_ids: targets,
			}),
			"dr_run",
		);
		expect(outcome.outcome).toBe("applied");
		expect(outcome.detail.placement).toBe("create");
		const createdId = outcome.detail.created_id as string;
		expect(createdId).toMatch(/^ke_/);

		const entry = getStored(`knowledge:${createdId}`);
		expect(entry.domain).toBe("turnover-aware alpha capture");
		expect(entry.current_view).toBe(
			"Across signals and portfolio work, turnover limits dominate alpha capture.",
		);
		const metadata = entry.metadata as Record<string, unknown>;
		expect(metadata.context_type).toBe("recurring_pattern");
		expect(metadata.evidence_support_entry_ids).toEqual(targets);
		expect((metadata.updated_by as Record<string, unknown>).actor_id).toBe(INSIGHT_SYNTHESIS_ACTOR);
		// Vector embedding was written for the new entry.
		expect(mockState.vectorUpserts.some((row) => row.id === createdId)).toBe(true);
	});
});
