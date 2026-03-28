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
});

describe("Scheduled Dream runner", () => {
	it("triggers the nightly bounded live Dream cycle", async () => {
		const controller = createScheduledController({
			cron: "0 3 * * *",
			scheduledTime: Date.parse("2026-03-28T03:00:00.000Z"),
		});
		const ctx = createExecutionContext();

		await worker.scheduled(controller, getTestEnv(), ctx);
		await waitOnExecutionContext(ctx);

		expect(dreamMock.runDreamCycle).toHaveBeenCalledWith(
			expect.objectContaining({
				UPSTASH_REDIS_REST_URL: "https://redis.test.local",
			}),
			expect.objectContaining({
				dryRun: false,
				trigger: "scheduled",
				cron: "0 3 * * *",
				archiveLimit: 5,
				promotionLimit: 10,
				note: "Nightly bounded Dream run with archiveLimit=5 and promotionLimit=10.",
			}),
		);
	});
});
