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
