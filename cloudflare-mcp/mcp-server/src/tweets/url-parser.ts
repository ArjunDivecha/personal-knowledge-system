import { TweetReaderError, type NormalizedTweetUrl } from "./types";

const STATUS_PATH_RE = /^\/([^/]+)\/status(?:es)?\/(\d{1,25})(?:\/|$)/i;
const TCO_HOST_RE = /^(?:www\.)?t\.co$/i;
const ACCEPTED_EXACT_HOSTS = new Set([
	"x.com",
	"twitter.com",
	"fixupx.com",
	"fxtwitter.com",
	"vxtwitter.com",
]);

function stripMobilePrefix(hostname: string): string {
	return hostname.replace(/^(?:www\.|mobile\.|m\.)/i, "").toLowerCase();
}

function isAcceptedTweetHost(hostname: string): boolean {
	const normalized = stripMobilePrefix(hostname);
	return (
		ACCEPTED_EXACT_HOSTS.has(normalized) || normalized.startsWith("nitter.")
	);
}

function makeCanonicalUrl(user: string, id: string): string {
	return `https://x.com/${encodeURIComponent(user)}/status/${id}`;
}

function parseTweetUrlString(inputUrl: string): NormalizedTweetUrl {
	let parsed: URL;
	try {
		parsed = new URL(inputUrl);
	} catch {
		throw new TweetReaderError(
			"invalid_url",
			`Not a recognizable X/Twitter URL: ${inputUrl}`,
		);
	}

	if (!["http:", "https:"].includes(parsed.protocol)) {
		throw new TweetReaderError(
			"invalid_url",
			`Not a recognizable X/Twitter URL: ${inputUrl}`,
		);
	}

	if (!isAcceptedTweetHost(parsed.hostname)) {
		throw new TweetReaderError(
			"invalid_url",
			`Not a recognizable X/Twitter URL: ${inputUrl}`,
		);
	}

	const match = parsed.pathname.match(STATUS_PATH_RE);
	if (!match) {
		throw new TweetReaderError(
			"invalid_url",
			`Not a recognizable X/Twitter URL: ${inputUrl}`,
		);
	}

	const user = decodeURIComponent(match[1]);
	const id = match[2];
	if (id.length > 25) {
		throw new TweetReaderError("invalid_url", `Tweet ID is too long: ${id}`);
	}

	return {
		input_url: inputUrl,
		id,
		user,
		canonical_url: makeCanonicalUrl(user, id),
	};
}

async function resolveShortUrl(
	inputUrl: string,
	timeoutMs: number,
): Promise<string> {
	const controller = new AbortController();
	const timeout = setTimeout(() => controller.abort(), timeoutMs);
	try {
		let response = await fetch(inputUrl, {
			method: "HEAD",
			redirect: "follow",
			signal: controller.signal,
		});
		if (!response.ok && response.status === 405) {
			response = await fetch(inputUrl, {
				method: "GET",
				redirect: "follow",
				signal: controller.signal,
			});
		}
		return response.url;
	} catch (error) {
		throw new TweetReaderError(
			"invalid_url",
			`Could not resolve t.co short link: ${inputUrl}`,
			{
				cause: error,
			},
		);
	} finally {
		clearTimeout(timeout);
	}
}

export async function normalizeTweetUrl(
	inputUrl: string,
	timeoutMs = 3000,
): Promise<NormalizedTweetUrl> {
	const trimmed = inputUrl.trim();
	if (!trimmed) {
		throw new TweetReaderError(
			"invalid_url",
			"Not a recognizable X/Twitter URL: ",
		);
	}

	let parsed: URL;
	try {
		parsed = new URL(trimmed);
	} catch {
		throw new TweetReaderError(
			"invalid_url",
			`Not a recognizable X/Twitter URL: ${inputUrl}`,
		);
	}

	if (TCO_HOST_RE.test(parsed.hostname)) {
		const resolvedUrl = await resolveShortUrl(trimmed, timeoutMs);
		return parseTweetUrlString(resolvedUrl);
	}

	return parseTweetUrlString(trimmed);
}

export function normalizeTweetUrlSync(inputUrl: string): NormalizedTweetUrl {
	const trimmed = inputUrl.trim();
	if (!trimmed) {
		throw new TweetReaderError(
			"invalid_url",
			"Not a recognizable X/Twitter URL: ",
		);
	}
	return parseTweetUrlString(trimmed);
}
