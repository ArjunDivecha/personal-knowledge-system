/**
 * =============================================================================
 * SCRIPT NAME: dreamProposalScoping.test.ts
 * =============================================================================
 *
 * INPUT FILES: None. Redis, Upstash Vector and OpenAI are all replaced with
 * in-memory mocks (vi.mock) — this suite performs no file or network I/O and
 * needs no credentials.
 *
 * OUTPUT FILES: None.
 *
 * VERSION: 1.0
 * LAST UPDATED: 2026-07-13
 * AUTHOR: Claude (Opus 4.8) for Arjun Divecha
 *
 * DESCRIPTION (what this proves, in plain terms):
 * When Dream is asked to consider a specific short list of entries, it should
 * fetch just those entries. It used to fetch the ENTIRE memory store first and
 * throw away everything it didn't need. With ~12,000 entries that was slow
 * enough to kill the nightly job outright. These tests fetch a 60-entry fake
 * corpus, ask for 2 of them, and fail if Dream reads more than it was asked for.
 *
 * =============================================================================
 *
 * Regression guard: a TARGETED Dream proposal (one that supplies candidate_ids)
 * must load ONLY those candidate entries — not the entire corpus.
 *
 * WHY THIS EXISTS (production incident, 2026-07-11 .. 2026-07-13):
 * runDreamProposal used to scan every `knowledge:*` / `project:*` key and mget
 * the whole corpus, and only THEN filter down to candidate_ids. The cost was
 * therefore identical whether the caller wanted 200 entries or 12,000 — a full
 * key-scan plus three full-corpus mgets (entries, access counts, last-accessed)
 * plus a salience computation per entry.
 *
 * That was survivable while the only caller was the unfiltered nightly. Then
 * PKS-SEMANTIC-CONSOLIDATION-001 added runBoundedSemanticSlicePass, a SECOND
 * runDreamProposal call carrying candidate_ids — which also flips on semantic
 * mode (a vector NN query per candidate). On the real ~12k-entry corpus the
 * nightly Dream then exceeded the Worker's execution limits and hung in
 * `running_proposal`, so NO nightly ran for three days: no consolidation, no
 * forgetting, no thin-index rebuild. Retrieval was unaffected and no data was
 * lost, but Dream was dead.
 *
 * The fix scopes the load to the requested ids. These tests fail against the
 * old code (they observe the corpus-wide mget) and pass against the fix.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockState = vi.hoisted(() => ({
	store: new Map<string, unknown>(),
	lists: new Map<string, string[]>(),
	sets: new Map<string, Set<string>>(),
	mgetCallSizes: [] as number[],
	mgetKeys: [] as string[][],
	scanCalls: 0,
	scanPatterns: [] as string[],
}));

function globToRegex(pattern: string): RegExp {
	const escaped = pattern.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*");
	return new RegExp(`^${escaped}$`);
}

vi.mock("@upstash/redis/cloudflare", () => ({
	Redis: class MockRedis {
		async get(key: string) {
			return mockState.store.get(key) ?? null;
		}
		async set(key: string, value: unknown) {
			mockState.store.set(key, value);
			return "OK";
		}
		async setnx(key: string, value: unknown) {
			if (mockState.store.has(key)) return 0;
			mockState.store.set(key, value);
			return 1;
		}
		async incr(key: string) {
			const next = Number(mockState.store.get(key) ?? 0) + 1;
			mockState.store.set(key, next);
			return next;
		}
		async expire() {
			return 1;
		}
		async del(...keys: string[]) {
			let n = 0;
			for (const k of keys) if (mockState.store.delete(k)) n += 1;
			return n;
		}
		async scan(cursor: string, options?: { match?: string; count?: number }): Promise<[string, string[]]> {
			mockState.scanCalls += 1;
			if (options?.match) mockState.scanPatterns.push(options.match);
			if (cursor !== "0") return ["0", []];
			const matcher = options?.match ? globToRegex(options.match) : /.*/;
			return ["0", [...mockState.store.keys()].filter((k) => matcher.test(k))];
		}
		async mget<T>(keys: string[]): Promise<T[]> {
			mockState.mgetCallSizes.push(keys.length);
			mockState.mgetKeys.push(keys);
			return keys.map((k) => (mockState.store.get(k) ?? null) as T);
		}
		async lpush(key: string, ...values: string[]) {
			const list = mockState.lists.get(key) ?? [];
			list.unshift(...values);
			mockState.lists.set(key, list);
			return list.length;
		}
		async ltrim() {
			return "OK";
		}
		async lrange(key: string) {
			return mockState.lists.get(key) ?? [];
		}
		async sadd(key: string, ...members: string[]) {
			const s = mockState.sets.get(key) ?? new Set<string>();
			members.forEach((m) => s.add(m));
			mockState.sets.set(key, s);
			return members.length;
		}
		async srem() {
			return 1;
		}
		async smembers(key: string) {
			return [...(mockState.sets.get(key) ?? [])];
		}
		async scard(key: string) {
			return (mockState.sets.get(key) ?? new Set()).size;
		}
		async llen(key: string) {
			return (mockState.lists.get(key) ?? []).length;
		}
	},
}));

vi.mock("@upstash/vector", () => ({
	Index: class MockIndex {
		async query() {
			return [];
		}
		async upsert() {
			return "Success";
		}
		async delete() {
			return { deleted: 0 };
		}
		async fetch() {
			return [];
		}
	},
}));

vi.mock("openai", () => ({
	default: class MockOpenAI {
		embeddings = { create: async () => ({ data: [{ embedding: new Array(8).fill(0.1) }] }) };
	},
}));

const CORPUS_SIZE = 60;

function seedCorpus(): string[] {
	const ids: string[] = [];
	for (let i = 0; i < CORPUS_SIZE; i += 1) {
		const id = `ke_scope${String(i).padStart(4, "0")}`;
		ids.push(id);
		mockState.store.set(`knowledge:${id}`, JSON.stringify({
			id,
			type: "knowledge",
			domain: `Scoping fixture ${i}`,
			state: "active",
			detail_level: "full",
			current_view: `Fixture entry number ${i}.`,
			confidence: "medium",
			key_insights: [],
			related_knowledge: [],
			metadata: {
				created_at: "2026-01-01T00:00:00.000Z",
				updated_at: "2026-01-01T00:00:00.000Z",
				context_type: "task_query",
				mention_count: 1,
				access_count: 0,
				source_conversations: [`conv_${i}`],
				injection_tier: 3,
				salience_score: 0.2,
				archived: false,
				revision: 0,
				consolidation_notes: [],
			},
		}));
	}
	return ids;
}

const testEnv = {
	UPSTASH_REDIS_REST_URL: "https://redis.test.local",
	UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
	UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
	UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
	OPENAI_API_KEY: "test-openai-key",
};

describe("runDreamProposal entry-load scoping (production hang regression, 2026-07-11)", () => {
	let ids: string[];

	beforeEach(() => {
		mockState.store.clear();
		mockState.lists.clear();
		mockState.sets.clear();
		mockState.mgetCallSizes.length = 0;
		mockState.mgetKeys.length = 0;
		mockState.scanCalls = 0;
		mockState.scanPatterns.length = 0;
		mockState.store.set("migration:backfill_complete", "2026-03-27T05:29:20+00:00");
		ids = seedCorpus();
	});

	it("a targeted proposal loads ONLY its candidates, not the whole corpus", async () => {
		const { runDreamProposal } = await import("../src/dream");
		const candidates = [ids[3], ids[7]];

		await runDreamProposal(testEnv as never, {
			trigger: "manual",
			actorId: "test-operator",
			candidateIds: candidates,
			archiveLimit: 0,
			promotionLimit: 0,
		});

		// The whole point: no single Redis read may pull the entire corpus.
		// Pre-fix this mget'd all 60 entry keys (and 60 access + 60 last-accessed
		// keys); on the real corpus that is ~12k keys, three times over.
		const entryKeyReads = mockState.mgetKeys.filter((keys) =>
			keys.some((k) => k.startsWith("knowledge:ke_scope")),
		);
		for (const keys of entryKeyReads) {
			expect(keys.length).toBeLessThanOrEqual(candidates.length);
		}
		const maxRead = Math.max(0, ...mockState.mgetCallSizes);
		expect(maxRead).toBeLessThan(CORPUS_SIZE);
	});

	it("a targeted proposal does not key-scan the corpus at all", async () => {
		const { runDreamProposal } = await import("../src/dream");

		await runDreamProposal(testEnv as never, {
			trigger: "manual",
			actorId: "test-operator",
			candidateIds: [ids[1]],
			archiveLimit: 0,
			promotionLimit: 0,
		});

		// The candidate ids ARE the key list — scanning `knowledge:*` / `project:*`
		// is pure waste. (Small unrelated scans, e.g. correction-contest hint keys,
		// are fine and are NOT what hung production — only the corpus-wide entry
		// scan is. Assert precisely that, rather than "no scans at all".)
		const corpusScans = mockState.scanPatterns.filter(
			(m) => m === "knowledge:*" || m === "project:*",
		);
		expect(corpusScans).toEqual([]);
	});

	it("an UNFILTERED proposal still loads the full corpus (no behaviour change for the nightly)", async () => {
		const { runDreamProposal } = await import("../src/dream");

		const proposal = await runDreamProposal(testEnv as never, {
			trigger: "manual",
			actorId: "test-operator",
			archiveLimit: 0,
			promotionLimit: 0,
		});

		// The nightly's unfiltered path is the one that must stay identical: it
		// genuinely needs every entry, and its corpus-wide counts must stay honest.
		const snapshot = (proposal as Record<string, unknown>).counts as Record<string, unknown> | undefined;
		const corpusScans = mockState.scanPatterns.filter(
			(m) => m === "knowledge:*" || m === "project:*",
		);
		expect(corpusScans.length).toBeGreaterThan(0);
		if (snapshot && typeof snapshot.total_entries === "number") {
			expect(snapshot.total_entries).toBe(CORPUS_SIZE);
		}
	});
});
