import { flattenArticle } from "./article-flattener";
import {
	type NormalizedTweetUrl,
	type ReadThreadOutput,
	type ReadTweetOutput,
	TweetReaderError,
	type TweetFetcherOptions,
	type TweetMedia,
	type TweetSourceApi,
	TweetUpstreamError,
} from "./types";

const DEFAULT_TIMEOUT_MS = 4000;
const UPSTREAM_HEADERS = {
	accept: "application/json",
	"accept-language": "en-US,en;q=0.9",
	"user-agent":
		"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
};

type JsonRecord = Record<string, unknown>;

type UpstreamAttempt = {
	source: TweetSourceApi;
	status: TweetUpstreamError["status"];
	message: string;
};

function asRecord(value: unknown): JsonRecord | null {
	if (!value || typeof value !== "object" || Array.isArray(value)) return null;
	return value as JsonRecord;
}

function asArray(value: unknown): unknown[] {
	return Array.isArray(value) ? value : [];
}

function asString(value: unknown): string | null {
	return typeof value === "string" && value.length > 0 ? value : null;
}

function asNumber(value: unknown): number {
	if (typeof value === "number" && Number.isFinite(value)) return value;
	if (typeof value === "string" && value.trim() !== "") {
		const parsed = Number(value);
		return Number.isFinite(parsed) ? parsed : 0;
	}
	return 0;
}

function asBoolean(value: unknown): boolean {
	return typeof value === "boolean" ? value : false;
}

function pickRecord(...values: unknown[]): JsonRecord | null {
	for (const value of values) {
		const record = asRecord(value);
		if (record) return record;
	}
	return null;
}

function toIsoDate(value: unknown, epochSeconds?: unknown): string {
	const epoch = asNumber(epochSeconds);
	if (epoch > 0) return new Date(epoch * 1000).toISOString();
	const raw = asString(value);
	if (!raw) return "";
	const parsed = new Date(raw);
	return Number.isNaN(parsed.getTime()) ? raw : parsed.toISOString();
}

function canonicalUrl(username: string, id: string): string {
	return `https://x.com/${encodeURIComponent(username || "i")}/status/${id}`;
}

function getAuthor(
	record: JsonRecord,
	fallbackUser: string,
): ReadTweetOutput["author"] {
	const author = asRecord(record.author) ?? {};
	const verification = asRecord(author.verification);
	const username =
		asString(author.screen_name) ??
		asString(author.username) ??
		asString(record.user_name) ??
		asString(record.user_screen_name) ??
		fallbackUser;
	return {
		username,
		display_name:
			asString(author.name) ?? asString(author.display_name) ?? username,
		verified: asBoolean(verification?.verified) || asBoolean(author.verified),
		followers: asNumber(author.followers),
	};
}

function bestVideoUrl(media: JsonRecord): string | null {
	const direct = asString(media.url) ?? asString(media.transcode_url);
	const formats = asArray(media.formats)
		.map((item) => asRecord(item))
		.filter((item): item is JsonRecord => Boolean(item))
		.sort((a, b) => asNumber(b.bitrate) - asNumber(a.bitrate));
	return asString(formats[0]?.url) ?? direct;
}

function normalizeFxMedia(
	mediaValue: unknown,
	includeAlt: boolean,
): TweetMedia[] {
	const media = asRecord(mediaValue);
	if (!media) return [];
	const items = [
		...asArray(media.photos),
		...asArray(media.videos),
		...asArray(media.all),
	]
		.map((item) => asRecord(item))
		.filter((item): item is JsonRecord => Boolean(item));

	const seen = new Set<string>();
	const result: TweetMedia[] = [];
	for (const item of items) {
		const rawType = (asString(item.type) ?? "").toLowerCase();
		const type: TweetMedia["type"] =
			rawType === "photo" || rawType === "image"
				? "image"
				: rawType === "gif"
					? "gif"
					: "video";
		const url =
			type === "video" || type === "gif"
				? bestVideoUrl(item)
				: asString(item.url);
		if (!url || seen.has(url)) continue;
		seen.add(url);
		const normalized: TweetMedia = { type, url };
		if (includeAlt) {
			const alt = asString(item.altText) ?? asString(item.alt_text);
			if (alt) normalized.alt_text = alt;
		}
		result.push(normalized);
	}
	return result;
}

function normalizeVxMedia(
	record: JsonRecord,
	includeAlt: boolean,
): TweetMedia[] {
	const extended = asArray(record.media_extended)
		.map((item) => asRecord(item))
		.filter((item): item is JsonRecord => Boolean(item));
	if (extended.length > 0) {
		return extended.flatMap((item) => {
			const rawType = (asString(item.type) ?? "").toLowerCase();
			const type: TweetMedia["type"] =
				rawType === "photo" || rawType === "image"
					? "image"
					: rawType === "gif"
						? "gif"
						: "video";
			const url = bestVideoUrl(item) ?? asString(item.url);
			if (!url) return [];
			const media: TweetMedia = { type, url };
			if (includeAlt) {
				const alt = asString(item.altText) ?? asString(item.alt_text);
				if (alt) media.alt_text = alt;
			}
			return [media];
		});
	}
	return asArray(record.mediaURLs)
		.filter(
			(item): item is string => typeof item === "string" && item.length > 0,
		)
		.map((url) => ({ type: "image", url }));
}

function communityNoteText(value: unknown): string | undefined {
	if (typeof value === "string" && value.trim().length > 0) return value;
	const note = asRecord(value);
	return asString(note?.text) ?? asString(note?.summary) ?? undefined;
}

function normalizeFxStatus(
	statusValue: unknown,
	source: TweetSourceApi,
	fallbackUser: string,
	includeAlt: boolean,
	quoteDepth = 0,
): ReadTweetOutput {
	const status = asRecord(statusValue);
	if (!status) {
		throw new TweetReaderError(
			"upstream_error",
			"Tweet payload did not include a status object.",
		);
	}
	if (status.type === "tombstone") {
		const reason = asString(status.reason);
		if (reason === "private") {
			throw new TweetReaderError(
				"protected",
				"Account is protected; cannot read without auth.",
				{
					status: 403,
				},
			);
		}
		throw new TweetReaderError(
			"not_found",
			"Tweet not found - may be deleted, protected, or the URL is wrong.",
			{ status: 404 },
		);
	}

	const id = asString(status.id) ?? "";
	const author = getAuthor(status, fallbackUser);
	const article = flattenArticle(status.article);
	const output: ReadTweetOutput = {
		id,
		url: asString(status.url) ?? canonicalUrl(author.username, id),
		author,
		created_at: toIsoDate(status.created_at, status.created_timestamp),
		text: asString(status.text) ?? asString(status.raw_text) ?? "",
		is_long_form_article: Boolean(article),
		media: normalizeFxMedia(status.media, includeAlt),
		engagement: {
			replies: asNumber(status.replies),
			retweets: asNumber(status.reposts ?? status.retweets),
			likes: asNumber(status.likes),
			quotes: asNumber(status.quotes),
			bookmarks: asNumber(status.bookmarks),
			views: asNumber(status.views),
		},
		source_api: source,
	};
	if (article) output.article = article;
	const note = communityNoteText(status.community_note);
	if (note) output.community_note = note;

	const quote = asRecord(status.quote ?? status.quoted_tweet);
	if (quote && quoteDepth < 1 && quote.type !== "tombstone") {
		output.quoted_tweet = normalizeFxStatus(
			quote,
			source,
			fallbackUser,
			includeAlt,
			quoteDepth + 1,
		);
	}

	return output;
}

function normalizeVxPayload(
	payload: unknown,
	fallbackUser: string,
	includeAlt: boolean,
	quoteDepth = 0,
): ReadTweetOutput {
	const record = asRecord(payload);
	if (!record) {
		throw new TweetReaderError(
			"upstream_error",
			"VxTwitter returned an invalid JSON payload.",
		);
	}
	const id = asString(record.tweetID) ?? asString(record.id) ?? "";
	const username =
		asString(record.user_name) ??
		asString(record.user_screen_name) ??
		fallbackUser;
	const article = flattenArticle(record.article);
	const output: ReadTweetOutput = {
		id,
		url: asString(record.tweetURL) ?? canonicalUrl(username, id),
		author: {
			username,
			display_name: asString(record.user_screen_name) ?? username,
			verified: asBoolean(record.user_verified),
			followers: asNumber(record.followers),
		},
		created_at: toIsoDate(record.date, record.date_epoch),
		text: asString(record.text) ?? "",
		is_long_form_article: Boolean(article),
		media: normalizeVxMedia(record, includeAlt),
		engagement: {
			replies: asNumber(record.replies),
			retweets: asNumber(record.retweets),
			likes: asNumber(record.likes),
			quotes: asNumber(record.quotes),
			bookmarks: asNumber(record.bookmarks),
			views: asNumber(record.views),
		},
		source_api: "vxtwitter",
	};
	if (article) output.article = article;
	const note = communityNoteText(record.communityNote);
	if (note) output.community_note = note;

	const quote = pickRecord(record.qrt, record.quoted_tweet);
	if (quote && quoteDepth < 1) {
		output.quoted_tweet = normalizeVxPayload(
			quote,
			fallbackUser,
			includeAlt,
			quoteDepth + 1,
		);
	}
	return output;
}

function normalizeAdhxPayload(
	payload: unknown,
	fallbackUser: string,
): ReadTweetOutput {
	const record = asRecord(payload);
	if (!record) {
		throw new TweetReaderError(
			"upstream_error",
			"ADHX returned an invalid JSON payload.",
		);
	}
	const id = asString(record.id) ?? "";
	const authorRecord = asRecord(record.author) ?? {};
	const username =
		asString(authorRecord.username) ??
		asString(authorRecord.name) ??
		asString(record.username) ??
		fallbackUser;
	const article = flattenArticle(record.article);
	return {
		id,
		url: asString(record.url) ?? canonicalUrl(username, id),
		author: {
			username,
			display_name: asString(authorRecord.name) ?? username,
			verified: asBoolean(authorRecord.verified),
			followers: asNumber(authorRecord.followers),
		},
		created_at: toIsoDate(record.createdAt ?? record.created_at),
		text: asString(record.text) ?? "",
		is_long_form_article: Boolean(article),
		article: article ?? undefined,
		media: normalizeFxMedia(record.media, true),
		engagement: {
			replies: asNumber(asRecord(record.engagement)?.replies ?? record.replies),
			retweets: asNumber(
				asRecord(record.engagement)?.retweets ?? record.retweets,
			),
			likes: asNumber(asRecord(record.engagement)?.likes ?? record.likes),
			quotes: asNumber(asRecord(record.engagement)?.quotes ?? record.quotes),
			bookmarks: asNumber(
				asRecord(record.engagement)?.bookmarks ?? record.bookmarks,
			),
			views: asNumber(asRecord(record.engagement)?.views ?? record.views),
		},
		source_api: "adhx",
	};
}

async function fetchJson(
	url: string,
	source: TweetSourceApi,
	timeoutMs: number,
): Promise<{ status: number; data: unknown }> {
	const controller = new AbortController();
	const timeout = setTimeout(() => controller.abort(), timeoutMs);
	try {
		const response = await fetch(url, {
			headers: UPSTREAM_HEADERS,
			signal: controller.signal,
		});
		let data: unknown = null;
		try {
			data = await response.json();
		} catch {
			data = null;
		}
		if (!response.ok) {
			throw new TweetUpstreamError(
				source,
				response.status,
				`HTTP ${response.status}`,
			);
		}
		return { status: response.status, data };
	} catch (error) {
		if (error instanceof TweetUpstreamError) throw error;
		if (error instanceof DOMException && error.name === "AbortError") {
			throw new TweetUpstreamError(source, "timeout", "request timed out");
		}
		throw new TweetUpstreamError(
			source,
			"network",
			error instanceof Error ? error.message : String(error),
		);
	} finally {
		clearTimeout(timeout);
	}
}

function ensureFxSuccess(data: unknown): void {
	const record = asRecord(data);
	const code = asNumber(record?.code);
	if (code && code !== 200) {
		const status =
			code === 404 ? 404 : code === 403 || code === 401 ? 403 : 500;
		throw new TweetUpstreamError("fxtwitter", status, `FxTwitter code ${code}`);
	}
}

export async function fetchFxTweet(
	normalizedUrl: NormalizedTweetUrl,
	options: TweetFetcherOptions = {},
): Promise<ReadTweetOutput> {
	const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
	const url = `https://api.fxtwitter.com/2/status/${normalizedUrl.id}?about_account=1`;
	const { data } = await fetchJson(url, "fxtwitter", timeoutMs);
	ensureFxSuccess(data);
	const record = asRecord(data);
	return normalizeFxStatus(
		record?.status ?? record?.tweet ?? data,
		"fxtwitter",
		normalizedUrl.user,
		options.includeMediaAlt ?? true,
	);
}

export async function fetchVxTweet(
	normalizedUrl: NormalizedTweetUrl,
	options: TweetFetcherOptions = {},
): Promise<ReadTweetOutput> {
	const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
	const url = `https://api.vxtwitter.com/${encodeURIComponent(normalizedUrl.user)}/status/${normalizedUrl.id}`;
	const { data } = await fetchJson(url, "vxtwitter", timeoutMs);
	return normalizeVxPayload(
		data,
		normalizedUrl.user,
		options.includeMediaAlt ?? true,
	);
}

export async function fetchAdhxTweet(
	normalizedUrl: NormalizedTweetUrl,
	options: TweetFetcherOptions = {},
): Promise<ReadTweetOutput> {
	const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
	const url = `https://adhx.com/api/share/tweet/${encodeURIComponent(normalizedUrl.user)}/${normalizedUrl.id}`;
	const { data } = await fetchJson(url, "adhx", timeoutMs);
	return normalizeAdhxPayload(data, normalizedUrl.user);
}

function classifyUpstreamFailure(
	attempts: UpstreamAttempt[],
): TweetReaderError {
	if (
		attempts.length > 0 &&
		attempts.every((attempt) => attempt.status === 404)
	) {
		return new TweetReaderError(
			"not_found",
			"Tweet not found - may be deleted, protected, or the URL is wrong.",
			{ status: 404 },
		);
	}
	if (
		attempts.length > 0 &&
		attempts.every(
			(attempt) => attempt.status === 403 || attempt.status === 401,
		)
	) {
		return new TweetReaderError(
			"protected",
			"Account is protected; cannot read without auth.",
			{
				status: 403,
			},
		);
	}
	const details = attempts
		.map((attempt) => `${attempt.source} (${attempt.status})`)
		.join(", ");
	return new TweetReaderError(
		"upstream_unavailable",
		`Tweet upstream unavailable. Tried ${details}.`,
	);
}

export async function fetchTweetWithFallback(
	normalizedUrl: NormalizedTweetUrl,
	options: TweetFetcherOptions = {},
): Promise<ReadTweetOutput> {
	const attempts: UpstreamAttempt[] = [];
	const fetchers = [fetchFxTweet, fetchVxTweet, fetchAdhxTweet] as const;
	for (const fetcher of fetchers) {
		try {
			return await fetcher(normalizedUrl, options);
		} catch (error) {
			if (error instanceof TweetReaderError) {
				if (error.kind === "not_found" || error.kind === "protected") {
					attempts.push({
						source:
							fetcher === fetchFxTweet
								? "fxtwitter"
								: fetcher === fetchVxTweet
									? "vxtwitter"
									: "adhx",
						status: error.status ?? "invalid",
						message: error.message,
					});
					continue;
				}
				throw error;
			}
			if (error instanceof TweetUpstreamError) {
				attempts.push({
					source: error.source,
					status: error.status,
					message: error.message,
				});
				continue;
			}
			attempts.push({
				source:
					fetcher === fetchFxTweet
						? "fxtwitter"
						: fetcher === fetchVxTweet
							? "vxtwitter"
							: "adhx",
				status: "invalid",
				message: error instanceof Error ? error.message : String(error),
			});
		}
	}
	throw classifyUpstreamFailure(attempts);
}

export async function fetchFxThread(
	normalizedUrl: NormalizedTweetUrl,
	options: TweetFetcherOptions = {},
): Promise<ReadThreadOutput> {
	const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
	const url = `https://api.fxtwitter.com/2/thread/${normalizedUrl.id}?about_account=1`;
	const { data } = await fetchJson(url, "fxtwitter", timeoutMs);
	ensureFxSuccess(data);
	const record = asRecord(data);
	const root = normalizeFxStatus(
		record?.status ?? data,
		"fxtwitter",
		normalizedUrl.user,
		options.includeMediaAlt ?? true,
	);
	const threadItems = asArray(record?.thread)
		.map((item) => asRecord(item))
		.filter((item): item is JsonRecord => Boolean(item));
	const authorId = asString(asRecord(root.author)?.id);
	const authorUsername = root.author.username.toLowerCase();
	const tweets = [root];
	const seen = new Set([root.id]);
	for (const item of threadItems) {
		if (tweets.length >= 100) break;
		const candidate = normalizeFxStatus(
			item,
			"fxtwitter",
			normalizedUrl.user,
			options.includeMediaAlt ?? true,
		);
		if (seen.has(candidate.id)) continue;
		const candidateAuthor = asRecord(item.author);
		const sameAuthor =
			(asString(candidateAuthor?.id) &&
				asString(candidateAuthor?.id) === authorId) ||
			candidate.author.username.toLowerCase() === authorUsername;
		if (!sameAuthor) continue;
		seen.add(candidate.id);
		tweets.push(candidate);
	}
	return {
		root,
		tweets,
		count: tweets.length,
		source_api: "fxtwitter",
	};
}

export async function checkTweetUpstreams(
	timeoutMs = 1500,
): Promise<Record<TweetSourceApi, string>> {
	const probe: NormalizedTweetUrl = {
		input_url: "https://x.com/jack/status/20",
		id: "20",
		user: "jack",
		canonical_url: "https://x.com/jack/status/20",
	};
	const entries = await Promise.all(
		(
			[
				["fxtwitter", fetchFxTweet],
				["vxtwitter", fetchVxTweet],
				["adhx", fetchAdhxTweet],
			] as const
		).map(async ([source, fetcher]) => {
			try {
				await fetcher(probe, { timeoutMs, includeMediaAlt: false });
				return [source, "ok"] as const;
			} catch (error) {
				if (error instanceof TweetUpstreamError) {
					return [source, String(error.status)] as const;
				}
				return [
					source,
					error instanceof Error ? error.message : "error",
				] as const;
			}
		}),
	);
	return Object.fromEntries(entries) as Record<TweetSourceApi, string>;
}
