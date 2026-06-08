import {
	classifyPhase8Query,
	scorePhase8Candidate,
	type Phase8QueryIntent,
} from "./phase8Retrieval";

export const PHASE9_SCHEMA_VERSION = 1;
export const PHASE9_VALIDATION_GATE = "dream_outcome_quality";
export const PHASE9_DEFAULT_PROBE_SET_KEY = "dream:outcome_probes";
export const PHASE9_MAX_PROBES = 25;
export const PHASE9_MAX_RESULTS_PER_PROBE = 10;

export interface Phase9OutcomeProbe {
	id: string;
	query: string;
	expected_intent?: string | null;
	expected_top_entry_id?: string | null;
	expected_entry_ids?: string[];
	excluded_entry_ids?: string[];
	min_results?: number | null;
	top_k?: number | null;
	disabled?: boolean | null;
}

export interface Phase9OutcomeEntry {
	id: string;
	type: "knowledge" | "project";
	entry: Record<string, unknown>;
	metadata: Record<string, unknown>;
	label?: string | null;
	summary?: string | null;
}

export interface Phase9OutcomeResult {
	id: string;
	type: "knowledge" | "project";
	final_score: number;
	phase8_retrieval: ReturnType<typeof scorePhase8Candidate>;
}

export interface Phase9OutcomeCheck {
	check_id: string;
	query: string;
	passed: boolean;
	expected: Record<string, unknown>;
	actual: Record<string, unknown>;
	issues: string[];
	schema_version: number;
}

export interface Phase9OutcomeEvalReport {
	generated_at: string;
	passed: boolean;
	check_count: number;
	failure_count: number;
	checks: Phase9OutcomeCheck[];
	retrieval_reports: Array<{
		query: string;
		intent: Phase8QueryIntent;
		results: Phase9OutcomeResult[];
		evaluated_at: string;
		schema_version: number;
	}>;
	schema_version: number;
}

export interface Phase9OutcomeRegression {
	check_id: string;
	query: string;
	reason: string;
	pre_passed: boolean;
	post_passed: boolean | null;
	pre_actual: Record<string, unknown>;
	post_actual: Record<string, unknown> | null;
	pre_issues: string[];
	post_issues: string[] | null;
	schema_version: number;
}

export interface Phase9OutcomeGateReport {
	generated_at: string;
	passed: boolean;
	rollback_required: boolean;
	rollback_reason: string | null;
	regression_count: number;
	regressions: Phase9OutcomeRegression[];
	pre_report: Phase9OutcomeEvalReport;
	post_report: Phase9OutcomeEvalReport;
	proposal_id: string | null;
	apply_mutation_id: string | null;
	schema_version: number;
}

export interface Phase9RollbackRecommendation {
	required: boolean;
	ready: boolean;
	proposal_id: string | null;
	apply_mutation_id: string | null;
	rollback_mutation_id: string | null;
	reason: string | null;
	issues: string[];
	operation_ids: string[] | null;
	schema_version: number;
}

export function parsePhase9OutcomeProbes(raw: unknown): Phase9OutcomeProbe[] {
	const payload = parseObject(raw);
	const source = Array.isArray(raw)
		? raw
		: Array.isArray(payload?.probes)
			? payload.probes
			: [];
	return source
		.map((item) => parsePhase9OutcomeProbe(item))
		.filter((probe): probe is Phase9OutcomeProbe => Boolean(probe))
		.slice(0, PHASE9_MAX_PROBES);
}

export function evaluatePhase9OutcomeProbes(
	probes: Phase9OutcomeProbe[],
	entries: Phase9OutcomeEntry[],
	options: { now?: Date } = {},
): Phase9OutcomeEvalReport {
	const now = options.now ?? new Date();
	const generatedAt = now.toISOString();
	const checks: Phase9OutcomeCheck[] = [];
	const retrievalReports: Phase9OutcomeEvalReport["retrieval_reports"] = [];
	const activeProbes = probes
		.filter((probe) => probe.disabled !== true)
		.slice(0, PHASE9_MAX_PROBES);

	for (const probe of activeProbes) {
		const report = retrievePhase9OutcomeProbe(probe, entries, now);
		retrievalReports.push(report);
		checks.push(evaluatePhase9OutcomeProbe(probe, report));
	}

	const failureCount = checks.filter((check) => !check.passed).length;
	return {
		generated_at: generatedAt,
		passed: failureCount === 0,
		check_count: checks.length,
		failure_count: failureCount,
		checks,
		retrieval_reports: retrievalReports,
		schema_version: PHASE9_SCHEMA_VERSION,
	};
}

export function evaluatePhase9OutcomeGate(
	preReport: Phase9OutcomeEvalReport,
	postReport: Phase9OutcomeEvalReport,
	options: {
	generatedAt?: string;
	proposalId?: string | null;
	applyMutationId?: string | null;
	blockOnNewPostFailures?: boolean;
} = {}): Phase9OutcomeGateReport {
	const generatedAt = options.generatedAt ?? new Date().toISOString();
	const proposalId = options.proposalId ?? null;
	const applyMutationId = options.applyMutationId ?? null;
	const blockOnNewPostFailures = options.blockOnNewPostFailures ?? true;
	if (!preReport.passed) {
		return {
			generated_at: generatedAt,
			passed: false,
			rollback_required: false,
			rollback_reason: "pre_outcome_baseline_failed",
			regression_count: 0,
			regressions: [],
			pre_report: preReport,
			post_report: postReport,
			proposal_id: proposalId,
			apply_mutation_id: applyMutationId,
			schema_version: PHASE9_SCHEMA_VERSION,
		};
	}

	const regressions = findPhase9OutcomeRegressions(
		preReport,
		postReport,
		blockOnNewPostFailures,
	);
	const rollbackRequired = regressions.length > 0;
	return {
		generated_at: generatedAt,
		passed: postReport.passed && !rollbackRequired,
		rollback_required: rollbackRequired,
		rollback_reason: rollbackRequired ? "phase9_outcome_probe_regression" : null,
		regression_count: regressions.length,
		regressions,
		pre_report: preReport,
		post_report: postReport,
		proposal_id: proposalId,
		apply_mutation_id: applyMutationId,
		schema_version: PHASE9_SCHEMA_VERSION,
	};
}

export function buildPhase9RollbackRecommendation(
	report: Phase9OutcomeGateReport,
	options: {
	rollbackMutationId?: string | null;
	operationIds?: string[] | null;
} = {}): Phase9RollbackRecommendation {
	const rollbackMutationId = options.rollbackMutationId;
	const operationIds = options.operationIds ?? null;
	if (!report.rollback_required) {
		return {
			required: false,
			ready: false,
			proposal_id: report.proposal_id,
			apply_mutation_id: report.apply_mutation_id,
			rollback_mutation_id: null,
			reason: null,
			issues: [],
			operation_ids: operationIds,
			schema_version: PHASE9_SCHEMA_VERSION,
		};
	}

	const issues: string[] = [];
	if (!report.proposal_id) issues.push("missing_proposal_id");
	if (!report.apply_mutation_id) issues.push("missing_apply_mutation_id");
	const ready = issues.length === 0;
	return {
		required: true,
		ready,
		proposal_id: report.proposal_id,
		apply_mutation_id: report.apply_mutation_id,
		rollback_mutation_id: ready
			? rollbackMutationId ?? defaultRollbackMutationId(report)
			: null,
		reason: rollbackReason(report),
		issues,
		operation_ids: operationIds,
		schema_version: PHASE9_SCHEMA_VERSION,
	};
}

export function buildPhase9ValidationGatePayload(
	report: Phase9OutcomeGateReport,
): {
	gate: string;
	passed: boolean;
	issues: Array<Record<string, unknown>>;
	details: Record<string, unknown>;
} {
	return {
		gate: PHASE9_VALIDATION_GATE,
		passed: report.passed,
		issues: report.regressions.map((regression) => ({
			check_id: regression.check_id,
			reason: regression.reason,
			post_issues: regression.post_issues ?? [],
		})),
		details: {
			schema_version: report.schema_version,
			gate: PHASE9_VALIDATION_GATE,
			passed: report.passed,
			rollback_required: report.rollback_required,
			rollback_reason: report.rollback_reason,
			proposal_id: report.proposal_id,
			apply_mutation_id: report.apply_mutation_id,
			pre_check_count: report.pre_report.check_count,
			pre_failure_count: report.pre_report.failure_count,
			post_check_count: report.post_report.check_count,
			post_failure_count: report.post_report.failure_count,
			regression_count: report.regression_count,
			regressed_check_ids: report.regressions.map((regression) => regression.check_id),
		},
	};
}

function retrievePhase9OutcomeProbe(
	probe: Phase9OutcomeProbe,
	entries: Phase9OutcomeEntry[],
	now: Date,
): Phase9OutcomeEvalReport["retrieval_reports"][number] {
	const intent = classifyPhase8Query(probe.query);
	const limit = Math.max(
		1,
		Math.min(
			PHASE9_MAX_RESULTS_PER_PROBE,
			Math.trunc(probe.top_k ?? PHASE9_MAX_RESULTS_PER_PROBE),
		),
	);
	const results = entries
		.map((entry) => {
			const phase8 = scorePhase8Candidate(probe.query, intent, {
				entry: entry.entry,
				metadata: entry.metadata,
				label: entry.label ?? null,
				summary: entry.summary ?? null,
				entryType: entry.type,
				vectorScore: 0,
				now,
			});
			return {
				id: entry.id,
				type: entry.type,
				final_score: phase8.final_score,
				phase8_retrieval: phase8,
			};
		})
		.sort((left, right) =>
			right.final_score - left.final_score ||
			left.id.localeCompare(right.id),
		)
		.slice(0, limit);

	return {
		query: probe.query,
		intent,
		results,
		evaluated_at: now.toISOString(),
		schema_version: PHASE9_SCHEMA_VERSION,
	};
}

function evaluatePhase9OutcomeProbe(
	probe: Phase9OutcomeProbe,
	report: Phase9OutcomeEvalReport["retrieval_reports"][number],
): Phase9OutcomeCheck {
	const expected: Record<string, unknown> = {};
	const actual: Record<string, unknown> = {};
	const issues: string[] = [];
	const resultIds = report.results.map((result) => result.id);

	if (probe.expected_intent) {
		expected.intent = probe.expected_intent;
		actual.intent = report.intent.intent;
		if (report.intent.intent !== probe.expected_intent) {
			issues.push(`intent mismatch: expected ${probe.expected_intent}, got ${report.intent.intent}`);
		}
	}

	if (probe.expected_top_entry_id) {
		expected.top_entry_id = probe.expected_top_entry_id;
		actual.top_entry_id = resultIds[0] ?? null;
		if (resultIds[0] !== probe.expected_top_entry_id) {
			issues.push(`top result mismatch: expected ${probe.expected_top_entry_id}, got ${actual.top_entry_id}`);
		}
	}

	if (probe.expected_entry_ids && probe.expected_entry_ids.length > 0) {
		expected.entry_ids = probe.expected_entry_ids;
		actual.entry_ids = resultIds;
		for (const entryId of probe.expected_entry_ids) {
			if (!resultIds.includes(entryId)) {
				issues.push(`missing entry: ${entryId}`);
			}
		}
	}

	if (probe.excluded_entry_ids && probe.excluded_entry_ids.length > 0) {
		expected.excluded_entry_ids = probe.excluded_entry_ids;
		actual.entry_ids = resultIds;
		for (const entryId of probe.excluded_entry_ids) {
			if (resultIds.includes(entryId)) {
				issues.push(`unexpected entry: ${entryId}`);
			}
		}
	}

	if (typeof probe.min_results === "number") {
		expected.min_results = probe.min_results;
		actual.result_count = resultIds.length;
		if (resultIds.length < probe.min_results) {
			issues.push(`result count below minimum: expected ${probe.min_results}, got ${resultIds.length}`);
		}
	}

	actual.entry_ids ??= resultIds;
	actual.scores = Object.fromEntries(
		report.results.map((result) => [result.id, result.final_score]),
	);

	return {
		check_id: probe.id,
		query: probe.query,
		passed: issues.length === 0,
		expected,
		actual,
		issues,
		schema_version: PHASE9_SCHEMA_VERSION,
	};
}

function findPhase9OutcomeRegressions(
	preReport: Phase9OutcomeEvalReport,
	postReport: Phase9OutcomeEvalReport,
	blockOnNewPostFailures: boolean,
): Phase9OutcomeRegression[] {
	const regressions: Phase9OutcomeRegression[] = [];
	const postById = new Map(postReport.checks.map((check) => [check.check_id, check]));
	for (const preCheck of preReport.checks) {
		const postCheck = postById.get(preCheck.check_id);
		if (!postCheck) {
			regressions.push(regressionFromChecks(preCheck, null, "check_missing_after_apply"));
			continue;
		}
		if (preCheck.passed && !postCheck.passed) {
			regressions.push(regressionFromChecks(preCheck, postCheck, "passed_pre_failed_post"));
		}
	}

	if (blockOnNewPostFailures) {
		const preIds = new Set(preReport.checks.map((check) => check.check_id));
		for (const postCheck of postReport.checks) {
			if (!preIds.has(postCheck.check_id) && !postCheck.passed) {
				regressions.push({
					check_id: postCheck.check_id,
					query: postCheck.query,
					reason: "new_post_failure",
					pre_passed: true,
					post_passed: false,
					pre_actual: {},
					post_actual: postCheck.actual,
					pre_issues: [],
					post_issues: postCheck.issues,
					schema_version: PHASE9_SCHEMA_VERSION,
				});
			}
		}
	}

	return regressions;
}

function regressionFromChecks(
	preCheck: Phase9OutcomeCheck,
	postCheck: Phase9OutcomeCheck | null,
	reason: string,
): Phase9OutcomeRegression {
	return {
		check_id: preCheck.check_id,
		query: preCheck.query,
		reason,
		pre_passed: preCheck.passed,
		post_passed: postCheck ? postCheck.passed : null,
		pre_actual: preCheck.actual,
		post_actual: postCheck ? postCheck.actual : null,
		pre_issues: preCheck.issues,
		post_issues: postCheck ? postCheck.issues : null,
		schema_version: PHASE9_SCHEMA_VERSION,
	};
}

function parsePhase9OutcomeProbe(raw: unknown): Phase9OutcomeProbe | null {
	const data = parseObject(raw);
	if (!data) return null;
	const id = stringValue(data.id);
	const query = stringValue(data.query);
	if (!id || !query) return null;
	return {
		id,
		query,
		expected_intent: stringValue(data.expected_intent),
		expected_top_entry_id: stringValue(data.expected_top_entry_id),
		expected_entry_ids: stringArray(data.expected_entry_ids),
		excluded_entry_ids: stringArray(data.excluded_entry_ids),
		min_results: integerValue(data.min_results),
		top_k: integerValue(data.top_k ?? data.limit),
		disabled: data.disabled === true,
	};
}

function parseObject(raw: unknown): Record<string, unknown> | null {
	if (typeof raw === "string") {
		try {
			const parsed = JSON.parse(raw);
			return parseObject(parsed);
		} catch {
			return null;
		}
	}
	if (raw && typeof raw === "object" && !Array.isArray(raw)) {
		return raw as Record<string, unknown>;
	}
	return null;
}

function stringValue(value: unknown): string | null {
	return typeof value === "string" && value.trim().length > 0
		? value.trim()
		: null;
}

function stringArray(value: unknown): string[] {
	return Array.isArray(value)
		? value.filter((item): item is string => typeof item === "string")
		: [];
}

function integerValue(value: unknown): number | null {
	const numeric = typeof value === "number"
		? value
		: typeof value === "string" && value.trim()
			? Number(value)
			: Number.NaN;
	if (!Number.isFinite(numeric)) return null;
	return Math.trunc(numeric);
}

function rollbackReason(report: Phase9OutcomeGateReport): string | null {
	if (!report.rollback_required) return null;
	return `phase9_outcome_probe_regression: ${report.regressions
		.map((regression) => regression.check_id)
		.join(", ")}`;
}

function defaultRollbackMutationId(report: Phase9OutcomeGateReport): string {
	return [
		"phase9_rollback",
		safeToken(report.proposal_id ?? "proposal"),
		safeToken(report.apply_mutation_id ?? "apply"),
		safeToken(report.generated_at),
	].join("_");
}

function safeToken(value: string): string {
	const token = value
		.toLowerCase()
		.replace(/[^a-z0-9]+/g, "_")
		.replace(/^_+|_+$/g, "")
		.replace(/_+/g, "_");
	return token || "unknown";
}
