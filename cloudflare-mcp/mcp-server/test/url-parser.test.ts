import { describe, expect, it, vi } from "vitest";
import {
	normalizeTweetUrl,
	normalizeTweetUrlSync,
} from "../src/tweets/url-parser";

describe("tweet URL parser", () => {
	it.each([
		["https://x.com/jack/status/20", "jack", "20"],
		["https://twitter.com/jack/status/20?s=20", "jack", "20"],
		["https://mobile.x.com/jack/status/20?t=abc", "jack", "20"],
		["https://www.x.com/jack/statuses/20/", "jack", "20"],
		["https://x.com/i/status/20", "i", "20"],
		["https://fxtwitter.com/jack/status/20", "jack", "20"],
		["https://vxtwitter.com/jack/status/20", "jack", "20"],
		["https://fixupx.com/jack/status/20", "jack", "20"],
		["https://nitter.net/jack/status/20#m", "jack", "20"],
	])("normalizes %s", (url, user, id) => {
		const parsed = normalizeTweetUrlSync(url);
		expect(parsed).toMatchObject({
			user,
			id,
			canonical_url: `https://x.com/${user}/status/${id}`,
		});
	});

	it("resolves t.co links and then parses the final X URL", async () => {
		const originalFetch = globalThis.fetch;
		globalThis.fetch = vi.fn(async () => {
			return new Response(null, {
				status: 200,
				headers: {},
			});
		}) as typeof fetch;
		vi.mocked(globalThis.fetch).mockResolvedValueOnce({
			ok: true,
			status: 200,
			url: "https://x.com/jack/status/20?s=20",
		} as Response);

		await expect(
			normalizeTweetUrl("https://t.co/example"),
		).resolves.toMatchObject({
			user: "jack",
			id: "20",
		});
		globalThis.fetch = originalFetch;
	});

	it("rejects non-twitter URLs and junk IDs", () => {
		expect(() =>
			normalizeTweetUrlSync("https://example.com/jack/status/20"),
		).toThrow(/Not a recognizable/);
		expect(() =>
			normalizeTweetUrlSync(
				"https://x.com/jack/status/12345678901234567890123456",
			),
		).toThrow(/recognizable|too long/i);
	});
});
