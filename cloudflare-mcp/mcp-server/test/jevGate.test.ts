import { describe, expect, it } from "vitest";

import { applyJevGate, buildJevRequest, jevGateFromEnv } from "../src/jevGate";
import { sourceFirstSearch, type SourceFirstEvidence } from "../src/sourceFirst";

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

function jevResponse(answers: Record<string, number>, model = "jev-1.13.0"): typeof fetch {
	return (async () => new Response(JSON.stringify({
		model,
		answers: Object.fromEntries(Object.entries(answers).map(([key, value]) => [key, { type: "noul", noul: value }])),
		usage: { input_tokens: 10, output_tokens: 0 },
	}), { status: 200, headers: { "Content-Type": "application/json" } })) as unknown as typeof fetch;
}

function result(overrides: Partial<{ id: string; text: string; exact_identifier_match: boolean; exact_lexical_match: boolean; explicit_project_match: boolean }> = {}) {
	return { id: "r1", text: "some passage", exact_identifier_match: false, exact_lexical_match: false, explicit_project_match: false, ...overrides };
}

describe("jev gate config", () => {
	it("is off without a mode or a key", () => {
		expect(jevGateFromEnv({})).toBeUndefined();
		expect(jevGateFromEnv({ JEV_ABSTENTION_MODE: "on" })).toBeUndefined();
		expect(jevGateFromEnv({ TYPESAFE_API_KEY: "k" })).toBeUndefined();
		expect(jevGateFromEnv({ TYPESAFE_API_KEY: "k", JEV_ABSTENTION_MODE: "off" })).toBeUndefined();
	});

	it("reads mode and threshold, defaulting a bad threshold", () => {
		expect(jevGateFromEnv({ TYPESAFE_API_KEY: "k", JEV_ABSTENTION_MODE: "shadow", JEV_ABSTENTION_THRESHOLD: "0.6" }))
			.toEqual({ apiKey: "k", mode: "shadow", threshold: 0.6 });
		expect(jevGateFromEnv({ TYPESAFE_API_KEY: "k", JEV_ABSTENTION_MODE: "on", JEV_ABSTENTION_THRESHOLD: "nope" })?.threshold)
			.toBe(0.5);
	});

	it("asks one whole-set and two per-passage questions with the query as state", () => {
		const request = buildJevRequest("what is X", [{ id: "a", text: "A" }, { id: "b", text: "B" }], "jev-latest") as any;
		expect(Object.keys(request.questions)).toEqual(["answerable", "p0_evidence", "p0_relevant", "p1_evidence", "p1_relevant"]);
		expect(request.state.query).toBe("what is X");
		expect(request.state.passages.map((p: any) => p.id)).toEqual(["p0", "p1"]);
	});
});

describe("jev gate decisions", () => {
	const config = { apiKey: "k", mode: "on" as const, threshold: 0.5 };

	it("abstains when no passage clears the threshold", async () => {
		const results = [result({ id: "r1" }), result({ id: "r2" })];
		const gated = await applyJevGate("q", results, { ...config, fetch: jevResponse({ answerable: 0.1, p0_evidence: 0.2, p0_relevant: 0.6, p1_evidence: 0.3, p1_relevant: 0.5 }) });
		expect(gated.abstain).toBe(true);
		expect(gated.results).toEqual([]);
		expect(gated.report.status).toBe("ok");
		expect(gated.report.max_evidence).toBe(0.3);
		expect(gated.report.would_abstain).toBe(true);
		expect(results[0].jev_evidence).toBe(0.2);
		expect(results[1].jev_relevant).toBe(0.5);
	});

	it("keeps results, order and count when one passage clears the threshold", async () => {
		const results = [result({ id: "r1" }), result({ id: "r2" })];
		const gated = await applyJevGate("q", results, { ...config, fetch: jevResponse({ answerable: 0.7, p0_evidence: 0.2, p0_relevant: 0.6, p1_evidence: 0.8, p1_relevant: 0.9 }) });
		expect(gated.abstain).toBe(false);
		expect(gated.results.map((r) => r.id)).toEqual(["r1", "r2"]);
		expect(gated.report.max_evidence).toBe(0.8);
	});

	it("only annotates in shadow mode", async () => {
		const results = [result()];
		const gated = await applyJevGate("q", results, { ...config, mode: "shadow", fetch: jevResponse({ answerable: 0.1, p0_evidence: 0.1, p0_relevant: 0.1 }) });
		expect(gated.abstain).toBe(false);
		expect(gated.results).toHaveLength(1);
		expect(gated.report.would_abstain).toBe(true);
		expect(results[0].jev_evidence).toBe(0.1);
	});

	it("never gates deterministic recovery (identifier, project, exact phrase)", async () => {
		for (const flag of ["exact_identifier_match", "exact_lexical_match", "explicit_project_match"] as const) {
			const results = [result({ [flag]: true })];
			const gated = await applyJevGate("q", results, { ...config, fetch: jevResponse({ answerable: 0.0, p0_evidence: 0.0, p0_relevant: 0.0 }) });
			expect(gated.abstain).toBe(false);
			expect(gated.results).toHaveLength(1);
			expect(gated.report.protected_results).toBe(1);
			expect(gated.report.would_abstain).toBe(false);
		}
	});

	it("fails open on an HTTP error", async () => {
		const results = [result()];
		const failing = (async () => new Response("overloaded", { status: 529 })) as unknown as typeof fetch;
		const gated = await applyJevGate("q", results, { ...config, fetch: failing });
		expect(gated.abstain).toBe(false);
		expect(gated.results).toHaveLength(1);
		expect(gated.report.status).toBe("unavailable");
		expect(gated.report.error).toBe("http_529");
	});

	it("fails open on a timeout", async () => {
		const results = [result()];
		const hanging = ((_url: string, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
			init.signal?.addEventListener("abort", () => {
				const error = new Error("aborted");
				error.name = "AbortError";
				reject(error);
			});
		})) as unknown as typeof fetch;
		const gated = await applyJevGate("q", results, { ...config, timeoutMs: 20, fetch: hanging });
		expect(gated.abstain).toBe(false);
		expect(gated.results).toHaveLength(1);
		expect(gated.report.error).toBe("timeout");
	});

	it("fails open on a malformed reply", async () => {
		const results = [result()];
		const junk = (async () => new Response(JSON.stringify({ nope: 1 }), { status: 200 })) as unknown as typeof fetch;
		const gated = await applyJevGate("q", results, { ...config, fetch: junk });
		expect(gated.abstain).toBe(false);
		expect(gated.report.error).toBe("malformed_response");
	});
});

describe("jev gate inside source-first search", () => {
	function fixture() {
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
		const vector = { query: async () => [{ id: "ev_tracker", score: 0.9 }] };
		return { redis: redis as any, vector: vector as any };
	}

	it("is byte-identical to today when no gate is configured", async () => {
		const { redis, vector } = fixture();
		const plain = await sourceFirstSearch(redis, vector, [0.1], "how are the daily signals produced", 5);
		expect(plain.abstained).toBe(false);
		expect(plain.jev).toBeNull();
		expect((plain.results as any[])[0]).not.toHaveProperty("jev_evidence");
	});

	it("adds a jev abstention after the floor and reports the reason", async () => {
		const { redis, vector } = fixture();
		const gated = await sourceFirstSearch(redis, vector, [0.1], "how are the daily signals produced", 5, {
			jev: { apiKey: "k", mode: "on", threshold: 0.5, fetch: jevResponse({ answerable: 0.05, p0_evidence: 0.1, p0_relevant: 0.7 }) },
		});
		expect(gated.abstained).toBe(true);
		expect(gated.abstain_reason).toBe("jev_no_answer_evidence");
		expect(gated.results).toEqual([]);
		expect((gated.jev as any).max_evidence).toBe(0.1);
	});

	it("does not call Jev at all when the floor already abstained", async () => {
		const { redis, vector } = fixture();
		let calls = 0;
		const counting = (async () => { calls += 1; return new Response("{}", { status: 200 }); }) as unknown as typeof fetch;
		const vectorLow = { query: async () => [{ id: "ev_tracker", score: 0.5 }] };
		const out = await sourceFirstSearch(redis, vectorLow as any, [0.1], "sourdough fermentation recipe", 5, {
			jev: { apiKey: "k", mode: "on", threshold: 0.5, fetch: counting },
		});
		expect(out.abstained).toBe(true);
		expect(out.abstain_reason).toBe("no_relevant_evidence_above_threshold");
		expect(calls).toBe(0);
	});
});
