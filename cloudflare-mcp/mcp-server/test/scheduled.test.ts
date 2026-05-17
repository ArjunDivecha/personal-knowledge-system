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
});
