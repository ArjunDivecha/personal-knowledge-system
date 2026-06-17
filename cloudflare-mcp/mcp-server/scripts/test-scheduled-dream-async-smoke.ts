import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Redis } from "@upstash/redis";

interface SmokeArgs {
	baseUrl: string;
	runDate: string;
	envFile: string;
	timeoutMs: number;
	pollMs: number;
	cleanup: boolean;
}

interface SmokeEnv {
	DREAM_OPERATOR_TOKEN: string;
	UPSTASH_REDIS_REST_URL?: string;
	UPSTASH_REDIS_REST_TOKEN?: string;
}

interface HttpResult {
	status: number;
	body: Record<string, unknown>;
}

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const WORKER_ROOT = path.resolve(SCRIPT_DIR, "..");
const DEFAULT_ENV_FILE = path.join(WORKER_ROOT, ".dev.vars");
const DEFAULT_BASE_URL = "https://mcp.dancing-ganesh.com";

function usage(): string {
	return [
		"Usage:",
		"  node --experimental-strip-types scripts/test-scheduled-dream-async-smoke.ts --base-url <url> --run-date <YYYY-MM-DD> [options]",
		"",
		"Options:",
		`  --env-file <path>     Env file with DREAM_OPERATOR_TOKEN (default: ${DEFAULT_ENV_FILE})`,
		"  --timeout-ms <n>      Poll timeout (default: 600000)",
		"  --poll-ms <n>         Poll interval (default: 5000)",
		"  --cleanup             Delete this smoke run's status/date-lock keys after success",
		"  --help                Show this help",
	].join("\n");
}

function parseArgs(argv: string[]): SmokeArgs {
	if (argv.includes("--help") || argv.includes("-h")) {
		console.log(usage());
		process.exit(0);
	}
	const get = (flag: string): string | null => {
		const idx = argv.indexOf(flag);
		if (idx === -1) return null;
		if (idx + 1 >= argv.length) throw new Error(`Missing value after ${flag}`);
		return argv[idx + 1];
	};
	const runDate = get("--run-date");
	if (!runDate || !/^\d{4}-\d{2}-\d{2}$/.test(runDate)) {
		throw new Error("--run-date must be YYYY-MM-DD");
	}
	const timeoutMs = Number(get("--timeout-ms") ?? "600000");
	const pollMs = Number(get("--poll-ms") ?? "5000");
	if (!Number.isInteger(timeoutMs) || timeoutMs <= 0) throw new Error("Invalid --timeout-ms");
	if (!Number.isInteger(pollMs) || pollMs <= 0) throw new Error("Invalid --poll-ms");
	return {
		baseUrl: (get("--base-url") ?? DEFAULT_BASE_URL).replace(/\/+$/, ""),
		runDate,
		envFile: path.resolve(get("--env-file") ?? DEFAULT_ENV_FILE),
		timeoutMs,
		pollMs,
		cleanup: argv.includes("--cleanup"),
	};
}

async function loadEnv(envFile: string): Promise<SmokeEnv> {
	const parsed: Record<string, string> = { ...process.env } as Record<string, string>;
	const content = await fs.readFile(envFile, "utf8");
	for (const line of content.split(/\r?\n/)) {
		const trimmed = line.trim();
		if (!trimmed || trimmed.startsWith("#")) continue;
		const idx = trimmed.indexOf("=");
		if (idx <= 0) continue;
		const key = trimmed.slice(0, idx).trim();
		const raw = trimmed.slice(idx + 1).trim();
		parsed[key] = raw.replace(/^['"]|['"]$/g, "");
	}
	if (!parsed.DREAM_OPERATOR_TOKEN) {
		throw new Error(`Missing DREAM_OPERATOR_TOKEN in environment or ${envFile}`);
	}
	return parsed as unknown as SmokeEnv;
}

function assertOk(condition: unknown, message: string): asserts condition {
	if (!condition) throw new Error(message);
}

function yyyymmdd(runDate: string): string {
	return runDate.replace(/-/g, "");
}

function makeBody(runDate: string, suffix = crypto.randomBytes(4).toString("hex")) {
	const compact = yyyymmdd(runDate);
	return {
		run_id: `dga_${compact}_${suffix}`,
		orchestrator_run_id: `pksn_${compact}_230000_${suffix}`,
		run_date: runDate,
		mode: "shadow",
		fencing_token: Math.max(1, Date.now() % 2_000_000_000),
		cron: "codex-smoke",
		scheduled_time: Date.now(),
	};
}

async function readJson(response: Response): Promise<Record<string, unknown>> {
	const text = await response.text();
	if (!text) return {};
	try {
		const parsed = JSON.parse(text);
		return parsed && typeof parsed === "object" && !Array.isArray(parsed)
			? (parsed as Record<string, unknown>)
			: { value: parsed };
	} catch {
		return { text };
	}
}

async function requestJson(
	method: string,
	url: string,
	token: string,
	body?: Record<string, unknown>,
): Promise<HttpResult> {
	const response = await fetch(url, {
		method,
		headers: {
			Authorization: `Bearer ${token}`,
			Accept: "application/json",
			"Content-Type": "application/json",
		},
		body: body ? JSON.stringify(body) : undefined,
	});
	return { status: response.status, body: await readJson(response) };
}

function sleep(ms: number): Promise<void> {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

async function pollTerminal(args: SmokeArgs, env: SmokeEnv, runId: string): Promise<Record<string, unknown>> {
	const deadline = Date.now() + args.timeoutMs;
	let last: HttpResult | null = null;
	while (Date.now() <= deadline) {
		last = await requestJson(
			"GET",
			`${args.baseUrl}/ops/dream/scheduled_governed/status?run_id=${encodeURIComponent(runId)}`,
			env.DREAM_OPERATOR_TOKEN,
		);
		if (last.status === 200 && last.body.state === "terminal") return last.body;
		await sleep(args.pollMs);
	}
	throw new Error(`Timed out waiting for terminal status; last=${JSON.stringify(last)}`);
}

async function cleanupKeys(args: SmokeArgs, env: SmokeEnv, runIds: string[]): Promise<void> {
	if (!args.cleanup) return;
	if (!env.UPSTASH_REDIS_REST_URL || !env.UPSTASH_REDIS_REST_TOKEN) {
		throw new Error("--cleanup requires UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN");
	}
	const redis = new Redis({
		url: env.UPSTASH_REDIS_REST_URL,
		token: env.UPSTASH_REDIS_REST_TOKEN,
	});
	const keys = [
		...runIds.map((runId) => `dream:scheduled-governed:status:${runId}`),
		`dream:scheduled-governed:date-lock:${args.runDate}`,
	];
	await redis.del(...keys);
}

async function main(): Promise<void> {
	const args = parseArgs(process.argv.slice(2));
	const env = await loadEnv(args.envFile);
	const first = makeBody(args.runDate);
	const second = makeBody(args.runDate);

	const start = await requestJson(
		"POST",
		`${args.baseUrl}/ops/dream/scheduled_governed/start`,
		env.DREAM_OPERATOR_TOKEN,
		first,
	);
	assertOk(start.status === 202, `Expected fresh start HTTP 202, got ${start.status}: ${JSON.stringify(start.body)}`);
	assertOk(start.body.accepted === true, `Fresh start was not accepted: ${JSON.stringify(start.body)}`);
	assertOk(start.body.executed_mode === "shadow", `Fresh start did not execute shadow: ${JSON.stringify(start.body)}`);

	const terminal = await pollTerminal(args, env, String(first.run_id));
	assertOk(terminal.executed_mode === "shadow", `Terminal status not shadow: ${JSON.stringify(terminal)}`);
	assertOk(terminal.applied_count === 0, `Shadow applied_count was not 0: ${JSON.stringify(terminal)}`);
	assertOk(terminal.status === "completed_shadow", `Unexpected terminal status: ${JSON.stringify(terminal)}`);

	const duplicate = await requestJson(
		"POST",
		`${args.baseUrl}/ops/dream/scheduled_governed/start`,
		env.DREAM_OPERATOR_TOKEN,
		first,
	);
	assertOk(duplicate.status === 200, `Expected duplicate HTTP 200, got ${duplicate.status}: ${JSON.stringify(duplicate.body)}`);
	assertOk(duplicate.body.duplicate === true, `Duplicate response missing duplicate=true: ${JSON.stringify(duplicate.body)}`);

	const locked = await requestJson(
		"POST",
		`${args.baseUrl}/ops/dream/scheduled_governed/start`,
		env.DREAM_OPERATOR_TOKEN,
		second,
	);
	assertOk(locked.status === 409, `Expected date lock HTTP 409, got ${locked.status}: ${JSON.stringify(locked.body)}`);
	assertOk(locked.body.error === "date_locked", `Expected date_locked error: ${JSON.stringify(locked.body)}`);

	await cleanupKeys(args, env, [String(first.run_id), String(second.run_id)]);

	console.log(JSON.stringify({
		ok: true,
		base_url: args.baseUrl,
		run_date: args.runDate,
		dream_run_id: first.run_id,
		terminal_status: terminal.status,
		executed_mode: terminal.executed_mode,
		applied_count: terminal.applied_count,
		duplicate_status: duplicate.status,
		date_lock_status: locked.status,
		cleanup: args.cleanup,
	}, null, 2));
}

main().catch((error) => {
	console.error(error instanceof Error ? error.message : String(error));
	process.exit(1);
});
