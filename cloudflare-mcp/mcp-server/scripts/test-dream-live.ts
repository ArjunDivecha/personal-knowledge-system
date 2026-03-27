import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Redis } from "@upstash/redis";
import { Index } from "@upstash/vector";

type EntryType = "knowledge" | "project";
type DreamRunResult = Record<string, unknown>;

interface TestEnv {
	UPSTASH_REDIS_REST_URL: string;
	UPSTASH_REDIS_REST_TOKEN: string;
	UPSTASH_VECTOR_REST_URL: string;
	UPSTASH_VECTOR_REST_TOKEN: string;
	OPENAI_API_KEY: string;
	GITHUB_TOKEN: string;
	DREAM_OPERATOR_TOKEN: string;
}

interface CandidateRef {
	id: string;
	type: EntryType;
	label: string | null;
}

interface EntrySnapshot {
	id: string;
	type: EntryType;
	active_key: string;
	active_exists: boolean;
	active_metadata: Record<string, unknown> | null;
	active_summary: Record<string, unknown> | null;
	latest_pointer_key: string;
	latest_pointer_exists: boolean;
	latest_pointer: Record<string, unknown> | null;
	archive_snapshot_exists: boolean;
	archive_snapshot: Record<string, unknown> | null;
	vector_probe: Record<string, unknown> | null;
	vector_probe_error: string | null;
}

interface AssertionResult {
	name: string;
	passed: boolean;
	details: string;
}

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const WORKER_ROOT = path.resolve(SCRIPT_DIR, "..");
const REPO_ROOT = path.resolve(SCRIPT_DIR, "../../..");
const REPORTS_DIR = path.join(REPO_ROOT, "scripts", "reports");
const DEFAULT_OPERATOR_BASE_URL = "https://mcp.dancing-ganesh.com";

function parseArgs(argv: string[]): { count: number; baseUrl: string } {
	const defaultCount = 3;
	const defaultBaseUrl = DEFAULT_OPERATOR_BASE_URL;
	const countFlagIndex = argv.findIndex((value) => value === "--count");
	const baseUrlFlagIndex = argv.findIndex((value) => value === "--base-url");

	let count = defaultCount;
	if (countFlagIndex !== -1) {
		if (countFlagIndex + 1 >= argv.length) {
			throw new Error("Missing value after --count");
		}
		const parsed = Number(argv[countFlagIndex + 1]);
		if (!Number.isInteger(parsed) || parsed <= 0) {
			throw new Error(`Invalid --count value: ${argv[countFlagIndex + 1]}`);
		}
		count = parsed;
	}

	let baseUrl = defaultBaseUrl;
	if (baseUrlFlagIndex !== -1) {
		if (baseUrlFlagIndex + 1 >= argv.length) {
			throw new Error("Missing value after --base-url");
		}
		baseUrl = argv[baseUrlFlagIndex + 1];
	}

	return { count, baseUrl };
}

async function loadEnvFromDevVars(): Promise<TestEnv> {
	const devVarsPath = path.join(WORKER_ROOT, ".dev.vars");
	const content = await fs.readFile(devVarsPath, "utf8");
	const parsed: Record<string, string> = {};

	for (const line of content.split(/\r?\n/)) {
		const trimmed = line.trim();
		if (!trimmed || trimmed.startsWith("#")) continue;
		const separatorIndex = trimmed.indexOf("=");
		if (separatorIndex <= 0) continue;
		const key = trimmed.slice(0, separatorIndex);
		const value = trimmed.slice(separatorIndex + 1);
		parsed[key] = value;
	}

	const requiredKeys = [
		"UPSTASH_REDIS_REST_URL",
		"UPSTASH_REDIS_REST_TOKEN",
		"UPSTASH_VECTOR_REST_URL",
		"UPSTASH_VECTOR_REST_TOKEN",
		"OPENAI_API_KEY",
		"GITHUB_TOKEN",
		"DREAM_OPERATOR_TOKEN",
	] as const;

	for (const key of requiredKeys) {
		if (!parsed[key]) {
			throw new Error(`Missing ${key} in ${devVarsPath}`);
		}
	}

	return parsed as TestEnv;
}

function createRedisClient(env: TestEnv): Redis {
	return new Redis({
		url: env.UPSTASH_REDIS_REST_URL,
		token: env.UPSTASH_REDIS_REST_TOKEN,
	});
}

function createVectorClient(env: TestEnv): Index {
	return new Index({
		url: env.UPSTASH_VECTOR_REST_URL,
		token: env.UPSTASH_VECTOR_REST_TOKEN,
	});
}

function parseStoredObject(raw: unknown): Record<string, unknown> | null {
	if (typeof raw === "string") {
		try {
			const parsed = JSON.parse(raw);
			if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
				return parsed as Record<string, unknown>;
			}
		} catch {
			return null;
		}
	}

	if (raw && typeof raw === "object" && !Array.isArray(raw)) {
		return { ...(raw as Record<string, unknown>) };
	}

	return null;
}

function inferEntryType(entryId: string, explicitType?: string | null): EntryType {
	if (explicitType === "knowledge" || explicitType === "project") {
		return explicitType;
	}
	return entryId.startsWith("pe_") ? "project" : "knowledge";
}

function getEntryKey(entryType: EntryType, entryId: string): string {
	return `${entryType}:${entryId}`;
}

function getArchivedLatestKey(entryType: EntryType, entryId: string): string {
	return `archived:${entryType}:${entryId}:latest`;
}

function summarizeMetadata(metadata: Record<string, unknown> | null): Record<string, unknown> | null {
	if (!metadata) return null;
	return {
		context_type: metadata.context_type ?? null,
		injection_tier: metadata.injection_tier ?? null,
		archived: metadata.archived ?? null,
		mention_count: metadata.mention_count ?? null,
		access_count: metadata.access_count ?? null,
		salience_score: metadata.salience_score ?? null,
		last_consolidated: metadata.last_consolidated ?? null,
		archived_at: metadata.archived_at ?? null,
		archived_reason: metadata.archived_reason ?? null,
		archived_run_id: metadata.archived_run_id ?? null,
		archive_snapshot_key: metadata.archive_snapshot_key ?? null,
		restored_at: metadata.restored_at ?? null,
		restored_reason: metadata.restored_reason ?? null,
	};
}

async function fetchEntrySnapshot(
	redis: Redis,
	vector: Index,
	candidate: CandidateRef,
): Promise<EntrySnapshot> {
	const activeKey = getEntryKey(candidate.type, candidate.id);
	const latestPointerKey = getArchivedLatestKey(candidate.type, candidate.id);
	const active = parseStoredObject(await redis.get(activeKey));
	const activeMetadata = parseStoredObject(active?.metadata);
	const latestPointer = parseStoredObject(await redis.get(latestPointerKey));
	const archiveSnapshot =
		typeof latestPointer?.snapshot_key === "string"
			? parseStoredObject(await redis.get(latestPointer.snapshot_key))
			: null;

	let vectorProbe: Record<string, unknown> | null = null;
	let vectorProbeError: string | null = null;
	try {
		const result = await vector.fetch([candidate.id], { includeMetadata: true });
		const item = Array.isArray(result) ? result[0] : null;
		const metadata =
			item && item.metadata && typeof item.metadata === "object" && !Array.isArray(item.metadata)
				? (item.metadata as Record<string, unknown>)
				: null;
		vectorProbe = {
			found: Boolean(item),
			metadata: metadata
				? {
					archived: metadata.archived ?? null,
					context_type: metadata.context_type ?? null,
					injection_tier: metadata.injection_tier ?? null,
					salience_score: metadata.salience_score ?? null,
					last_consolidated: metadata.last_consolidated ?? null,
				}
				: null,
		};
	} catch (error) {
		vectorProbeError = error instanceof Error ? error.message : String(error);
	}

	return {
		id: candidate.id,
		type: candidate.type,
		active_key: activeKey,
		active_exists: active !== null,
		active_metadata: activeMetadata,
		active_summary: summarizeMetadata(activeMetadata),
		latest_pointer_key: latestPointerKey,
		latest_pointer_exists: latestPointer !== null,
		latest_pointer: latestPointer,
		archive_snapshot_exists: archiveSnapshot !== null,
		archive_snapshot: archiveSnapshot
			? {
				entry_id: archiveSnapshot.entry_id ?? null,
				entry_type: archiveSnapshot.entry_type ?? null,
				run_id: archiveSnapshot.run_id ?? null,
				archived_at: archiveSnapshot.archived_at ?? null,
				archive_reason: archiveSnapshot.archive_reason ?? null,
				snapshot_metadata: summarizeMetadata(
					parseStoredObject(parseStoredObject(archiveSnapshot.snapshot)?.metadata),
				),
			}
			: null,
		vector_probe: vectorProbe,
		vector_probe_error: vectorProbeError,
	};
}

async function fetchHealth(url: string): Promise<Record<string, unknown>> {
	const response = await fetch(url);
	return {
		url,
		status: response.status,
		ok: response.ok,
		body: await response.json(),
	};
}

async function callOperatorEndpoint<T>(
	baseUrl: string,
	token: string,
	pathname: string,
	body: Record<string, unknown>,
): Promise<T> {
	const response = await fetch(`${baseUrl}${pathname}`, {
		method: "POST",
		headers: {
			"Content-Type": "application/json",
			Authorization: `Bearer ${token}`,
		},
		body: JSON.stringify(body),
	});
	const payload = await response.json();
	if (!response.ok) {
		throw new Error(`Operator endpoint ${pathname} failed with ${response.status}: ${JSON.stringify(payload)}`);
	}
	return payload as T;
}

async function runOperatorDreamCycle(
	baseUrl: string,
	token: string,
	body: {
		dry_run: boolean;
		candidate_ids?: string[];
		archive_limit?: number;
		set_as_latest?: boolean;
		note?: string;
	},
): Promise<DreamRunResult> {
	return callOperatorEndpoint<DreamRunResult>(baseUrl, token, "/ops/dream/run", body);
}

async function restoreOperatorArchivedEntry(
	baseUrl: string,
	token: string,
	entryId: string,
	reason: string,
): Promise<Record<string, unknown>> {
	return callOperatorEndpoint<Record<string, unknown>>(baseUrl, token, "/ops/dream/restore", {
		entry_id: entryId,
		reason,
	});
}

function normalizeCandidate(raw: Record<string, unknown>): CandidateRef | null {
	if (typeof raw.id !== "string" || raw.id.length === 0) return null;
	return {
		id: raw.id,
		type: inferEntryType(raw.id, typeof raw.type === "string" ? raw.type : null),
		label: typeof raw.label === "string" ? raw.label : null,
	};
}

function buildAssertions(report: Record<string, unknown>): AssertionResult[] {
	const selectedCandidates = report.selected_candidates as CandidateRef[];
	const before = report.before_state as EntrySnapshot[];
	const liveRun = report.live_archive_run as Record<string, unknown>;
	const afterArchive = report.after_archive_state as EntrySnapshot[];
	const restoreResults = report.restore_results as Array<Record<string, unknown>>;
	const afterRestore = report.after_restore_state as EntrySnapshot[];
	const postRestoreDryRun = report.post_restore_dry_run as Record<string, unknown>;
	const baselineLastRunId = report.baseline_last_run_id as string | null;
	const finalLastRunId = report.final_last_run_id as string | null;

	return [
		{
			name: "selected_candidate_count",
			passed: selectedCandidates.length > 0,
			details: `Selected ${selectedCandidates.length} candidates for the controlled Dream run.`,
		},
		{
			name: "clean_baseline_state",
			passed: before.every((entry) => entry.active_summary?.archived === false && !entry.latest_pointer_exists),
			details: "Each selected entry started active and without a pre-existing archive pointer.",
		},
		{
			name: "live_archive_count_matches_selection",
			passed: Number(liveRun.counts?.archived ?? 0) === selectedCandidates.length,
			details: `Live archive run archived ${String(liveRun.counts?.archived ?? 0)} entries for ${selectedCandidates.length} selected candidates.`,
		},
		{
			name: "after_archive_state_is_reversible",
			passed: afterArchive.every(
				(entry) =>
					entry.active_summary?.archived === true &&
					entry.latest_pointer_exists &&
					entry.archive_snapshot_exists,
			),
			details: "Each archived entry retained both the active archived marker and a recoverable snapshot pointer.",
		},
		{
			name: "restore_completed_for_all_entries",
			passed: restoreResults.length === selectedCandidates.length,
			details: `Restore step returned ${restoreResults.length} results for ${selectedCandidates.length} selected entries.`,
		},
		{
			name: "restored_entries_return_to_active_tier_one",
			passed: afterRestore.every(
				(entry) =>
					entry.active_summary?.archived === false &&
					entry.active_summary?.context_type === "explicit_save" &&
					entry.active_summary?.injection_tier === 1 &&
					entry.latest_pointer_exists,
			),
			details: "Each restored entry is active again as explicit_save / Tier 1 while preserving the archive pointer.",
		},
		{
			name: "post_restore_candidates_no_longer_prunable",
			passed: Number(postRestoreDryRun.counts?.archive_candidates ?? 0) === 0,
			details: `Post-restore dry run reported ${String(postRestoreDryRun.counts?.archive_candidates ?? 0)} archive candidates across the restored ids.`,
		},
		{
			name: "latest_dream_run_unchanged",
			passed: baselineLastRunId === finalLastRunId,
			details: `Baseline last Dream run id: ${baselineLastRunId ?? "null"}; final last Dream run id: ${finalLastRunId ?? "null"}.`,
		},
	];
}

async function main(): Promise<void> {
	const { count, baseUrl } = parseArgs(process.argv.slice(2));
	await fs.mkdir(REPORTS_DIR, { recursive: true });

	const env = await loadEnvFromDevVars();
	const redis = createRedisClient(env);
	const vector = createVectorClient(env);
	const startedAt = new Date().toISOString();
	const healthBefore = await Promise.all([
		fetchHealth(`${baseUrl}/health`),
		fetchHealth("https://arjun-knowledge-mcp.arjun-divecha.workers.dev/health"),
	]);
	const baselineLastRun = parseStoredObject(await redis.get("dream:last_run"));
	const baselineLastRunId =
		typeof baselineLastRun?.run_id === "string" ? baselineLastRun.run_id : null;

	const preflightDryRun = await runOperatorDreamCycle(baseUrl, env.DREAM_OPERATOR_TOKEN, {
		dry_run: true,
		note: "Controlled Dream live test preflight",
		set_as_latest: false,
	});

	const archiveCandidatesRaw = Array.isArray(preflightDryRun.archive_candidates)
		? preflightDryRun.archive_candidates
		: [];
	const candidatePool = archiveCandidatesRaw
		.map((item) => (item && typeof item === "object" ? normalizeCandidate(item as Record<string, unknown>) : null))
		.filter((item): item is CandidateRef => item !== null);

	const selectedCandidates: CandidateRef[] = [];
	const beforeState: EntrySnapshot[] = [];
	for (const candidate of candidatePool) {
		if (selectedCandidates.length >= count) break;
		const snapshot = await fetchEntrySnapshot(redis, vector, candidate);
		if (snapshot.active_summary?.archived === false && !snapshot.latest_pointer_exists) {
			selectedCandidates.push(candidate);
			beforeState.push(snapshot);
		}
	}

	if (selectedCandidates.length < count) {
		throw new Error(
			`Requested ${count} clean archive candidates but only found ${selectedCandidates.length}.`,
		);
	}

	const selectedIds = selectedCandidates.map((candidate) => candidate.id);
	let liveArchiveRun: Record<string, unknown> | null = null;
	let afterArchiveState: EntrySnapshot[] = [];
	const restoreResults: Array<Record<string, unknown>> = [];
	let afterRestoreState: EntrySnapshot[] = [];

	try {
		liveArchiveRun = await runOperatorDreamCycle(baseUrl, env.DREAM_OPERATOR_TOKEN, {
			dry_run: false,
			note: `Controlled Dream live archive verification for ${selectedIds.length} entries`,
			candidate_ids: selectedIds,
			archive_limit: selectedIds.length,
			set_as_latest: false,
		});

		afterArchiveState = await Promise.all(
			selectedCandidates.map((candidate) => fetchEntrySnapshot(redis, vector, candidate)),
		);

		for (const candidate of selectedCandidates) {
			restoreResults.push(
				await restoreOperatorArchivedEntry(
					baseUrl,
					env.DREAM_OPERATOR_TOKEN,
					candidate.id,
					"Restore after controlled Dream live archive verification",
				),
			);
		}

		afterRestoreState = await Promise.all(
			selectedCandidates.map((candidate) => fetchEntrySnapshot(redis, vector, candidate)),
		);
	} finally {
		for (const candidate of selectedCandidates) {
			const snapshot = await fetchEntrySnapshot(redis, vector, candidate);
			if (snapshot.active_summary?.archived === true) {
				await restoreOperatorArchivedEntry(
					baseUrl,
					env.DREAM_OPERATOR_TOKEN,
					candidate.id,
					"Recovery restore after interrupted Dream live archive verification",
				);
			}
		}
	}

	const postRestoreDryRun = await runOperatorDreamCycle(baseUrl, env.DREAM_OPERATOR_TOKEN, {
		dry_run: true,
		note: "Controlled Dream live test post-restore verification",
		candidate_ids: selectedIds,
		archive_limit: selectedIds.length,
		set_as_latest: false,
	});

	const healthAfter = await Promise.all([
		fetchHealth(`${baseUrl}/health`),
		fetchHealth("https://arjun-knowledge-mcp.arjun-divecha.workers.dev/health"),
	]);
	const finalLastRun = parseStoredObject(await redis.get("dream:last_run"));
	const finalLastRunId = typeof finalLastRun?.run_id === "string" ? finalLastRun.run_id : null;

	const report = {
		schema_version: 1,
		test_name: "dream_live_archive_verification",
		started_at: startedAt,
		finished_at: new Date().toISOString(),
		operator_base_url: baseUrl,
		requested_candidate_count: count,
		selected_candidates: selectedCandidates,
		baseline_last_run_id: baselineLastRunId,
		final_last_run_id: finalLastRunId,
		health_before: healthBefore,
		health_after: healthAfter,
		preflight_dry_run: preflightDryRun,
		before_state: beforeState,
		live_archive_run: liveArchiveRun,
		after_archive_state: afterArchiveState,
		restore_results: restoreResults,
		after_restore_state: afterRestoreState,
		post_restore_dry_run: postRestoreDryRun,
	};

	const assertions = buildAssertions(report);
	const failedAssertions = assertions.filter((assertion) => !assertion.passed);
	const finalReport = {
		...report,
		assertions,
		status: failedAssertions.length === 0 ? "passed" : "failed",
	};

	const timestampToken = finalReport.finished_at.replace(/[:.]/g, "-");
	const reportPath = path.join(REPORTS_DIR, `dream_live_test_${timestampToken}.json`);
	await fs.writeFile(reportPath, `${JSON.stringify(finalReport, null, 2)}\n`, "utf8");

	console.log(
		JSON.stringify(
			{
				status: finalReport.status,
				report_path: reportPath,
				selected_candidates: selectedCandidates,
				live_run_id: liveArchiveRun?.run_id ?? null,
				post_restore_archive_candidates: postRestoreDryRun.counts?.archive_candidates ?? null,
				failed_assertions: failedAssertions,
			},
			null,
			2,
		),
	);

	if (failedAssertions.length > 0) {
		process.exitCode = 1;
	}
}

await main();
