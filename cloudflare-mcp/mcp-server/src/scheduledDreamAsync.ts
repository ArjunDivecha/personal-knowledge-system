/**
 * scheduledDreamAsync.ts
 *
 * Phase 2 of the PKS nightly orchestrator redesign: an async, idempotent
 * scheduled-governed Dream contract for the M4 orchestrator.
 *
 * See docs/pks-nightly-orchestrator-phase-2-dream-worker-prd-2026-06-16.md and
 * docs/pks-nightly-orchestrator-redesign-2026-06-16.md (Atomicity And Race
 * Contract).
 *
 * This module WRAPS the existing Dream machinery (runDreamProposal,
 * gradeDreamProposal, buildScheduledGovernedDecision, applyDreamProposal,
 * verifyScheduledGovernedApply). It does NOT reimplement Dream apply.
 *
 * Guarantees:
 * - One scheduled-governed Dream run per dream_run_id (idempotent start).
 * - One run per run_date (atomic Redis date lock; equality-only fence).
 * - Shadow mode proposes/grades/decides but NEVER calls applyDreamProposal;
 *   terminal status always has executed_mode and applied_count: 0.
 * - Live apply is gated behind a pure, directly-tested gate AND
 *   env.PKS_ORCH_DREAM_LIVE_ENABLED === "1" (kept off in production Phase 2).
 *
 * No request-scoped module globals: all run state lives in Redis or
 * function-local variables.
 */
import { z } from "zod";
import {
	applyDreamProposal,
	gradeDreamProposal,
	runDreamProposal,
} from "./dream";
import {
	buildScheduledGovernedDecision,
	createRedisClient,
	SCHEDULED_DREAM_ARCHIVE_LIMIT,
	SCHEDULED_DREAM_DUPLICATE_MERGE_LIMIT,
	SCHEDULED_DREAM_MARK_CONTESTED_LIMIT,
	SCHEDULED_DREAM_MAX_ENTRIES_TOUCHED,
	SCHEDULED_DREAM_PROMOTION_LIMIT,
	verifyScheduledGovernedApply,
} from "./index";

type Redis = ReturnType<typeof createRedisClient>;

// ── Redis keys (PRD "Redis Keys") ────────────────────────────────────────────
const STATUS_PREFIX = "dream:scheduled-governed:status:";
const DATE_LOCK_PREFIX = "dream:scheduled-governed:date-lock:";
const LAST_STARTED_KEY = "dream:scheduled-governed:last_started";
const LAST_COMPLETED_KEY = "dream:scheduled-governed:last_completed";

const DATE_LOCK_TTL_SECONDS = 36 * 60 * 60; // PRD: 36 hours
const STATUS_TTL_SECONDS = 14 * 24 * 60 * 60; // keep status well past the date lock
const SCHEMA_VERSION = 1;

const statusKey = (dreamRunId: string) => `${STATUS_PREFIX}${dreamRunId}`;
const dateLockKey = (runDate: string) => `${DATE_LOCK_PREFIX}${runDate}`;

// ── Request schema (PRD "Validation") ────────────────────────────────────────
export const scheduledDreamStartRequestSchema = z
	.object({
		run_id: z.string().regex(/^dga_[0-9]{8}_[0-9a-f]{8}$/),
		orchestrator_run_id: z.string().regex(/^pksn_[0-9]{8}_[0-9]{6}_[0-9a-f]{8}$/),
		run_date: z.string().regex(/^\d{4}-\d{2}-\d{2}$/),
		mode: z.enum(["shadow", "live"]),
		fencing_token: z.number().int().positive(),
		cron: z.string().min(1).max(64),
		scheduled_time: z.number().int().positive(),
	})
	.superRefine((val, ctx) => {
		const dreamSuffix = val.run_id.slice(-8);
		const orchSuffix = val.orchestrator_run_id.slice(-8);
		if (dreamSuffix !== orchSuffix) {
			ctx.addIssue({
				code: z.ZodIssueCode.custom,
				message: "suffix mismatch between run_id and orchestrator_run_id",
			});
		}
		const compactDate = val.run_date.replace(/-/g, "");
		const dreamDate = val.run_id.slice(4, 12);
		const orchDate = val.orchestrator_run_id.slice(5, 13);
		if (dreamDate !== compactDate || orchDate !== compactDate) {
			ctx.addIssue({
				code: z.ZodIssueCode.custom,
				message: "run_date does not match the date embedded in the ids",
			});
		}
	});

export type ScheduledDreamStartRequest = z.infer<typeof scheduledDreamStartRequestSchema>;

// Required fields for every terminal status (PRD "Required fields for every
// terminal response"). applied_count: 0 is valid (present, non-null).
export const REQUIRED_TERMINAL_STATUS_FIELDS = [
	"executed_mode",
	"applied_count",
	"dream_run_id",
	"orchestrator_run_id",
	"run_date",
	"requested_mode",
	"state",
	"status",
] as const;

export function validateTerminalStatus(
	status: Record<string, unknown> | null,
): { ok: boolean; missing: string[] } {
	if (!status) return { ok: false, missing: [...REQUIRED_TERMINAL_STATUS_FIELDS] };
	const missing = REQUIRED_TERMINAL_STATUS_FIELDS.filter(
		(field) => status[field] === undefined || status[field] === null,
	);
	return { ok: missing.length === 0, missing };
}

export interface StartResult {
	status: number;
	body: Record<string, unknown>;
	/** present only when a fresh run was accepted and must be executed */
	execute?: ScheduledDreamStartRequest & { executed_mode: string };
}

// ── helpers ──────────────────────────────────────────────────────────────────
function parseJson(raw: unknown): Record<string, unknown> | null {
	if (raw && typeof raw === "object" && !Array.isArray(raw)) {
		return raw as Record<string, unknown>;
	}
	if (typeof raw === "string") {
		try {
			const parsed = JSON.parse(raw);
			return parsed && typeof parsed === "object" && !Array.isArray(parsed)
				? (parsed as Record<string, unknown>)
				: null;
		} catch {
			return null;
		}
	}
	return null;
}

function liveEnabled(env: Env): boolean {
	return env.PKS_ORCH_DREAM_LIVE_ENABLED === "1";
}

async function setStatus(redis: Redis, doc: Record<string, unknown>): Promise<void> {
	doc.updated_at = new Date().toISOString();
	await redis.set(statusKey(String(doc.dream_run_id)), JSON.stringify(doc), {
		ex: STATUS_TTL_SECONDS,
	});
}

// ── atomic date-lock acquisition (Atomicity And Race Contract) ───────────────
// One Lua script: status-exists -> duplicate; no lock -> write lock+accepted
// status; same-run lock -> ensure status + duplicate; different-run lock ->
// date_locked. Equality only; never compares fences with </>.
const DATE_LOCK_ACQUIRE_SCRIPT = `
local status_existing = redis.call('GET', KEYS[1])
if status_existing then
  return cjson.encode({outcome = 'duplicate', status = cjson.decode(status_existing)})
end
local lock_existing = redis.call('GET', KEYS[2])
if lock_existing then
  local l = cjson.decode(lock_existing)
  if l.dream_run_id == ARGV[1] and l.orchestrator_run_id == ARGV[2] then
    redis.call('SET', KEYS[1], ARGV[4], 'EX', tonumber(ARGV[6]))
    return cjson.encode({outcome = 'duplicate', reattached = true, status = cjson.decode(ARGV[4])})
  end
  return cjson.encode({outcome = 'date_locked', blocked_by = {
    dream_run_id = l.dream_run_id,
    orchestrator_run_id = l.orchestrator_run_id,
    fencing_token = l.fencing_token
  }})
end
redis.call('SET', KEYS[2], ARGV[3], 'EX', tonumber(ARGV[5]))
redis.call('SET', KEYS[1], ARGV[4], 'EX', tonumber(ARGV[6]))
return cjson.encode({outcome = 'accepted', status = cjson.decode(ARGV[4])})
`;

function acceptedStatusDoc(req: ScheduledDreamStartRequest, executedMode: string,
	acceptedAt: string): Record<string, unknown> {
	return {
		schema_version: SCHEMA_VERSION,
		dream_run_id: req.run_id,
		orchestrator_run_id: req.orchestrator_run_id,
		run_date: req.run_date,
		requested_mode: req.mode,
		executed_mode: executedMode,
		fencing_token: req.fencing_token,
		cron: req.cron,
		scheduled_time: req.scheduled_time,
		state: "accepted",
		status: "accepted",
		accepted_at: acceptedAt,
		updated_at: acceptedAt,
		applied_count: 0,
		held_count: 0,
		errors: [],
		warnings: [],
		next_action: null,
	};
}

function dateLockDoc(req: ScheduledDreamStartRequest, executedMode: string,
	acquiredAt: string): Record<string, unknown> {
	return {
		dream_run_id: req.run_id,
		orchestrator_run_id: req.orchestrator_run_id,
		run_date: req.run_date,
		mode: executedMode,
		fencing_token: req.fencing_token,
		acquired_at: acquiredAt,
	};
}

// ── start ────────────────────────────────────────────────────────────────────
export async function startScheduledGovernedDreamAsync(
	env: Env,
	ctx: ExecutionContext,
	requestBody: unknown,
): Promise<StartResult> {
	const parsed = scheduledDreamStartRequestSchema.safeParse(requestBody);
	if (!parsed.success) {
		return {
			status: 400,
			body: { accepted: false, error: "invalid_request", issues: parsed.error.issues },
		};
	}
	const req = parsed.data;

	// Production Phase 2: live is rejected unless explicitly enabled in the env.
	if (req.mode === "live" && !liveEnabled(env)) {
		return {
			status: 403,
			body: {
				accepted: false,
				error: "rejected_live_disabled",
				reason: "Worker live apply is disabled (PKS_ORCH_DREAM_LIVE_ENABLED != 1).",
			},
		};
	}

	const executedMode = req.mode; // shadow stays shadow; live only if enabled above
	const redis = createRedisClient(env);
	const now = new Date().toISOString();
	const acceptedDoc = acceptedStatusDoc(req, executedMode, now);

	const raw = await redis.eval(
		DATE_LOCK_ACQUIRE_SCRIPT,
		[statusKey(req.run_id), dateLockKey(req.run_date)],
		[
			req.run_id,
			req.orchestrator_run_id,
			JSON.stringify(dateLockDoc(req, executedMode, now)),
			JSON.stringify(acceptedDoc),
			String(DATE_LOCK_TTL_SECONDS),
			String(STATUS_TTL_SECONDS),
		],
	);
	const result = parseJson(typeof raw === "string" ? raw : JSON.stringify(raw));
	const outcome = result?.outcome;

	if (outcome === "duplicate") {
		return {
			status: 200,
			body: { accepted: true, duplicate: true, reason: "status_exists", status: result?.status },
		};
	}
	if (outcome === "date_locked") {
		return {
			status: 409,
			body: { accepted: false, error: "date_locked", blocked_by: result?.blocked_by },
		};
	}

	// accepted: record last_started, then run the executor in the background.
	await redis.set(LAST_STARTED_KEY, now);
	const execReq = { ...req, executed_mode: executedMode };
	ctx.waitUntil(executeScheduledGovernedDreamAsync(env, execReq));

	return {
		status: 202,
		execute: execReq,
		body: {
			accepted: true,
			duplicate: false,
			requested_mode: req.mode,
			executed_mode: executedMode,
			dream_run_id: req.run_id,
			orchestrator_run_id: req.orchestrator_run_id,
			run_date: req.run_date,
			state: "accepted",
			status: "accepted",
			status_url: `/ops/dream/scheduled_governed/status?run_id=${req.run_id}`,
		},
	};
}

// ── status ───────────────────────────────────────────────────────────────────
export async function getScheduledGovernedDreamStatus(
	env: Env,
	runId: string,
): Promise<Record<string, unknown> | null> {
	const redis = createRedisClient(env);
	return parseJson(await redis.get(statusKey(runId)));
}

// ── live apply gate (pure, directly tested) ──────────────────────────────────
export interface LiveApplyGateInput {
	liveEnabled: boolean;
	requestMode: string;
	statusExecutedMode: string;
	request: {
		run_id: string;
		orchestrator_run_id: string;
		fencing_token: number;
		run_date: string;
	};
	dateLock: {
		mode?: unknown;
		dream_run_id?: unknown;
		orchestrator_run_id?: unknown;
		fencing_token?: unknown;
		run_date?: unknown;
	} | null;
}

export type GateRejection =
	| "rejected_live_disabled"
	| "rejected_shadow_mode"
	| "rejected_fence_mismatch"
	| "rejected_superseded";

export interface GateResult {
	allowed: boolean;
	rejection: GateRejection | null;
}

export function verifyScheduledGovernedLiveApplyGate(input: LiveApplyGateInput): GateResult {
	const reject = (rejection: GateRejection): GateResult => ({ allowed: false, rejection });
	if (!input.liveEnabled) return reject("rejected_live_disabled");
	if (input.requestMode !== "live") return reject("rejected_shadow_mode");
	if (!input.dateLock) return reject("rejected_superseded");
	if (input.dateLock.mode !== "live") return reject("rejected_shadow_mode");
	if (input.dateLock.dream_run_id !== input.request.run_id) return reject("rejected_superseded");
	if (input.dateLock.orchestrator_run_id !== input.request.orchestrator_run_id) {
		return reject("rejected_superseded");
	}
	if (input.dateLock.fencing_token !== input.request.fencing_token) {
		return reject("rejected_fence_mismatch");
	}
	if (input.dateLock.run_date !== input.request.run_date) return reject("rejected_superseded");
	if (input.statusExecutedMode !== "live") return reject("rejected_shadow_mode");
	return { allowed: true, rejection: null };
}

// ── executor (idempotent; shadow can never apply) ────────────────────────────
export async function executeScheduledGovernedDreamAsync(
	env: Env,
	request: ScheduledDreamStartRequest & { executed_mode: string },
): Promise<void> {
	const redis = createRedisClient(env);
	const dreamRunId = request.run_id;
	const base = (await getScheduledGovernedDreamStatus(env, dreamRunId)) ?? {};

	// Idempotency: if already terminal, do nothing.
	if (base.state === "terminal") return;

	const doc: Record<string, unknown> = {
		...base,
		schema_version: SCHEMA_VERSION,
		dream_run_id: dreamRunId,
		orchestrator_run_id: request.orchestrator_run_id,
		run_date: request.run_date,
		requested_mode: request.mode,
		executed_mode: request.executed_mode,
		fencing_token: request.fencing_token,
		started_at: base.started_at ?? new Date().toISOString(),
		errors: [],
		warnings: [],
		applied_count: 0,
		held_count: 0,
	};

	const fail = async (terminalStatus: string, message: string): Promise<void> => {
		doc.state = "terminal";
		doc.status = terminalStatus;
		doc.executed_mode = request.executed_mode; // required field even on failure
		doc.applied_count = 0;
		doc.completed_at = new Date().toISOString();
		doc.errors = [message];
		doc.next_action = "Inspect the Worker logs; the orchestrator will treat this as terminal.";
		await setStatus(redis, doc);
		await redis.set(LAST_COMPLETED_KEY, String(doc.completed_at));
	};

	try {
		doc.state = "running_proposal";
		await setStatus(redis, doc);
		const proposal = await runDreamProposal(env, {
			trigger: "manual",
			actorId: "scheduled:dream-governance",
			archiveLimit: SCHEDULED_DREAM_ARCHIVE_LIMIT,
			promotionLimit: SCHEDULED_DREAM_PROMOTION_LIMIT,
			note: `Async scheduled-governed Dream (${request.executed_mode}). cron=${request.cron} scheduled_time=${request.scheduled_time}`,
		});
		const proposalId = typeof proposal.run_id === "string" ? proposal.run_id : null;
		const proposalStatus = typeof proposal.status === "string" ? proposal.status : null;
		const operations = Array.isArray(proposal.operations)
			? (proposal.operations as Array<Record<string, unknown>>)
			: [];
		doc.proposal_id = proposalId;
		doc.proposal_status = proposalStatus;
		doc.risk_score = typeof proposal.risk_score === "string" ? proposal.risk_score : null;

		if (!proposalId || proposalStatus !== "proposal_ready") {
			doc.state = "terminal";
			doc.status = "failed";
			doc.completed_at = new Date().toISOString();
			doc.counts = { operation_count: operations.length, selected_operation_count: 0,
				held_operation_count: operations.length, applied_count: 0 };
			doc.next_action = "Dream proposal did not reach proposal_ready.";
			await setStatus(redis, doc);
			await redis.set(LAST_COMPLETED_KEY, String(doc.completed_at));
			return;
		}

		doc.state = "proposal_ready";
		await setStatus(redis, doc);

		doc.state = "running_grade";
		await setStatus(redis, doc);
		const grade = await gradeDreamProposal(env, {
			proposalId,
			actorId: "scheduled:dream-governance",
			rubricVersion: "scheduled-governed-v1",
		});
		doc.grade_id = typeof grade.grade_id === "string" ? grade.grade_id : null;
		doc.grade_status = typeof grade.status === "string" ? grade.status : null;

		const decision = buildScheduledGovernedDecision(proposal, grade);
		doc.state = "decision_ready";
		doc.decision = {
			selected_operation_ids: decision.selectedOperationIds,
			held_operations: decision.heldOperations,
		};
		doc.counts = {
			operation_count: operations.length,
			selected_operation_count: decision.selectedOperationIds.length,
			held_operation_count: decision.heldOperations.length,
			applied_count: 0,
			archive_limit: SCHEDULED_DREAM_ARCHIVE_LIMIT,
			promotion_limit: SCHEDULED_DREAM_PROMOTION_LIMIT,
			duplicate_merge_limit: SCHEDULED_DREAM_DUPLICATE_MERGE_LIMIT,
			mark_contested_limit: SCHEDULED_DREAM_MARK_CONTESTED_LIMIT,
			operation_counts: decision.operationCounts,
			selected_counts: decision.selectedCounts,
		};
		doc.held_count = decision.heldOperations.length;
		await setStatus(redis, doc);

		// SHADOW: propose + grade + decide, but NEVER apply.
		if (request.executed_mode !== "live") {
			doc.state = "terminal";
			doc.status = "completed_shadow";
			doc.executed_mode = "shadow";
			doc.applied_count = 0;
			doc.completed_at = new Date().toISOString();
			doc.next_action = null;
			await setStatus(redis, doc);
			await redis.set(LAST_COMPLETED_KEY, String(doc.completed_at));
			return;
		}

		// LIVE path (production Phase 2 keeps this disabled at start). Gate first.
		const lock = parseJson(await redis.get(dateLockKey(request.run_date)));
		const gate = verifyScheduledGovernedLiveApplyGate({
			liveEnabled: liveEnabled(env),
			requestMode: request.mode,
			statusExecutedMode: String(doc.executed_mode),
			request: {
				run_id: dreamRunId,
				orchestrator_run_id: request.orchestrator_run_id,
				fencing_token: request.fencing_token,
				run_date: request.run_date,
			},
			dateLock: lock,
		});
		if (!gate.allowed) {
			doc.state = "terminal";
			doc.status = gate.rejection;
			doc.applied_count = 0;
			doc.completed_at = new Date().toISOString();
			doc.next_action = "Live apply gate rejected this run; no mutation performed.";
			await setStatus(redis, doc);
			await redis.set(LAST_COMPLETED_KEY, String(doc.completed_at));
			return;
		}

		doc.state = "running_apply";
		await setStatus(redis, doc);
		let applyResult: Record<string, unknown> | null = null;
		if (decision.selectedOperationIds.length > 0) {
			applyResult = await applyDreamProposal(env, {
				proposalId,
				mutationId: `scheduled_governed_async_${proposalId}`,
				reason: "Async scheduled governed Dream live apply",
				actorId: "scheduled:dream-governance",
				operationIds: decision.selectedOperationIds,
				requireGradePass: true,
				gradeId: typeof grade.grade_id === "string" ? grade.grade_id : null,
				maxEntriesTouched: SCHEDULED_DREAM_MAX_ENTRIES_TOUCHED,
			});
		}
		const verification = verifyScheduledGovernedApply(applyResult, decision.selectedOperationIds);
		const appliedCount = typeof applyResult?.applied_count === "number" ? applyResult.applied_count : 0;
		doc.applied_count = appliedCount;
		doc.verification = verification;
		(doc.counts as Record<string, unknown>).applied_count = appliedCount;
		doc.state = "terminal";
		doc.status = verification.passed !== true
			? "failed"
			: decision.heldOperations.length > 0
				? "completed_with_holds"
				: "completed";
		doc.completed_at = new Date().toISOString();
		await setStatus(redis, doc);
		await redis.set(LAST_COMPLETED_KEY, String(doc.completed_at));
	} catch (error) {
		await fail("failed", error instanceof Error ? error.message : String(error));
	}
}
