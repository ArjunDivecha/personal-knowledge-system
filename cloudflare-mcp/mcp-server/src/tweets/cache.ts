import type { Redis } from "@upstash/redis/cloudflare";
import type { NormalizedTweetUrl, ReadTweetOutput } from "./types";

const CACHE_PREFIX = "tweet_reader:cache:v2:";
const QUEUE_KEY = "tweet_reader:queue";

export function getTweetCacheKey(id: string, includeMediaAlt: boolean): string {
	return `${CACHE_PREFIX}${id}:alt:${includeMediaAlt ? "1" : "0"}`;
}

export async function getCachedTweet(
	redis: Redis,
	normalizedUrl: NormalizedTweetUrl,
	includeMediaAlt: boolean,
): Promise<ReadTweetOutput | null> {
	const raw = await redis.get(
		getTweetCacheKey(normalizedUrl.id, includeMediaAlt),
	);
	if (!raw) return null;
	if (typeof raw === "string") {
		try {
			return JSON.parse(raw) as ReadTweetOutput;
		} catch {
			return null;
		}
	}
	if (typeof raw === "object") {
		return raw as ReadTweetOutput;
	}
	return null;
}

export async function setCachedTweet(
	redis: Redis,
	tweet: ReadTweetOutput,
	includeMediaAlt: boolean,
	ttlSeconds: number,
): Promise<void> {
	await redis.set(
		getTweetCacheKey(tweet.id, includeMediaAlt),
		JSON.stringify(tweet),
		{ ex: ttlSeconds },
	);
}

export async function enqueueTweetRead(
	redis: Redis,
	tweet: ReadTweetOutput,
	normalizedUrl: NormalizedTweetUrl,
	cacheStatus: "hit" | "miss",
): Promise<void> {
	await redis.lpush(
		QUEUE_KEY,
		JSON.stringify({
			event: "tweet_read",
			read_at: new Date().toISOString(),
			cache_status: cacheStatus,
			id: tweet.id,
			url: tweet.url,
			input_url: normalizedUrl.input_url,
			canonical_url: normalizedUrl.canonical_url,
			author_username: tweet.author.username,
			source_api: tweet.source_api,
			is_long_form_article: tweet.is_long_form_article,
		}),
	);
}
