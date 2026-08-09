import { describe, expect, it } from "vitest";

import {
	getSourceFirstIndex,
	findExplicitProject,
	isSuppressed,
	lexicalOverlap,
	scoreSourceFirstResult,
	sourceRecencyScore,
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
	it("detects an explicitly named project using phrase boundaries", () => {
		const projects = [{ id: "p_tracker", name: "Tracker" }, { id: "p_track", name: "Track" }];
		expect(findExplicitProject("What is the current Tracker architecture?", projects)?.id).toBe("p_tracker");
		expect(findExplicitProject("Show attraction research", projects)).toBeNull();
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
