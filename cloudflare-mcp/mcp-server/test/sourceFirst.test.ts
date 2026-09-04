import { describe, expect, it } from "vitest";

import {
	getSourceFirstIndex,
	getSourceFirstEvidence,
	getSourceFirstOperationalStatus,
	findExplicitProject,
	evaluateSourceFirstFreshness,
	isSuppressed,
	lexicalOverlap,
	scoreSourceFirstResult,
	sourceFirstSearch,
	sourceRecencyScore,
	workingContextAttentionScore,
	type SourceFirstEvidence,
} from "../src/sourceFirst";

const evidence: SourceFirstEvidence = {
	id: "ev_tracker",
	title: "Tracker current architecture",
	text: "Tracker produces portfolio signals from current market data.",
	source_path: "/projects/Tracker/README.md",
	source_kind: "working_project",
	project: "Tracker",
	source_modified_at: "2026-08-01T00:00:00.000Z",
	content_checksum: "abc",
	chunk_index: 0,
	chunk_count: 1,
	authority: 0.9,
	pinned: false,
};

describe("source-first scoring", () => {
	it("uses transparent fixed components without salience or access signals", () => {
		const result = scoreSourceFirstResult(
			"Tracker portfolio signals",
			evidence,
			0.9,
			new Date("2026-08-08T00:00:00.000Z"),
		);
		expect(result.lexical_score).toBe(1);
		expect(result.recency_score).toBeGreaterThan(0.9);
		expect(result.final_score).toBeGreaterThan(0.9);
		expect(result).not.toHaveProperty("salience_score");
		expect(result).not.toHaveProperty("injection_tier");
		expect(result).not.toHaveProperty("access_count");
	});

	it("makes recency monotonic and source-based", () => {
		const now = new Date("2026-08-08T00:00:00.000Z");
		expect(sourceRecencyScore("2026-08-01T00:00:00.000Z", now))
			.toBeGreaterThan(sourceRecencyScore("2025-08-01T00:00:00.000Z", now));
	});

	it("gives recent relevant working context a transparent decaying lift", () => {
		const session = {
			...evidence,
			evidence_role: "working_context" as const,
			session_surface: "codex" as const,
			attention_observed_at: "2026-08-10T00:00:00.000Z",
		};
		const now = new Date("2026-08-10T00:00:00.000Z");
		const fresh = scoreSourceFirstResult("Tracker portfolio signals", session, 0.8, now);
		const authoritative = scoreSourceFirstResult("Tracker portfolio signals", evidence, 0.8, now);
		expect(fresh.attention_score).toBe(0.8);
		expect(fresh.working_context_bonus).toBe(0.064);
		expect(fresh.final_score).toBeGreaterThan(authoritative.final_score);
		expect(workingContextAttentionScore(
			{ ...session, attention_observed_at: "2026-08-07T00:00:00.000Z" },
			0.8,
			now,
		)).toBeCloseTo(0.4, 6);
	});

	it("cannot boost unrelated recent context without semantic relevance", () => {
		const session = {
			...evidence,
			evidence_role: "working_context" as const,
			attention_observed_at: "2026-08-10T00:00:00.000Z",
		};
		expect(workingContextAttentionScore(session, 0, new Date("2026-08-10T00:00:00.000Z"))).toBe(0);
	});

	it("lexical overlap is query transparent", () => {
		expect(lexicalOverlap("Tracker signals", evidence)).toBe(1);
		expect(lexicalOverlap("California statute", evidence)).toBe(0);
	});

	it("does not let generic question boilerplate outrank an exact project identifier", () => {
		const unrelated = {
			...evidence,
			project: "Via Orchestrator",
			source_path: "/projects/Via Orchestrator/README.md",
			title: "Current project architecture",
			text: "A production-ready orchestration system.",
		};
		expect(lexicalOverlap("What is the current Tracker project architecture?", evidence)).toBe(1);
		expect(lexicalOverlap("What is the current Tracker project architecture?", unrelated)).toBe(0);
	});

	it("does not treat a generic one-word project as an explicit identifier", () => {
		const research = { id: "p_research", name: "Research" };
		expect(findExplicitProject("which database file holds the macro research data", [research])).toBeNull();
	});
});

describe("explicit suppression rules", () => {
	const loopPilot = { ...evidence, title: "LoopPilot", project: "Loop Pilot", source_path: "/projects/Loop Pilot/README.md" };
	const rules = [{
		id: "s1",
		terms: ["looppilot", "loop pilot"],
		source_path_contains: ["/Loop Pilot/"],
		allow_explicit_query: true,
	}];

	it("suppresses an unwanted topic from unrelated retrieval", () => {
		expect(isSuppressed("quant research methodology", loopPilot, rules)).toBe(true);
	});

	it("still permits an explicit historical lookup", () => {
		expect(isSuppressed("what happened with LoopPilot?", loopPilot, rules)).toBe(false);
	});
});

describe("source-first project index", () => {
	it("abstains when no evidence clears the relevance floor", async () => {
		const values = new Map<string, unknown>([
			["sf:current_generation", "sf_test"],
			["sf:sf_test:suppressions", JSON.stringify({ rules: [] })],
			["sf:sf_test:projects", JSON.stringify([])],
			["sf:sf_test:evidence:ev_tracker", JSON.stringify(evidence)],
		]);
		const redis = {
			get: async (key: string) => values.get(key) ?? null,
			mget: async (...keys: string[]) => keys.map((key) => values.get(key) ?? null),
		};
		const vector = { query: async () => [{ id: "ev_tracker", score: 0.55 }] };
		const result = await sourceFirstSearch(redis as any, vector as any, [0.1], "sourdough fermentation recipe", 5);
		expect(result.abstained).toBe(true);
		expect(result.results).toEqual([]);
	});

	it("does not mistake ordinary sentence capitalization for an exact identifier", async () => {
		const franceEvidence = {
			...evidence,
			id: "ev_france",
			text: "A project report briefly mentions France.",
		};
		const values = new Map<string, unknown>([
			["sf:current_generation", "sf_test"],
			["sf:sf_test:suppressions", JSON.stringify({ rules: [] })],
			["sf:sf_test:projects", JSON.stringify([])],
			["sf:sf_test:evidence:ev_france", JSON.stringify(franceEvidence)],
		]);
		const redis = {
			get: async (key: string) => values.get(key) ?? null,
			mget: async (...keys: string[]) => keys.map((key) => values.get(key) ?? null),
		};
		const vector = { query: async () => [{ id: "ev_france", score: 0.2 }] };
		const result = await sourceFirstSearch(redis as any, vector as any, [0.1], "What is the capital of France?", 5);
		expect(result.abstained).toBe(true);
		expect(result.results).toEqual([]);
	});

	it("does not treat a plain quantity as an exact source identifier", async () => {
		const numericEvidence = { ...evidence, id: "ev_numeric", text: "A report contains the value 100." };
		const values = new Map<string, unknown>([
			["sf:current_generation", "sf_test"],
			["sf:sf_test:suppressions", JSON.stringify({ rules: [] })],
			["sf:sf_test:projects", JSON.stringify([])],
			["sf:sf_test:evidence:ev_numeric", JSON.stringify(numericEvidence)],
		]);
		const redis = {
			get: async (key: string) => values.get(key) ?? null,
			mget: async (...keys: string[]) => keys.map((key) => values.get(key) ?? null),
		};
		const vector = { query: async () => [{ id: "ev_numeric", score: 0.5 }] };
		const result = await sourceFirstSearch(redis as any, vector as any, [0.1], "convert 100 fahrenheit to celsius", 5);
		expect(result.abstained).toBe(true);
	});

	it("collapses byte-identical evidence and retains alternate provenance", async () => {
		const duplicate = {
			...evidence,
			id: "ev_duplicate",
			project: "Tracker-worktree",
			source_path: "/projects/Tracker-worktree/README.md",
		};
		const values = new Map<string, unknown>([
			["sf:current_generation", "sf_test"],
			["sf:sf_test:suppressions", JSON.stringify({ rules: [] })],
			["sf:sf_test:projects", JSON.stringify([])],
			["sf:sf_test:evidence:ev_tracker", JSON.stringify(evidence)],
			["sf:sf_test:evidence:ev_duplicate", JSON.stringify(duplicate)],
		]);
		const redis = {
			get: async (key: string) => values.get(key) ?? null,
			mget: async (...keys: string[]) => keys.map((key) => values.get(key) ?? null),
		};
		const vector = { query: async () => [
			{ id: "ev_tracker", score: 0.9 },
			{ id: "ev_duplicate", score: 0.89 },
		] };
		const result = await sourceFirstSearch(redis as any, vector as any, [0.1], "Tracker portfolio signals", 5);
		const results = result.results as Array<Record<string, unknown>>;
		expect(results).toHaveLength(1);
		expect(results[0]?.duplicate_count).toBe(2);
		expect(results[0]?.alternate_sources).toEqual([
			expect.objectContaining({ source_path: "/projects/Tracker-worktree/README.md" }),
		]);
	});

	it("returns every sibling chunk for get_deep", async () => {
		const first = { ...evidence, source_id: "src_tracker", chunk_index: 0, chunk_count: 2 };
		const second = { ...first, id: "ev_tracker_2", chunk_index: 1, text: "Second source chunk." };
		const values = new Map<string, unknown>([
			["sf:current_generation", "sf_test"],
			["sf:sf_test:evidence:ev_tracker", JSON.stringify(first)],
			["sf:sf_test:evidence:ev_tracker_2", JSON.stringify(second)],
			["sf:sf_test:source_evidence:src_tracker", JSON.stringify(["ev_tracker", "ev_tracker_2"])],
		]);
		const redis = {
			get: async (key: string) => values.get(key) ?? null,
			mget: async (...keys: string[]) => keys.map((key) => values.get(key) ?? null),
		};
		const result = await getSourceFirstEvidence(redis as any, "ev_tracker");
		expect(result.chunk_count).toBe(2);
		expect(result.complete_source).toBe(true);
		expect((result.chunks as SourceFirstEvidence[]).map((chunk) => chunk.chunk_index)).toEqual([0, 1]);
	});

	it("reports active source-first validation without inheriting Dream status", async () => {
		const values = new Map<string, unknown>([
			["sf:current_generation", "sf_test"],
			["sf:manifest:sf_test", JSON.stringify({
				generation: "sf_test",
				built_at: "2026-08-08T11:55:00.000Z",
				published_at: "2026-08-08T11:55:00.000Z",
				evidence_count: 10,
			})],
			["sf:heartbeat", JSON.stringify({ generation: "sf_test", published_at: "2026-08-08T11:55:00.000Z" })],
		]);
		const redis = { get: async (key: string) => values.get(key) ?? null };
		const result = await getSourceFirstOperationalStatus(redis as any, 36 * 60 * 60);
		expect(result.mode).toBe("source_first");
		expect((result.gates as any).legacy_dream.status).toBe("retired");
	});

	it("adds exact identifier candidates that vector search misses", async () => {
		const exact: SourceFirstEvidence = {
			...evidence,
			id: "ev_exact",
			title: "1MTR factor signal",
			text: "The 1MTR signal is an exact source identifier.",
		};
		const semantic: SourceFirstEvidence = {
			...evidence,
			id: "ev_semantic",
			title: "Generic factor research",
			text: "Broad factor research without the opaque identifier.",
		};
		const values = new Map<string, unknown>([
			["sf:current_generation", "sf_test"],
			["sf:sf_test:suppressions", JSON.stringify({ rules: [] })],
			["sf:sf_test:projects", JSON.stringify([])],
			["sf:sf_test:lex:1mtr", JSON.stringify(["ev_exact"])],
			["sf:sf_test:evidence:ev_exact", JSON.stringify(exact)],
			["sf:sf_test:evidence:ev_semantic", JSON.stringify(semantic)],
		]);
		const redis = {
			get: async (key: string) => values.get(key) ?? null,
			mget: async (...keys: string[]) => keys.map((key) => values.get(key) ?? null),
		};
		const vector = { query: async () => [{ id: "ev_semantic", score: 0.99 }] };
		const result = await sourceFirstSearch(redis as any, vector as any, [0.1], "1MTR signal", 5);
		expect((result.results as Array<Record<string, unknown>>)[0]?.id).toBe("ev_exact");
		expect((result.results as Array<Record<string, unknown>>)[0]?.exact_identifier_match).toBe(true);
	});

	// Regression 2026-09-04: recovered candidates were scored with similarity 0
	// because they came from the Redis lexical/project maps, not the vector
	// query. Two chunks recovered for "Corwin-Schultz spread estimator" led at
	// final_score 0.296 over a 0.795 semantic hit. The ranker now fetches the
	// stored vectors for the best-placed recovered candidates and scores them
	// with their real cosine similarity.
	it("scores recovered candidates with their real vector similarity", async () => {
		const relevant: SourceFirstEvidence = {
			...evidence,
			id: "ev_relevant_recovered",
			content_checksum: "checksum-relevant",
			title: "Corwin-Schultz verdict",
			text: "Corwin-Schultz spread estimator tested on the CNN pattern universe; rejected as a filter.",
		};
		const unrelated: SourceFirstEvidence = {
			...evidence,
			id: "ev_unrelated_recovered",
			content_checksum: "checksum-unrelated",
			title: "Corwin-Schultz mention",
			text: "Corwin-Schultz appears once in a long list of estimator names in an unrelated agenda.",
		};
		const values = new Map<string, unknown>([
			["sf:current_generation", "sf_test"],
			["sf:sf_test:suppressions", JSON.stringify({ rules: [] })],
			["sf:sf_test:projects", JSON.stringify([])],
			["sf:sf_test:lex:corwin", JSON.stringify(["ev_unrelated_recovered", "ev_relevant_recovered"])],
			["sf:sf_test:lex:schultz", JSON.stringify(["ev_unrelated_recovered", "ev_relevant_recovered"])],
			["sf:sf_test:evidence:ev_relevant_recovered", JSON.stringify(relevant)],
			["sf:sf_test:evidence:ev_unrelated_recovered", JSON.stringify(unrelated)],
		]);
		const redis = {
			get: async (key: string) => values.get(key) ?? null,
			mget: async (...keys: string[]) => keys.map((key) => values.get(key) ?? null),
		};
		const fetchedIds: string[][] = [];
		const vector = {
			query: async () => [],
			namespace: () => ({
				fetch: async (ids: string[]) => {
					fetchedIds.push(ids);
					return ids.map((id) => ({ id, vector: id === "ev_relevant_recovered" ? [1, 0] : [0.05, 1] }));
				},
			}),
		};
		const result = await sourceFirstSearch(redis as any, vector as any, [1, 0], "Corwin-Schultz spread estimator", 5);
		const rows = result.results as Array<Record<string, unknown>>;
		expect(fetchedIds.length).toBe(1);
		expect(rows[0]?.id).toBe("ev_relevant_recovered");
		expect(rows[0]?.similarity_source).toBe("vector_fetch");
		expect(Number(rows[0]?.similarity_score)).toBeGreaterThan(0.9);
		expect(Number(rows[1]?.similarity_score)).toBeLessThan(0.1);
		expect(Number(rows[0]?.final_score)).toBeGreaterThan(Number(rows[1]?.final_score));
	});

	it("leaves recovered candidates unscored when the vector client cannot fetch", async () => {
		const exact: SourceFirstEvidence = { ...evidence, id: "ev_exact", title: "1MTR factor signal", text: "The 1MTR signal is an exact source identifier." };
		const values = new Map<string, unknown>([
			["sf:current_generation", "sf_test"],
			["sf:sf_test:suppressions", JSON.stringify({ rules: [] })],
			["sf:sf_test:projects", JSON.stringify([])],
			["sf:sf_test:lex:1mtr", JSON.stringify(["ev_exact"])],
			["sf:sf_test:evidence:ev_exact", JSON.stringify(exact)],
		]);
		const redis = {
			get: async (key: string) => values.get(key) ?? null,
			mget: async (...keys: string[]) => keys.map((key) => values.get(key) ?? null),
		};
		const vector = { query: async () => [] };
		const result = await sourceFirstSearch(redis as any, vector as any, [0.1], "1MTR signal", 5);
		const rows = result.results as Array<Record<string, unknown>>;
		expect(rows[0]?.id).toBe("ev_exact");
		expect(rows[0]?.similarity_source).toBe("unscored");
	});

	// Regression: probe neg_quicksort, 2026-08-21. docs/source-first-memory.md
	// promises the working-context lift "cannot rescue unrelated session text
	// because it is multiplied by semantic relevance" — but multiplying only
	// bounds the SIZE of the lift, it does not stop the lift being what carries
	// a below-floor record over the line. An unrelated session scored
	// similarity 0.5846 / lexical 0.6667 -> base 0.6292 (below the 0.65 floor),
	// and a 0.0467 attention lift admitted it at 0.6759.
	it("does not let the working-context lift admit evidence that fails the floor", async () => {
		const now = new Date();
		const session: SourceFirstEvidence = {
			...evidence,
			id: "ev_recent_session",
			title: "Unrelated project — claude_code session",
			text: "explain once again in plain english what the strategy is and how it works day to day",
			source_kind: "claude_code_session",
			evidence_role: "working_context",
			source_modified_at: now.toISOString(),
			attention_observed_at: now.toISOString(),
			authority: 0.7,
		};
		const values = new Map<string, unknown>([
			["sf:current_generation", "sf_test"],
			["sf:sf_test:suppressions", JSON.stringify({ rules: [] })],
			["sf:sf_test:projects", JSON.stringify([])],
			["sf:sf_test:evidence:ev_recent_session", JSON.stringify(session)],
		]);
		const redis = {
			get: async (key: string) => values.get(key) ?? null,
			mget: async (...keys: string[]) => keys.map((key) => values.get(key) ?? null),
		};
		// Similarity high enough that base + lift would clear 0.65, but base alone does not.
		const vector = { query: async () => [{ id: "ev_recent_session", score: 0.58 }] };
		const result = await sourceFirstSearch(
			redis as any, vector as any, [0.1], "explain how quicksort works", 5,
		);
		expect(result.abstained).toBe(true);
		expect(result.results).toEqual([]);
	});

	// The lift must still REORDER what already qualifies — this is the whole
	// point of recent-session attention, and the fix above must not kill it.
	it("still lets the working-context lift reorder evidence that already clears the floor", () => {
		const now = new Date();
		const recent: SourceFirstEvidence = {
			...evidence,
			id: "ev_recent",
			evidence_role: "working_context",
			source_modified_at: now.toISOString(),
			attention_observed_at: now.toISOString(),
		};
		const scoredRecent = scoreSourceFirstResult("tracker project architecture", recent, 0.95);
		const scoredOld = scoreSourceFirstResult("tracker project architecture", evidence, 0.95);
		expect(scoredRecent.working_context_bonus).toBeGreaterThan(0);
		expect(scoredRecent.final_score).toBeGreaterThan(scoredOld.final_score);
		expect(scoredRecent.base_score).toBeGreaterThanOrEqual(0.65);
	});

	// Regression: BEAM run 20260821T0435Z_fix1. exact_identifier_count was the
	// PRIMARY sort key, so matching MORE distinct identifier terms beat being
	// more relevant. Observed: a chunk matching both REST and API led at
	// final_score 0.18 against 0.738. An identifier match must still lead over a
	// non-match (that is how 1MTR recovery works), but among chunks that all
	// matched something, relevance decides.
	it("orders results that all match an identifier by relevance, not match count", async () => {
		const twoTermsIrrelevant: SourceFirstEvidence = {
			...evidence,
			id: "ev_two_terms",
			title: "Unrelated aside",
			text: "A passing note about a REST endpoint and an API key rotation chore.",
		};
		const oneTermRelevant: SourceFirstEvidence = {
			...evidence,
			id: "ev_one_term",
			title: "REST error handling",
			text: "Typical REST errors to handle: timeouts, 429 rate limits, 500s, and malformed payloads.",
		};
		const values = new Map<string, unknown>([
			["sf:current_generation", "sf_test"],
			["sf:sf_test:suppressions", JSON.stringify({ rules: [] })],
			["sf:sf_test:projects", JSON.stringify([])],
			["sf:sf_test:lex:rest", JSON.stringify(["ev_two_terms", "ev_one_term"])],
			["sf:sf_test:lex:api", JSON.stringify(["ev_two_terms"])],
			["sf:sf_test:evidence:ev_two_terms", JSON.stringify(twoTermsIrrelevant)],
			["sf:sf_test:evidence:ev_one_term", JSON.stringify(oneTermRelevant)],
		]);
		const redis = {
			get: async (key: string) => values.get(key) ?? null,
			mget: async (...keys: string[]) => keys.map((key) => values.get(key) ?? null),
		};
		// The relevant chunk is also what vector search prefers.
		const vector = {
			query: async () => [{ id: "ev_one_term", score: 0.95 }, { id: "ev_two_terms", score: 0.2 }],
		};
		const result = await sourceFirstSearch(
			redis as any, vector as any, [0.1],
			"When building against a REST API, what errors should I handle?", 5,
		);
		const rows = result.results as Array<Record<string, unknown>>;
		expect(rows[0]?.id).toBe("ev_one_term");
		// The count is still recorded, it just no longer dictates the order.
		const twoTermRow = rows.find((row) => row.id === "ev_two_terms");
		if (twoTermRow) {
			expect(twoTermRow.exact_identifier_count as number)
				.toBeGreaterThanOrEqual(rows[0]?.exact_identifier_count as number);
		}
	});

	// Regression: BEAM baseline 2026-08-21 (beam-eval run 20260821T0415Z_baseline).
	// queryIdentifierTerms treated any ALL-CAPS word as an opaque identifier, so
	// "Mention ONLY and ALL of the concepts" produced identifier terms
	// [only, all, concepts]. Because exact_identifier_count is the primary sort
	// key AND bypasses the 0.65 floor, every chunk containing "only" outranked
	// genuinely relevant results — observed top-1 scoring 0.175 against a
	// runner-up at 0.755.
	it("does not treat an emphasized common word as an exact identifier", async () => {
		const relevant: SourceFirstEvidence = {
			...evidence,
			id: "ev_relevant",
			title: "Triangle geometry concepts",
			text: "We covered the triangle inequality, then similar triangles, then the law of cosines.",
		};
		const filler: SourceFirstEvidence = {
			...evidence,
			id: "ev_filler",
			title: "Unrelated scheduling note",
			text: "I only had time for a short session, and all of it went to logistics.",
		};
		const values = new Map<string, unknown>([
			["sf:current_generation", "sf_test"],
			["sf:sf_test:suppressions", JSON.stringify({ rules: [] })],
			["sf:sf_test:projects", JSON.stringify([])],
			["sf:sf_test:lex:only", JSON.stringify(["ev_filler"])],
			["sf:sf_test:lex:all", JSON.stringify(["ev_filler"])],
			["sf:sf_test:evidence:ev_relevant", JSON.stringify(relevant)],
			["sf:sf_test:evidence:ev_filler", JSON.stringify(filler)],
		]);
		const redis = {
			get: async (key: string) => values.get(key) ?? null,
			mget: async (...keys: string[]) => keys.map((key) => values.get(key) ?? null),
		};
		const vector = { query: async () => [{ id: "ev_relevant", score: 0.82 }] };
		const result = await sourceFirstSearch(
			redis as any, vector as any, [0.1],
			"List the triangle geometry concepts in order. Mention ONLY and ALL of the concepts.",
			5,
		);
		const rows = result.results as Array<Record<string, unknown>>;
		expect(rows[0]?.id).toBe("ev_relevant");
		expect(rows.find((row) => row.id === "ev_filler")?.exact_identifier_match ?? false).toBe(false);
	});

	// Regression: same baseline. A trailing sentence period made "concepts."
	// match the identifier-punctuation rule, so an ordinary noun ending a
	// sentence became an opaque identifier.
	it("does not treat a word ending a sentence as a punctuated identifier", async () => {
		const filler: SourceFirstEvidence = {
			...evidence,
			id: "ev_filler",
			title: "Unrelated note",
			text: "A stray mention of concepts with no bearing on the question.",
		};
		const values = new Map<string, unknown>([
			["sf:current_generation", "sf_test"],
			["sf:sf_test:suppressions", JSON.stringify({ rules: [] })],
			["sf:sf_test:projects", JSON.stringify([])],
			["sf:sf_test:lex:concepts", JSON.stringify(["ev_filler"])],
			["sf:sf_test:evidence:ev_filler", JSON.stringify(filler)],
		]);
		const redis = {
			get: async (key: string) => values.get(key) ?? null,
			mget: async (...keys: string[]) => keys.map((key) => values.get(key) ?? null),
		};
		const vector = { query: async () => [{ id: "ev_filler", score: 0.2 }] };
		const result = await sourceFirstSearch(
			redis as any, vector as any, [0.1], "Please summarize the concepts.", 5,
		);
		// Nothing clears the floor on relevance, and no identifier match may
		// smuggle the filler past it.
		expect(result.abstained).toBe(true);
	});

	it("recovers a strong ordinary lexical phrase that vector search misses", async () => {
		const exact: SourceFirstEvidence = {
			...evidence,
			id: "ev_dsr",
			title: "Multiple-testing controls",
			text: "Compute the Deflated Sharpe Ratio over every logged trial.",
		};
		const semantic: SourceFirstEvidence = {
			...evidence,
			id: "ev_plain_sharpe",
			title: "Backtest table",
			text: "The strategy has a plain Sharpe ratio of 1.2.",
		};
		const values = new Map<string, unknown>([
			["sf:current_generation", "sf_test"],
			["sf:sf_test:suppressions", JSON.stringify({ rules: [] })],
			["sf:sf_test:projects", JSON.stringify([])],
			["sf:sf_test:lex:deflated", JSON.stringify(["ev_dsr"])],
			["sf:sf_test:lex:sharpe", JSON.stringify(["ev_dsr", "ev_plain_sharpe"])],
			["sf:sf_test:lex:ratio", JSON.stringify(["ev_dsr", "ev_plain_sharpe"])],
			["sf:sf_test:evidence:ev_dsr", JSON.stringify(exact)],
			["sf:sf_test:evidence:ev_plain_sharpe", JSON.stringify(semantic)],
		]);
		const redis = {
			get: async (key: string) => values.get(key) ?? null,
			mget: async (...keys: string[]) => keys.map((key) => values.get(key) ?? null),
		};
		const vector = { query: async () => [{ id: "ev_plain_sharpe", score: 0.8 }] };
		const result = await sourceFirstSearch(redis as any, vector as any, [0.1], "deflated Sharpe ratio", 5);
		const results = result.results as Array<Record<string, unknown>>;
		expect(results[0]?.id).toBe("ev_dsr");
		expect(results[0]?.exact_lexical_match).toBe(true);
	});

	it("reports a stale or mismatched generation heartbeat instead of silently serving it as healthy", () => {
		const now = new Date("2026-08-08T12:00:00.000Z");
		expect(evaluateSourceFirstFreshness(
			{ generation: "sf_test", published_at: "2026-08-08T00:00:00.000Z" },
			{ generation: "sf_test", published_at: "2026-08-08T00:00:00.000Z" },
			now,
			3600,
		)).toMatchObject({ status: "stale", age_seconds: 43200 });
		expect(evaluateSourceFirstFreshness(
			{ generation: "sf_test", published_at: "2026-08-08T11:55:00.000Z" },
			{ generation: "sf_other", published_at: "2026-08-08T11:55:00.000Z" },
			now,
			3600,
		).status).toBe("stale");
	});

	it("detects an explicitly named project using phrase boundaries", () => {
		const projects = [{ id: "p_tracker", name: "Tracker" }, { id: "p_track", name: "Track" }];
		expect(findExplicitProject("What is the current Tracker architecture?", projects)?.id).toBe("p_tracker");
		expect(findExplicitProject("Show attraction research", projects)).toBeNull();
	});

	it("maps conservative project-name shorthand to the complete evidence set", () => {
		const projects = [{ id: "p_t2", name: "T2 MEGA FACTOR TIMING V2" }, { id: "p_law", name: "California Law Chatbot" }];
		expect(findExplicitProject("T2 factor timing pipeline", projects)?.id).toBe("p_t2");
		expect(findExplicitProject("law chatbot PDF chunking", projects)?.id).toBe("p_law");
	});

	it("does not force a generic law-chatbot project when an acronym names the source", () => {
		const projects = [{ id: "p_law", name: "California Law Chatbot" }];
		expect(findExplicitProject("CEB PDF chunking for the law chatbot", projects)).toBeNull();
	});

	it("orders active projects by real source activity before applying the display cap", async () => {
		const projects = Array.from({ length: 101 }, (_, index) => ({
			name: `Old ${index}`,
			status: "active",
			last_touched: "2026-01-01T00:00:00.000Z",
		}));
		projects.push({ name: "WSH", status: "active", last_touched: "2026-07-28T00:00:00.000Z" });
		const values = new Map<string, unknown>([
			["sf:current_generation", "sf_test"],
			["sf:manifest:sf_test", JSON.stringify({ built_at: "2026-08-08", evidence_count: 10, source_file_count: 4 })],
			["sf:sf_test:projects", JSON.stringify(projects)],
		]);
		const redis = { get: async (key: string) => values.get(key) ?? null };
		const index = await getSourceFirstIndex(redis as any);
		expect((index.projects as Array<Record<string, unknown>>)[0]?.name).toBe("WSH");
		expect((index.projects as Array<Record<string, unknown>>).some((project) => project.name === "WSH")).toBe(true);
	});
});
