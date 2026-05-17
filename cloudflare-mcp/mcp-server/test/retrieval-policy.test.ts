// Tests for Layer 0 retrieval policy: query classification, entry
// classification, cross-context penalty, quarantine penalty, and the
// env-var kill switch.

import { describe, expect, it } from "vitest";

import {
	CONFIDENCE_THRESHOLD,
	CROSS_BUCKET_PENALTY,
	QUARANTINE_PENALTY,
	classifyEntryTopic,
	classifyQueryIntent,
	computeCrossContextPenalty,
	computeQuarantinePenalty,
} from "../src/retrievalPolicy";

describe("classifyQueryIntent", () => {
	it("recognizes a clear coding query", () => {
		const intent = classifyQueryIntent(
			"how do I deploy a Cloudflare Worker with TypeScript",
		);
		expect(intent.bucket).toBe("coding_dev");
		expect(intent.confidence).toBeGreaterThan(0);
	});

	it("recognizes a clear quant/finance query", () => {
		const intent = classifyQueryIntent(
			"country equity rotation backtest with Sharpe ratio",
		);
		expect(intent.bucket).toBe("finance_quant");
		expect(intent.confidence).toBeGreaterThanOrEqual(CONFIDENCE_THRESHOLD);
	});

	it("recognizes a clear personal-health query", () => {
		const intent = classifyQueryIntent(
			"what is panchakarma therapy in ayurveda",
		);
		expect(intent.bucket).toBe("personal_health");
		expect(intent.confidence).toBeGreaterThanOrEqual(CONFIDENCE_THRESHOLD);
	});

	it("returns general with zero confidence for empty query", () => {
		const intent = classifyQueryIntent("");
		expect(intent.bucket).toBe("general");
		expect(intent.confidence).toBe(0);
	});

	it("returns general with zero confidence for non-keyword query", () => {
		const intent = classifyQueryIntent("what is the meaning of life");
		expect(intent.bucket).toBe("general");
		expect(intent.confidence).toBe(0);
	});
});

describe("classifyEntryTopic", () => {
	it("classifies a coding entry from claude_code source", () => {
		const bucket = classifyEntryTopic({
			label: "Loop Pilot runner state management",
			source: "claude_code",
			project: "loop-pilot",
		});
		expect(bucket).toBe("coding_dev");
	});

	it("classifies a finance entry from domain text", () => {
		const bucket = classifyEntryTopic({
			label: "Country equity rotation signal smoothing",
			currentView: "Factor timing model with information coefficient evaluation.",
		});
		expect(bucket).toBe("finance_quant");
	});

	it("classifies a health entry", () => {
		const bucket = classifyEntryTopic({
			label: "Panchakarma therapy questions",
			currentView: "Ayurvedic medicine, supplements, and wellness.",
		});
		expect(bucket).toBe("personal_health");
	});

	it("falls back to general when nothing matches", () => {
		const bucket = classifyEntryTopic({
			label: "Random musing",
			currentView: "Just thinking about stuff.",
		});
		expect(bucket).toBe("general");
	});
});

describe("computeCrossContextPenalty", () => {
	const codingQuery = classifyQueryIntent(
		"how do I deploy a Cloudflare Worker with TypeScript and a python script",
	);

	it("returns 1.0 when mode is off, regardless of mismatch", () => {
		const result = computeCrossContextPenalty({
			mode: "off",
			queryIntent: codingQuery,
			entryBucket: "personal_health",
		});
		expect(result).toBe(1.0);
	});

	it("returns penalty when buckets mismatch confidently and mode is on", () => {
		const result = computeCrossContextPenalty({
			mode: "on",
			queryIntent: codingQuery,
			entryBucket: "personal_health",
		});
		expect(result).toBe(CROSS_BUCKET_PENALTY);
	});

	it("returns 1.0 when buckets match", () => {
		const result = computeCrossContextPenalty({
			mode: "on",
			queryIntent: codingQuery,
			entryBucket: "coding_dev",
		});
		expect(result).toBe(1.0);
	});

	it("returns 1.0 when confidence is below threshold", () => {
		const ambiguous = { bucket: "coding_dev" as const, confidence: 0.5, matchedKeywords: [] };
		const result = computeCrossContextPenalty({
			mode: "on",
			queryIntent: ambiguous,
			entryBucket: "personal_health",
		});
		expect(result).toBe(1.0);
	});

	it("returns 1.0 when either side is general (no opinion)", () => {
		const generalQuery = { bucket: "general" as const, confidence: 0.9, matchedKeywords: [] };
		const r1 = computeCrossContextPenalty({
			mode: "on",
			queryIntent: generalQuery,
			entryBucket: "personal_health",
		});
		expect(r1).toBe(1.0);

		const r2 = computeCrossContextPenalty({
			mode: "on",
			queryIntent: codingQuery,
			entryBucket: "general",
		});
		expect(r2).toBe(1.0);
	});
});

describe("computeQuarantinePenalty", () => {
	it("returns 1.0 when mode is off", () => {
		expect(
			computeQuarantinePenalty({ mode: "off", isQuarantined: true }),
		).toBe(1.0);
	});

	it("returns 1.0 when entry is not quarantined", () => {
		expect(
			computeQuarantinePenalty({ mode: "on", isQuarantined: false }),
		).toBe(1.0);
	});

	it("returns penalty when entry is quarantined and mode is on", () => {
		expect(
			computeQuarantinePenalty({ mode: "on", isQuarantined: true }),
		).toBe(QUARANTINE_PENALTY);
	});
});

describe("Panchakarma scenario integration", () => {
	it("suppresses a panchakarma entry when query is about country rotation", () => {
		const query = "country equity rotation backtest with factor signals";
		const intent = classifyQueryIntent(query);
		expect(intent.bucket).toBe("finance_quant");
		expect(intent.confidence).toBeGreaterThanOrEqual(CONFIDENCE_THRESHOLD);

		const panchakarmaBucket = classifyEntryTopic({
			label: "Panchakarma therapy notes",
			currentView: "Ayurvedic detox and wellness considerations.",
		});
		expect(panchakarmaBucket).toBe("personal_health");

		const multiplier = computeCrossContextPenalty({
			mode: "on",
			queryIntent: intent,
			entryBucket: panchakarmaBucket,
		});
		expect(multiplier).toBe(CROSS_BUCKET_PENALTY);
	});

	it("does NOT suppress a panchakarma entry when policy is off", () => {
		const intent = classifyQueryIntent("country equity rotation backtest");
		const multiplier = computeCrossContextPenalty({
			mode: "off",
			queryIntent: intent,
			entryBucket: "personal_health",
		});
		expect(multiplier).toBe(1.0);
	});
});
