export type TweetSourceApi = "fxtwitter" | "vxtwitter" | "adhx";

export type TweetMedia = {
	type: "image" | "video" | "gif";
	url: string;
	alt_text?: string;
};

export type ReadTweetOutput = {
	id: string;
	url: string;
	author: {
		username: string;
		display_name: string;
		verified: boolean;
		followers: number;
	};
	created_at: string;
	text: string;
	is_long_form_article: boolean;
	article?: {
		title: string;
		body_markdown: string;
	};
	media: TweetMedia[];
	engagement: {
		replies: number;
		retweets: number;
		likes: number;
		quotes: number;
		bookmarks: number;
		views: number;
	};
	quoted_tweet?: ReadTweetOutput;
	community_note?: string;
	source_api: TweetSourceApi;
};

export type ReadThreadOutput = {
	root: ReadTweetOutput;
	tweets: ReadTweetOutput[];
	count: number;
	source_api: "fxtwitter";
};

export type NormalizedTweetUrl = {
	input_url: string;
	id: string;
	user: string;
	canonical_url: string;
};

export type TweetFetcherOptions = {
	includeMediaAlt?: boolean;
	timeoutMs?: number;
};

export class TweetReaderError extends Error {
	readonly kind:
		| "invalid_url"
		| "not_found"
		| "protected"
		| "upstream_unavailable"
		| "upstream_error";
	readonly status?: number;

	constructor(
		kind: TweetReaderError["kind"],
		message: string,
		options: { status?: number; cause?: unknown } = {},
	) {
		super(message);
		this.name = "TweetReaderError";
		this.kind = kind;
		this.status = options.status;
		if (options.cause) {
			(this as Error & { cause?: unknown }).cause = options.cause;
		}
	}
}

export class TweetUpstreamError extends Error {
	readonly source: TweetSourceApi;
	readonly status: number | "timeout" | "network" | "invalid";

	constructor(
		source: TweetSourceApi,
		status: TweetUpstreamError["status"],
		message: string,
	) {
		super(message);
		this.name = "TweetUpstreamError";
		this.source = source;
		this.status = status;
	}
}
