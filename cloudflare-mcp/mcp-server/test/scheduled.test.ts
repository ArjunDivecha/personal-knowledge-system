import { env } from "cloudflare:workers";
import {
	createExecutionContext,
	createScheduledController,
	waitOnExecutionContext,
} from "cloudflare:test";
import { beforeEach, describe, expect, it, vi } from "vitest";

const dreamMock = vi.hoisted(() => ({
	restoreArchivedEntry: vi.fn(),
	runDreamCycle: vi.fn(),
	runDreamProposal: vi.fn(),
}));

vi.mock("../src/dream", () => dreamMock);

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
	dreamMock.runDreamCycle.mockResolvedValue({
		run_id: "dr_test",
		status: "completed",
		dry_run: false,
	});
	dreamMock.runDreamProposal.mockResolvedValue({
		run_id: "dpr_test",
		status: "proposal_ready",
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
	tripwireMock.getEffectiveMode.mockImplementation(async (_redis, envValue) => ({
		effective: envValue ?? "off",
		env_value: envValue ?? "off",
		tripped: false,
		trip_record: null,
	}));
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
				archiveLimit: 10,
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
				archiveLimit: 10,
				promotionLimit: 10,
				note: expect.stringContaining("auto-apply=full"),
			}),
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
			reason: "destructive-action count breached threshold 3.0 for 2 consecutive days",
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
