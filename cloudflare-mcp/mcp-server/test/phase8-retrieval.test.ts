import { describe, expect, it } from "vitest";

import {
	classifyPhase8Query,
	scorePhase8Candidate,
} from "../src/phase8Retrieval";

const now = new Date("2026-06-05T00:00:00Z");

describe("classifyPhase8Query", () => {
	it("classifies current, evidence, point-in-time, and policy queries", () => {
		expect(classifyPhase8Query("what is the live MCP runtime path now?").intent).toBe("current_answer");
		expect(classifyPhase8Query("why did the live MCP runtime path change?").intent).toBe("evidence_history");
		expect(classifyPhase8Query("what was true as of 2026-04-01 for MCP runtime path?")).toMatchObject({
			intent: "point_in_time",
			as_of: "2026-04-01",
		});
		expect(classifyPhase8Query("what policy file defines compile latency?").intent).toBe("procedural_policy");
	});
});

describe("scorePhase8Candidate", () => {
	it("prefers current surfaces for current-answer queries", () => {
		const intent = classifyPhase8Query("what is the live MCP runtime path now?");
		const current = scorePhase8Candidate("what is the live MCP runtime path now?", intent, {
			now,
			vectorScore: 0.4,
			entryType: "knowledge",
			entry: {
				id: "ke_current",
				domain: "MCP runtime path",
				current_view: "The live MCP runtime path is cloudflare-mcp/mcp-server.",
				state: "active",
				metadata: {
					context_type: "active_project",
					source_conversations: ["conv_current"],
				},
			},
		});
		const stale = scorePhase8Candidate("what is the live MCP runtime path now?", intent, {
			now,
			vectorScore: 0.99,
			entryType: "knowledge",
			entry: {
				id: "ke_old",
				domain: "MCP runtime path",
				current_view: "The live MCP runtime path was knowledge-system/mcp-server before the Phase 7 path update.",
				state: "stale",
				metadata: {
					temporal_status: "historical",
					source_conversations: ["conv_old"],
				},
			},
		});

		expect(current.final_score).toBeGreaterThan(stale.final_score);
		expect(current.score_multiplier).toBeGreaterThan(stale.score_multiplier);
		expect(current.reasons).toContain("current_surface_preferred");
	});

	it("boosts evidence-backed entries for history queries", () => {
		const intent = classifyPhase8Query("why did the live MCP runtime path change?");
		const scored = scorePhase8Candidate("why did the live MCP runtime path change?", intent, {
			now,
			vectorScore: 0.7,
			entryType: "knowledge",
			entry: {
				id: "ke_history",
				domain: "MCP runtime path",
				current_view: "The runtime path changed from knowledge-system/mcp-server to cloudflare-mcp/mcp-server.",
				state: "contested",
				metadata: {
					source_conversations: ["conv_history"],
					source_messages: ["msg_history"],
				},
			},
		});

		expect(scored.final_score).toBeGreaterThan(0.7);
		expect(scored.reasons).toContain("evidence_source_preferred");
	});

	it("recognizes point-in-time validity windows", () => {
		const query = "what was true as of 2026-04-01 for the MCP runtime path?";
		const intent = classifyPhase8Query(query);
		const scored = scorePhase8Candidate(query, intent, {
			now,
			vectorScore: 0.7,
			entryType: "knowledge",
			entry: {
				id: "ke_old",
				domain: "MCP runtime path",
				current_view: "The live MCP runtime path was knowledge-system/mcp-server.",
				state: "stale",
				metadata: {
					valid_from: "2026-03-01",
					valid_to: "2026-04-30",
					source_messages: ["msg_old"],
				},
			},
		});

		expect(scored.temporal_score).toBe(1);
		expect(scored.reasons).toContain("point_in_time_temporal_fit");
	});

	it("boosts policy pointer style entries for procedural policy queries", () => {
		const query = "what policy file defines compile latency?";
		const intent = classifyPhase8Query(query);
		const scored = scorePhase8Candidate(query, intent, {
			now,
			vectorScore: 0.2,
			entryType: "project",
			entry: {
				id: "pe_policy",
				name: "Phase 7 policy pointers",
				goal: "Procedural and policy memory lives in AGENTS.md and docs/pks-phase-7a-compile-latency-policy-2026-06-05.md.",
				status: "active",
				metadata: {
					artifact_path: "docs/pks-phase-7a-compile-latency-policy-2026-06-05.md",
					context_type: "active_project",
				},
			},
		});

		expect(scored.lane_score).toBe(1);
		expect(scored.source_priority_score).toBe(1);
		expect(scored.reasons).toContain("policy_pointer_preferred");
	});

	it("penalizes expired temporal facts for current-answer queries", () => {
		const query = "what is the Singapore trip plan now?";
		const intent = classifyPhase8Query(query);
		const scored = scorePhase8Candidate(query, intent, {
			now,
			vectorScore: 0.9,
			entryType: "knowledge",
			entry: {
				id: "ke_trip",
				domain: "Operator travel",
				current_view: "The operator is going to Singapore in May 2026.",
				state: "active",
				metadata: {
					source_conversations: ["conv_trip"],
				},
			},
		});
		const current = scorePhase8Candidate(query, intent, {
			now,
			vectorScore: 0.9,
			entryType: "knowledge",
			entry: {
				id: "ke_trip_current",
				domain: "Operator travel",
				current_view: "The operator is going to Singapore in July 2026.",
				state: "active",
				metadata: {
					source_conversations: ["conv_trip"],
				},
			},
		});

		expect(scored.temporal_score).toBeLessThan(0.1);
		expect(scored.final_score).toBeLessThan(current.final_score);
		expect(scored.score_multiplier).toBeLessThan(current.score_multiplier);
	});
});
