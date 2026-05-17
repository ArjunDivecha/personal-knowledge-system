import { afterEach, describe, expect, it, vi } from "vitest";
import fxSimple from "./fixtures/fxtwitter-simple.json";
import vxSimple from "./fixtures/vxtwitter-simple.json";
import { fetchTweetWithFallback } from "../src/tweets/fetchers";
import type { NormalizedTweetUrl } from "../src/tweets/types";

const parsed: NormalizedTweetUrl = {
	input_url: "https://x.com/jack/status/20",
	id: "20",
	user: "jack",
	canonical_url: "https://x.com/jack/status/20",
};

afterEach(() => {
	vi.unstubAllGlobals();
});

describe("tweet fallback fetcher", () => {
	it("normalizes FxTwitter payloads", async () => {
		vi.stubGlobal(
			"fetch",
			vi.fn(async () => Response.json(fxSimple)),
		);

		const tweet = await fetchTweetWithFallback(parsed, { timeoutMs: 1000 });
		expect(tweet).toMatchObject({
			id: "20",
			text: "just setting up my twttr",
			source_api: "fxtwitter",
			author: {
				username: "jack",
				verified: true,
			},
			engagement: {
				retweets: 126246,
				bookmarks: 21453,
			},
		});
	});

	it("falls back to VxTwitter after an FxTwitter 5xx", async () => {
		const fetchMock = vi
			.fn()
			.mockResolvedValueOnce(
				Response.json({ code: 500, error: "bad gateway" }, { status: 500 }),
			)
			.mockResolvedValueOnce(Response.json(vxSimple));
		vi.stubGlobal("fetch", fetchMock);

		const tweet = await fetchTweetWithFallback(parsed, { timeoutMs: 1000 });
		expect(tweet.source_api).toBe("vxtwitter");
		expect(tweet.text).toBe("just setting up my twttr");
		expect(fetchMock).toHaveBeenCalledTimes(2);
	});

	it("returns a structured not-found error when all upstreams 404", async () => {
		vi.stubGlobal(
			"fetch",
			vi.fn(async () => Response.json({ error: "missing" }, { status: 404 })),
		);

		await expect(
			fetchTweetWithFallback(parsed, { timeoutMs: 1000 }),
		).rejects.toThrow(/Tweet not found/);
	});
});
