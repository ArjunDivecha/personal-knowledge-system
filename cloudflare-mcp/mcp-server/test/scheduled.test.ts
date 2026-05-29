import {
	createExecutionContext,
	createScheduledController,
	waitOnExecutionContext,
} from "cloudflare:test";
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

const redisMock = vi.hoisted(() => ({
	del: vi.fn(),
	get: vi.fn(),
	set: vi.fn(),
}));

vi.mock("@upstash/redis/cloudflare", () => ({
	Redis: class MockRedis {
		del(...args: Parameters<typeof redisMock.del>) {
			return redisMock.del(...args);
		}

		get(...args: Parameters<typeof redisMock.get>) {
			return redisMock.get(...args);
		}

		set(...args: Parameters<typeof redisMock.set>) {
			return redisMock.set(...args);
		}
	},
}));

// Mock the tripwire module so the scheduled handler doesn't make real
// Redis calls during tests. We test the tripwires themselves in
// test/tripwires.test.ts with an in-memory fake Redis.
const tripwireMock = vi.hoisted(() => ({
	checkDestructiveTripwire: vi.fn(),
	checkRetrievalTripwire: vi.fn(),
	getEffectiveMode: vi.fn(),
	recordSearchQuery: vi.fn(),
	setKillFlag: vi.fn(),
}));

vi.mock("../src/tripwires", () => tripwireMock);

import worker from "../src/index";

function getTestEnv(): Env {
	return {
		...env,
		UPSTASH_REDIS_REST_URL: "https://redis.test.local",
		UPSTASH_REDIS_REST_TOKEN: "test-redis-token",
		UPSTASH_VECTOR_REST_URL: "https://vector.test.local",
		UPSTASH_VECTOR_REST_TOKEN: "test-vector-token",
		OPENAI_API_KEY: "test-openai-key",
		GITHUB_TOKEN: "test-github-token",
		DREAM_OPERATOR_TOKEN: "test-dream-operator-token",
	};
}

beforeEach(() => {
	vi.clearAllMocks();
	redisMock.del.mockResolvedValue(1);
	redisMock.get.mockResolvedValue(null);
	redisMock.set.mockResolvedValue("OK");
	dreamMock.runDreamCycle.mockResolvedValue({
		run_id: "dr_test",
		status: "completed",
		dry_run: false,
	});
	dreamMock.runDreamProposal.mockResolvedValue({
		run_id: "dpr_test",
		status: "proposal_ready",
		risk_score: "low",
		operations: [],
	});
	dreamMock.gradeDreamProposal.mockResolvedValue({
		grade_id: "dpg_test",
		proposal_id: "dpr_test",
		status: "passed",
		passed: true,
		operation_ids: [],
	});
	dreamMock.applyDreamProposal.mockResolvedValue({
		ok: true,
		proposal_id: "dpr_test",
		apply_run_id: "apply_dpr_test",
		mutation_id: "scheduled_governed_dpr_test",
		applied_count: 0,
		operation_ids: [],
		side_effects: { index: "rebuilt" },
	});
	tripwireMock.checkDestructiveTripwire.mockResolvedValue({
		tripped: false,
		day_counts: [],
		baseline_median: 0,
		threshold: 0,
		consecutive_breaches: 0,
		reason: null,
	});
	tripwireMock.checkRetrievalTripwire.mockResolvedValue({
		tripped: false,
		day_ratios: [],
		baseline_median_ratio: 0,
		threshold_ratio: 0,
		consecutive_breaches: 0,
		reason: null,
	});
	// Default: getEffectiveMode echoes the env value (no kill flag).
	tripwireMock.getEffectiveMode.mockImplementation(
		async (_redis, envValue) => ({
			effective: envValue ?? "off",
			env_value: envValue ?? "off",
			tripped: false,
			trip_record: null,
		}),
	);
});

describe("Scheduled Dream runner", () => {
	it("default (DREAM_AUTO_APPLY_MODE unset): proposal-only via runDreamProposal", async () => {
		const controller = createScheduledController({
			cron: "10 7 * * *",
			scheduledTime: Date.parse("2026-03-28T07:10:00.000Z"),
		});
		const ctx = createExecutionContext();

		await worker.scheduled(controller, getTestEnv(), ctx);
		await waitOnExecutionContext(ctx);

		expect(dreamMock.runDreamProposal).toHaveBeenCalledWith(
			expect.objectContaining({
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
			}),
			expect.objectContaining({
				trigger: "manual",
				actorId: "scheduled:dream-governance",
				archiveLimit: 50,
				promotionLimit: 10,
				note: "Nightly Dream governance proposal. cron=10 7 * * * scheduled_time=1774681800000",
			}),
		);
		expect(dreamMock.runDreamCycle).not.toHaveBeenCalled();
	});

	it("DREAM_AUTO_APPLY_MODE=off: still proposal-only", async () => {
		const controller = createScheduledController({
			cron: "10 7 * * *",
			scheduledTime: Date.parse("2026-03-28T07:10:00.000Z"),
		});
		const ctx = createExecutionContext();

		const testEnv = { ...getTestEnv(), DREAM_AUTO_APPLY_MODE: "off" as const };
		await worker.scheduled(controller, testEnv, ctx);
		await waitOnExecutionContext(ctx);

		expect(dreamMock.runDreamProposal).toHaveBeenCalledTimes(1);
		expect(dreamMock.runDreamCycle).not.toHaveBeenCalled();
	});

	it("DREAM_AUTO_APPLY_MODE=full: live cycle via runDreamCycle", async () => {
		const controller = createScheduledController({
			cron: "10 7 * * *",
			scheduledTime: Date.parse("2026-03-28T07:10:00.000Z"),
		});
		const ctx = createExecutionContext();

		const testEnv = { ...getTestEnv(), DREAM_AUTO_APPLY_MODE: "full" as const };
		await worker.scheduled(controller, testEnv, ctx);
		await waitOnExecutionContext(ctx);

		expect(dreamMock.runDreamProposal).not.toHaveBeenCalled();
		expect(dreamMock.runDreamCycle).toHaveBeenCalledWith(
			expect.objectContaining({
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
			}),
			expect.objectContaining({
				dryRun: false,
				trigger: "scheduled",
				cron: "10 7 * * *",
				scheduledTime: 1774681800000,
				archiveLimit: 50,
				promotionLimit: 10,
				note: expect.stringContaining("auto-apply=full"),
			}),
		);
	});

	it("DREAM_AUTO_APPLY_MODE=governed: proposal, grade, bounded apply, and run record", async () => {
		dreamMock.runDreamProposal.mockResolvedValueOnce({
			run_id: "dpr_2026-03-28T07-10-00-000Z",
			status: "proposal_ready",
			risk_score: "medium",
			operations: [
				{ operation_id: "dop_archive_ke_1", type: "archive_entry" },
				{ operation_id: "dop_merge_ke_2", type: "duplicate_merge" },
			],
		});
		dreamMock.gradeDreamProposal.mockResolvedValueOnce({
			grade_id: "dpg_2026-03-28T07-10-01-000Z",
			status: "passed",
			passed: true,
			operation_ids: ["dop_archive_ke_1", "dop_merge_ke_2"],
		});
		dreamMock.applyDreamProposal.mockResolvedValueOnce({
			ok: true,
			proposal_id: "dpr_2026-03-28T07-10-00-000Z",
			apply_run_id: "apply_dpr_2026-03-28T07-10-00-000Z",
			mutation_id: "scheduled_governed_dpr_2026-03-28T07-10-00-000Z",
			applied_count: 2,
			operation_ids: ["dop_archive_ke_1", "dop_merge_ke_2"],
			side_effects: { index: "rebuilt" },
		});
		const controller = createScheduledController({
			cron: "10 7 * * *",
			scheduledTime: Date.parse("2026-03-28T07:10:00.000Z"),
		});
		const ctx = createExecutionContext();

		const testEnv = {
			...getTestEnv(),
			DREAM_AUTO_APPLY_MODE: "governed" as const,
		};
		await worker.scheduled(controller, testEnv, ctx);
		await waitOnExecutionContext(ctx);

		expect(dreamMock.runDreamCycle).not.toHaveBeenCalled();
		expect(dreamMock.runDreamProposal).toHaveBeenCalledWith(
			expect.anything(),
			expect.objectContaining({
				actorId: "scheduled:dream-governance",
				archiveLimit: 50,
				promotionLimit: 10,
				note: expect.stringContaining("Nightly Dream governed proposal"),
			}),
		);
		expect(dreamMock.gradeDreamProposal).toHaveBeenCalledWith(
			expect.anything(),
			expect.objectContaining({
				proposalId: "dpr_2026-03-28T07-10-00-000Z",
				actorId: "scheduled:dream-governance",
				rubricVersion: "scheduled-governed-v1",
			}),
		);
		expect(dreamMock.applyDreamProposal).toHaveBeenCalledWith(
			expect.anything(),
			expect.objectContaining({
				proposalId: "dpr_2026-03-28T07-10-00-000Z",
				mutationId: "scheduled_governed_dpr_2026-03-28T07-10-00-000Z",
				actorId: "scheduled:dream-governance",
				operationIds: ["dop_archive_ke_1", "dop_merge_ke_2"],
				requireGradePass: true,
				gradeId: "dpg_2026-03-28T07-10-01-000Z",
			}),
		);
		const lastRunWrite = redisMock.set.mock.calls.find(
			(call) => call[0] === "dream:last_run",
		);
		expect(lastRunWrite).toBeTruthy();
		const lastRun = JSON.parse(lastRunWrite?.[1] as string) as Record<
			string,
			unknown
		>;
		expect(lastRun).toMatchObject({
			status: "completed",
			auto_apply_mode: "governed",
			proposal_id: "dpr_2026-03-28T07-10-00-000Z",
			grade_status: "passed",
		});
		const lastRunCounts = lastRun.counts as Record<string, unknown>;
		expect(lastRun.counts).toMatchObject({
			selected_operation_count: 2,
			held_operation_count: 0,
			applied_count: 2,
		});
		expect(lastRunCounts.applied_count).toBe(2);
		const lastRunVerification = lastRun.verification as Record<string, unknown>;
		expect(lastRunVerification.passed).toBe(true);
	});

	it("DREAM_AUTO_APPLY_MODE=governed: skips when another Dream run holds the lock", async () => {
		redisMock.set.mockResolvedValueOnce(null);
		redisMock.get.mockResolvedValueOnce({
			run_id: "dr_existing",
			run_at: new Date().toISOString(),
		});
		const controller = createScheduledController({
			cron: "10 7 * * *",
			scheduledTime: Date.parse("2026-03-28T07:10:00.000Z"),
		});
		const ctx = createExecutionContext();

		const testEnv = {
			...getTestEnv(),
			DREAM_AUTO_APPLY_MODE: "governed" as const,
		};
		await worker.scheduled(controller, testEnv, ctx);
		await waitOnExecutionContext(ctx);

		expect(dreamMock.runDreamProposal).not.toHaveBeenCalled();
		expect(dreamMock.gradeDreamProposal).not.toHaveBeenCalled();
		expect(dreamMock.applyDreamProposal).not.toHaveBeenCalled();
		const lastAttemptWrite = redisMock.set.mock.calls.find(
			(call) => call[0] === "dream:last_attempt",
		);
		expect(lastAttemptWrite).toBeTruthy();
		const lastAttempt = JSON.parse(lastAttemptWrite?.[1] as string) as Record<
			string,
			unknown
		>;
		expect(lastAttempt).toMatchObject({
			status: "skipped_locked",
			blocked_by: "dr_existing",
			auto_apply_mode: "governed",
		});
	});

	it("DREAM_AUTO_APPLY_MODE=governed: holds high-risk proposals without apply", async () => {
		dreamMock.runDreamProposal.mockResolvedValueOnce({
			run_id: "dpr_high_risk",
			status: "proposal_ready",
			risk_score: "high",
			operations: [{ operation_id: "dop_archive_ke_1", type: "archive_entry" }],
		});
		dreamMock.gradeDreamProposal.mockResolvedValueOnce({
			grade_id: "dpg_high_risk",
			status: "passed",
			passed: true,
			operation_ids: ["dop_archive_ke_1"],
		});
		const controller = createScheduledController({
			cron: "10 7 * * *",
			scheduledTime: Date.parse("2026-03-28T07:10:00.000Z"),
		});
		const ctx = createExecutionContext();

		const testEnv = {
			...getTestEnv(),
			DREAM_AUTO_APPLY_MODE: "governed" as const,
		};
		await worker.scheduled(controller, testEnv, ctx);
		await waitOnExecutionContext(ctx);

		expect(dreamMock.applyDreamProposal).not.toHaveBeenCalled();
		const lastAttemptWrite = redisMock.set.mock.calls.find(
			(call) => call[0] === "dream:last_attempt",
		);
		expect(lastAttemptWrite).toBeTruthy();
		const lastAttempt = JSON.parse(lastAttemptWrite?.[1] as string) as Record<
			string,
			unknown
		>;
		expect(lastAttempt.status).toBe("held");
		expect(lastAttempt.held_operations).toEqual([
			expect.objectContaining({
				operation_id: "dop_archive_ke_1",
				reason: "risk_score_not_auto_applicable:high",
			}),
		]);
		expect(redisMock.set).not.toHaveBeenCalledWith(
			"dream:last_run",
			expect.anything(),
		);
	});

	it("DREAM_AUTO_APPLY_MODE=full + destructive tripwire fired: falls back to proposal", async () => {
		// Tripwire says spike happened.
		tripwireMock.checkDestructiveTripwire.mockResolvedValueOnce({
			tripped: true,
			day_counts: [],
			baseline_median: 1,
			threshold: 3,
			consecutive_breaches: 2,
			reason:
				"destructive-action count breached threshold 3.0 for 2 consecutive days",
		});
		// And effective-mode resolves to off because the kill flag is now set.
		tripwireMock.getEffectiveMode.mockResolvedValueOnce({
			effective: "off",
			env_value: "full",
			tripped: true,
			trip_record: {
				tripped_at: "2026-03-28T07:00:00.000Z",
				reason: "spike",
				source_tripwire: "destructive_spike",
			},
		});

		const controller = createScheduledController({
			cron: "10 7 * * *",
			scheduledTime: Date.parse("2026-03-28T07:10:00.000Z"),
		});
		const ctx = createExecutionContext();
		const testEnv = { ...getTestEnv(), DREAM_AUTO_APPLY_MODE: "full" as const };
		await worker.scheduled(controller, testEnv, ctx);
		await waitOnExecutionContext(ctx);

		// Even with env=full, the cycle does NOT auto-apply when tripwire is active.
		expect(dreamMock.runDreamCycle).not.toHaveBeenCalled();
		expect(dreamMock.runDreamProposal).toHaveBeenCalled();
		// Tripwire setKillFlag was invoked for DREAM_AUTO_APPLY_MODE.
		expect(tripwireMock.setKillFlag).toHaveBeenCalledWith(
			expect.anything(),
			"DREAM_AUTO_APPLY_MODE",
			expect.objectContaining({ source_tripwire: "destructive_spike" }),
		);
	});
});
