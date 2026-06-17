/**
 * Phase 2 async scheduled-governed Dream Worker tests.
 *
 * The date-lock Lua and Redis state are exercised through a stateful in-memory
 * fake Redis whose `.eval` reproduces the date-lock script's contract (the real
 * Lua is covered by the staging/prod smoke). Dream proposal/grade/apply are
 * mocked; buildScheduledGovernedDecision/verifyScheduledGovernedApply run for real.
 */
import { createExecutionContext, waitOnExecutionContext } from "cloudflare:test";
import { env } from "cloudflare:workers";
import { beforeEach, describe, expect, it, vi } from "vitest";

const dreamMock = vi.hoisted(() => ({
	applyDreamProposal: vi.fn(),
	gradeDreamProposal: vi.fn(),
	restoreArchivedEntry: vi.fn(),
	runDreamCycle: vi.fn(),
	runDreamProposal: vi.fn(),
}));
vi.mock("../src/dream", () => dreamMock);

// Stateful fake Redis with an .eval that mirrors DATE_LOCK_ACQUIRE_SCRIPT.
const redisState = vi.hoisted(() => ({ map: new Map<string, string>() }));
vi.mock("@upstash/redis/cloudflare", () => ({
	Redis: class FakeRedis {
		async get(key: string) {
			return redisState.map.has(key) ? redisState.map.get(key) : null;
		}
		async set(key: string, value: string) {
			redisState.map.set(key, value);
			return "OK";
		}
		async del(key: string) {
			const had = redisState.map.has(key);
			redisState.map.delete(key);
			return had ? 1 : 0;
		}
		async eval(_script: string, keys: string[], args: string[]) {
			const [statusKey, lockKey] = keys;
			const [dreamRunId, orchRunId, lockJson, acceptedJson] = args;
			const statusExisting = redisState.map.get(statusKey);
			if (statusExisting) {
				return JSON.stringify({ outcome: "duplicate", status: JSON.parse(statusExisting) });
			}
			const lockExisting = redisState.map.get(lockKey);
			if (lockExisting) {
				const l = JSON.parse(lockExisting);
				if (l.dream_run_id === dreamRunId && l.orchestrator_run_id === orchRunId) {
					redisState.map.set(statusKey, acceptedJson);
					return JSON.stringify({ outcome: "duplicate", reattached: true, status: JSON.parse(acceptedJson) });
				}
				return JSON.stringify({
					outcome: "date_locked",
					blocked_by: {
						dream_run_id: l.dream_run_id,
						orchestrator_run_id: l.orchestrator_run_id,
						fencing_token: l.fencing_token,
					},
				});
			}
			redisState.map.set(lockKey, lockJson);
			redisState.map.set(statusKey, acceptedJson);
			return JSON.stringify({ outcome: "accepted", status: JSON.parse(acceptedJson) });
		}
	},
}));

import worker from "../src/index";
import {
	executeScheduledGovernedDreamAsync,
	getScheduledGovernedDreamStatus,
	scheduledDreamStartRequestSchema,
	startScheduledGovernedDreamAsync,
	validateTerminalStatus,
	verifyScheduledGovernedLiveApplyGate,
} from "../src/scheduledDreamAsync";

const TOKEN = "test-dream-operator-token";

function testEnv(overrides: Partial<Env> = {}): Env {
	return {
		...env,
		UPSTASH_REDIS_REST_URL: "https://redis.test.local",
		UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
		UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
		UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
		OPENAI_API_KEY: "test-openai-key",
		GITHUB_TOKEN: "test-github-token",
		DREAM_OPERATOR_TOKEN: TOKEN,
		...overrides,
	};
}

const validBody = (over: Record<string, unknown> = {}) => ({
	run_id: "dga_20260616_ab12cd34",
	orchestrator_run_id: "pksn_20260616_230000_ab12cd34",
	run_date: "2026-06-16",
	mode: "shadow",
	fencing_token: 17,
	cron: "m4-orchestrator",
	scheduled_time: 1781650800000,
	...over,
});

beforeEach(() => {
	vi.clearAllMocks();
	redisState.map.clear();
	dreamMock.runDreamProposal.mockResolvedValue({
		run_id: "dpr_test",
		status: "proposal_ready",
		risk_score: "low",
		operations: [{ operation_id: "dop_archive_ke_1", type: "archive_entry" }],
	});
	dreamMock.gradeDreamProposal.mockResolvedValue({
		grade_id: "dpg_test",
		status: "passed",
		passed: true,
		operation_ids: ["dop_archive_ke_1"],
	});
	dreamMock.applyDreamProposal.mockResolvedValue({
		ok: true,
		applied_count: 1,
		operation_ids: ["dop_archive_ke_1"],
		side_effects: { index: "rebuilt" },
	});
});

describe("schema validation", () => {
	it("rejects a malformed request", () => {
		const bad = scheduledDreamStartRequestSchema.safeParse(validBody({ run_id: "nope" }));
		expect(bad.success).toBe(false);
	});
	it("rejects suffix mismatch between ids", () => {
		const r = scheduledDreamStartRequestSchema.safeParse(
			validBody({ orchestrator_run_id: "pksn_20260616_230000_ffffffff" }),
		);
		expect(r.success).toBe(false);
	});
	it("rejects run_date not matching ids", () => {
		const r = scheduledDreamStartRequestSchema.safeParse(validBody({ run_date: "2026-06-17" }));
		expect(r.success).toBe(false);
	});
	it("start returns 400 on invalid body", async () => {
		const ctx = createExecutionContext();
		const res = await startScheduledGovernedDreamAsync(testEnv(), ctx, { run_id: "nope" });
		expect(res.status).toBe(400);
		expect(res.body.error).toBe("invalid_request");
	});
});

describe("auth (route)", () => {
	it("unauthorized start returns 401", async () => {
		const ctx = createExecutionContext();
		const res = await worker.fetch(
			new Request("https://mcp.test/ops/dream/scheduled_governed/start", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify(validBody()),
			}),
			testEnv(),
			ctx,
		);
		await waitOnExecutionContext(ctx);
		expect(res.status).toBe(401);
	});
	it("status returns 404 for unknown run id (authorized)", async () => {
		const ctx = createExecutionContext();
		const res = await worker.fetch(
			new Request("https://mcp.test/ops/dream/scheduled_governed/status?run_id=dga_20260616_deadbeef", {
				headers: { Authorization: `Bearer ${TOKEN}` },
			}),
			testEnv(),
			ctx,
		);
		await waitOnExecutionContext(ctx);
		expect(res.status).toBe(404);
	});
});

describe("start + executor (shadow)", () => {
	it("writes accepted status before executor work, then completes shadow", async () => {
		const ctx = createExecutionContext();
		const res = await startScheduledGovernedDreamAsync(testEnv(), ctx, validBody());
		expect(res.status).toBe(202);
		expect(res.body.state).toBe("accepted");
		// accepted status is durable before the background executor finishes
		const accepted = await getScheduledGovernedDreamStatus(testEnv(), "dga_20260616_ab12cd34");
		expect(accepted?.status).toBe("accepted");
		await waitOnExecutionContext(ctx);
		const terminal = await getScheduledGovernedDreamStatus(testEnv(), "dga_20260616_ab12cd34");
		expect(terminal?.state).toBe("terminal");
		expect(terminal?.status).toBe("completed_shadow");
		expect(terminal?.executed_mode).toBe("shadow");
		expect(terminal?.applied_count).toBe(0);
		expect(dreamMock.applyDreamProposal).not.toHaveBeenCalled();
		expect(validateTerminalStatus(terminal).ok).toBe(true);
	});

	it("duplicate start with same dream_run_id does not duplicate work", async () => {
		const ctx1 = createExecutionContext();
		await startScheduledGovernedDreamAsync(testEnv(), ctx1, validBody());
		await waitOnExecutionContext(ctx1);
		const ctx2 = createExecutionContext();
		const dup = await startScheduledGovernedDreamAsync(testEnv(), ctx2, validBody());
		await waitOnExecutionContext(ctx2);
		expect(dup.body.duplicate).toBe(true);
		expect(dreamMock.runDreamProposal).toHaveBeenCalledTimes(1);
	});

	it("same-date different-run start is rejected by the date lock", async () => {
		const ctxA = createExecutionContext();
		await startScheduledGovernedDreamAsync(testEnv(), ctxA, validBody());
		await waitOnExecutionContext(ctxA);
		const ctxB = createExecutionContext();
		const res = await startScheduledGovernedDreamAsync(
			testEnv(),
			ctxB,
			validBody({
				run_id: "dga_20260616_99999999",
				orchestrator_run_id: "pksn_20260616_233000_99999999",
				fencing_token: 18,
			}),
		);
		expect(res.status).toBe(409);
		expect(res.body.error).toBe("date_locked");
		expect((res.body.blocked_by as Record<string, unknown>).dream_run_id).toBe("dga_20260616_ab12cd34");
	});

	it("writes terminal failed status when the proposal throws", async () => {
		dreamMock.runDreamProposal.mockRejectedValueOnce(new Error("proposal boom"));
		const ctx = createExecutionContext();
		await startScheduledGovernedDreamAsync(testEnv(), ctx, validBody());
		await waitOnExecutionContext(ctx);
		const terminal = await getScheduledGovernedDreamStatus(testEnv(), "dga_20260616_ab12cd34");
		expect(terminal?.state).toBe("terminal");
		expect(terminal?.status).toBe("failed");
		expect(terminal?.executed_mode).toBe("shadow");
		expect(terminal?.applied_count).toBe(0);
		expect(validateTerminalStatus(terminal).ok).toBe(true);
	});
});

describe("live disabled in production", () => {
	it("rejects live start when PKS_ORCH_DREAM_LIVE_ENABLED is absent", async () => {
		const ctx = createExecutionContext();
		const res = await startScheduledGovernedDreamAsync(testEnv(), ctx, validBody({ mode: "live" }));
		expect(res.status).toBe(403);
		expect(res.body.error).toBe("rejected_live_disabled");
		expect(dreamMock.runDreamProposal).not.toHaveBeenCalled();
	});
});

describe("live apply gate (pure)", () => {
	const lock = {
		mode: "live",
		dream_run_id: "dga_20260616_ab12cd34",
		orchestrator_run_id: "pksn_20260616_230000_ab12cd34",
		fencing_token: 17,
		run_date: "2026-06-16",
	};
	const request = {
		run_id: "dga_20260616_ab12cd34",
		orchestrator_run_id: "pksn_20260616_230000_ab12cd34",
		fencing_token: 17,
		run_date: "2026-06-16",
	};
	const base = {
		liveEnabled: true,
		requestMode: "live",
		statusExecutedMode: "live",
		request,
		dateLock: lock,
	};

	it("allows when everything matches", () => {
		expect(verifyScheduledGovernedLiveApplyGate(base)).toEqual({ allowed: true, rejection: null });
	});
	it("rejects when live disabled", () => {
		expect(verifyScheduledGovernedLiveApplyGate({ ...base, liveEnabled: false }).rejection).toBe(
			"rejected_live_disabled",
		);
	});
	it("rejects wrong mode", () => {
		expect(verifyScheduledGovernedLiveApplyGate({ ...base, requestMode: "shadow" }).rejection).toBe(
			"rejected_shadow_mode",
		);
	});
	it("rejects wrong fence", () => {
		expect(
			verifyScheduledGovernedLiveApplyGate({ ...base, dateLock: { ...lock, fencing_token: 16 } }).rejection,
		).toBe("rejected_fence_mismatch");
	});
	it("rejects wrong run id", () => {
		expect(
			verifyScheduledGovernedLiveApplyGate({ ...base, dateLock: { ...lock, dream_run_id: "dga_20260616_00000000" } })
				.rejection,
		).toBe("rejected_superseded");
	});
	it("rejects wrong date", () => {
		expect(
			verifyScheduledGovernedLiveApplyGate({ ...base, dateLock: { ...lock, run_date: "2026-06-17" } }).rejection,
		).toBe("rejected_superseded");
	});
	it("rejects when no date lock present", () => {
		expect(verifyScheduledGovernedLiveApplyGate({ ...base, dateLock: null }).rejection).toBe(
			"rejected_superseded",
		);
	});
});

describe("terminal status validation helper", () => {
	it("detects missing executed_mode", () => {
		const r = validateTerminalStatus({
			applied_count: 0,
			dream_run_id: "x",
			orchestrator_run_id: "y",
			run_date: "2026-06-16",
			requested_mode: "shadow",
			state: "terminal",
			status: "completed_shadow",
		});
		expect(r.ok).toBe(false);
		expect(r.missing).toContain("executed_mode");
	});
	it("detects missing applied_count", () => {
		const r = validateTerminalStatus({
			executed_mode: "shadow",
			dream_run_id: "x",
			orchestrator_run_id: "y",
			run_date: "2026-06-16",
			requested_mode: "shadow",
			state: "terminal",
			status: "completed_shadow",
		});
		expect(r.ok).toBe(false);
		expect(r.missing).toContain("applied_count");
	});
	it("accepts a complete terminal status (applied_count 0 is valid)", () => {
		const r = validateTerminalStatus({
			executed_mode: "shadow",
			applied_count: 0,
			dream_run_id: "x",
			orchestrator_run_id: "y",
			run_date: "2026-06-16",
			requested_mode: "shadow",
			state: "terminal",
			status: "completed_shadow",
		});
		expect(r.ok).toBe(true);
	});
});
