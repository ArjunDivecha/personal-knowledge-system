import fs from "node:fs";
import path from "node:path";
import process from "node:process";

import { Redis } from "@upstash/redis";
import { Index } from "@upstash/vector";
import OpenAI from "openai";

import { sourceFirstSearchGeneration } from "../src/sourceFirst.ts";

type Probe = {
	id: string;
	axis: string;
	enabled?: boolean;
	query: string;
	min_rank?: number;
	expect_entry_ids?: string[];
	expect_any_of?: string[];
	forbid_any_of?: string[];
};

type SearchResult = Record<string, unknown>;

function resultText(result: SearchResult): string {
	return ["label", "summary", "domain", "title", "text", "project", "source_path", "source_kind"]
		.map((key) => String(result[key] ?? ""))
		.join(" ")
		.toLowerCase();
}

function loadProbes(root: string): Probe[] {
	const directory = path.join(root, "tests", "probes");
	return fs.readdirSync(directory)
		.filter((name) => name.endsWith(".json"))
		.sort()
		.flatMap((name) => JSON.parse(fs.readFileSync(path.join(directory, name), "utf8")) as Probe[])
		.filter((probe) => probe.enabled === true);
}

function scoreProbe(probe: Probe, payload: Record<string, unknown>): Record<string, unknown> {
	const results = Array.isArray(payload.results) ? payload.results as SearchResult[] : [];
	const top = results.slice(0, probe.min_rank ?? 5);
	const topIds = top.map((result) => String(result.id ?? ""));
	const texts = top.map(resultText);
	const expectedIds = probe.expect_entry_ids ?? [];
	const expectedText = (probe.expect_any_of ?? []).map((value) => value.toLowerCase());
	const forbiddenText = (probe.forbid_any_of ?? []).map((value) => value.toLowerCase());
	const idHit = expectedIds.some((value) => topIds.includes(value));
	const textHit = expectedText.some((value) => texts.some((text) => text.includes(value)));
	const expectedFound = expectedIds.length > 0 || expectedText.length > 0 ? idHit || textHit : true;
	const leaks = forbiddenText.filter((value) => texts.some((text) => text.includes(value)));
	const passed = probe.axis === "negative"
		? payload.abstained === true && results.length === 0
		: probe.axis === "stale_fact"
			? leaks.length === 0
			: expectedFound && leaks.length === 0;
	return {
		id: probe.id,
		axis: probe.axis,
		passed,
		abstained: payload.abstained === true,
		top_ids: topIds,
		top_final_scores: top.map((result) => result.final_score ?? null),
		expected_found: expectedFound,
		leaks,
	};
}

async function main(): Promise<void> {
	const generation = process.argv[2];
	if (!generation) throw new Error("usage: evaluate-source-first-candidate.ts <generation> [output.json]");
	const outputPath = process.argv[3] ?? `/tmp/source-first-candidate-${generation}.json`;
	const required = [
		"UPSTASH_REDIS_REST_URL",
		"UPSTASH_REDIS_REST_TOKEN",
		"UPSTASH_VECTOR_REST_URL",
		"UPSTASH_VECTOR_REST_TOKEN",
		"OPENAI_API_KEY",
	];
	for (const name of required) {
		if (!process.env[name]) throw new Error(`missing environment variable: ${name}`);
	}
	const redis = new Redis({
		url: process.env.UPSTASH_REDIS_REST_URL!,
		token: process.env.UPSTASH_REDIS_REST_TOKEN!,
	});
	const vector = new Index({
		url: process.env.UPSTASH_VECTOR_REST_URL!,
		token: process.env.UPSTASH_VECTOR_REST_TOKEN!,
	});
	const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY! });
	const repoRoot = path.resolve(import.meta.dirname, "../../..");
	const rows: Record<string, unknown>[] = [];
	for (const probe of loadProbes(repoRoot)) {
		const embedding = await openai.embeddings.create({
			model: "text-embedding-3-large",
			input: probe.query,
			dimensions: 3072,
		});
		const payload = await sourceFirstSearchGeneration(
			redis as never,
			vector as never,
			embedding.data[0]!.embedding,
			probe.query,
			5,
			generation,
		);
		const row = scoreProbe(probe, payload);
		rows.push(row);
		process.stdout.write(`${row.passed ? "PASS" : "FAIL"} ${probe.id}\n`);
	}
	const failed = rows.filter((row) => row.passed !== true);
	const report = {
		schema_version: 1,
		generation,
		generated_at: new Date().toISOString(),
		passed: failed.length === 0,
		probe_count: rows.length,
		failed_count: failed.length,
		rows,
	};
	fs.writeFileSync(outputPath, `${JSON.stringify(report, null, 2)}\n`);
	process.stdout.write(`wrote ${outputPath}\n`);
	if (failed.length > 0) process.exitCode = 1;
}

await main();
