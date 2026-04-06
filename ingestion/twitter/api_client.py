"""
=============================================================================
TWITTER / X API v2 CLIENT
=============================================================================
Version: 1.1.0
Last Updated: April 2026

PURPOSE:
Thin wrapper around the Twitter/X API v2 that handles:
  - Bearer Token authentication
  - Paginating through a user's full tweet timeline (up to ~3,200 tweets)
  - Incremental pulls using since_id so repeated runs only fetch new tweets
  - Fetching the parent tweet for every reply (so context is preserved)
  - Fetching the quoted tweet for every quote-tweet
  - Automatic rate-limit back-off (HTTP 429 → sleep until reset)
  - Usage tracking for billable X resources

TWITTER API ENDPOINTS USED:
  GET /2/users/by/username/:username     — resolve handle to numeric user ID
  GET /2/users/:id/tweets                — paginate user's timeline
  GET /2/tweets                          — batch-fetch parent / quoted tweets

RATE LIMITS (pay-per-use, app-level Bearer Token):
  User timeline:  1,500 requests / 15 min
  Tweet lookup:   900 requests / 15 min
  (We stay well under these limits in normal usage)

INPUT:
  TWITTER_BEARER_TOKEN  env var / ingestion/.env
  TWITTER_USERNAME      env var / ingestion/.env

OUTPUT:
  Python lists of tweet-record dicts (see TweetRecord schema below)

TWEET RECORD SCHEMA:
{
  "id":                 "1234567890",
  "text":               "cleaned tweet text",
  "created_at":         "2024-03-15T14:22:00",   # ISO-8601 UTC, no tz suffix
  "is_reply":           True,
  "is_quote":           False,
  "reply_to_id":        "0987654321" or None,
  "reply_to_text":      "the tweet you replied to" or None,
  "reply_to_author":    "@handle" or None,
  "quoted_id":          "1122334455" or None,
  "quoted_text":        "the tweet you quoted" or None,
  "quoted_author":      "@handle" or None,
  "source_url":         "https://x.com/arjundivecha/status/1234567890",
  "lang":               "en",
  "like_count":         12,
  "retweet_count":      3,
  "reply_count":        1,
  "quote_count":        0,
}
=============================================================================
"""

import time
from datetime import datetime, timezone
from typing import Iterator, Optional

import requests

# ---------------------------------------------------------------------------
# Lazy import of config so this file can also be imported in isolation
# ---------------------------------------------------------------------------
def _get_config():
    """Import config lazily to allow standalone use."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.config import (
        TWITTER_BEARER_TOKEN,
        TWITTER_USERNAME,
        TWITTER_MAX_RESULTS_PER_PAGE,
        TWITTER_MIN_TWEET_LENGTH,
    )
    return TWITTER_BEARER_TOKEN, TWITTER_USERNAME, TWITTER_MAX_RESULTS_PER_PAGE, TWITTER_MIN_TWEET_LENGTH


# ---------------------------------------------------------------------------
# Twitter API v2 base URL
# ---------------------------------------------------------------------------
_BASE = "https://api.twitter.com/2"

# Fields and expansions requested on every timeline / lookup call
_TWEET_FIELDS = (
    "id,text,created_at,lang,public_metrics,"
    "referenced_tweets,in_reply_to_user_id"
)
_USER_FIELDS = "id,username,name"
_EXPANSIONS = "referenced_tweets.id,referenced_tweets.id.author_id,in_reply_to_user_id"


# ---------------------------------------------------------------------------
# Helper: parse Twitter ISO-8601 date → clean string + datetime object
# ---------------------------------------------------------------------------
def _parse_twitter_date(date_str: str) -> tuple[str, datetime]:
    """
    Convert '2024-03-15T14:22:00.000Z' to ('2024-03-15T14:22:00', datetime).
    """
    if not date_str:
        return "", datetime.utcnow().replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    return dt.strftime("%Y-%m-%dT%H:%M:%S"), dt


def _clean_text(text: str) -> str:
    """Remove trailing t.co URLs that Twitter appends automatically."""
    import re
    text = re.sub(r"\s*https://t\.co/\S+$", "", text).strip()
    return re.sub(r" {2,}", " ", text)


# ---------------------------------------------------------------------------
# Main client class
# ---------------------------------------------------------------------------
class TwitterAPIClient:
    """
    Twitter/X API v2 client for fetching a user's tweets and reply context.

    Usage:
        client = TwitterAPIClient(bearer_token="...")
        for tweet in client.iter_user_tweets(username="arjundivecha"):
            print(tweet["text"])
    """

    def __init__(self, bearer_token: str, username: str = ""):
        if not bearer_token:
            raise ValueError(
                "TWITTER_BEARER_TOKEN is empty. "
                "Add it to ingestion/.env or the Cursor Dashboard secrets."
            )
        self.bearer_token = bearer_token
        self.username = username.lstrip("@")
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self.bearer_token}",
            "User-Agent": "personal-knowledge-system/1.0",
        })
        # X pricing is credit-based and shown in the Developer Console.
        # We track billable usage counts here instead of guessing dollars.
        self.usage = {
            "user_lookups": 0,
            "timeline_posts_consumed": 0,
            "lookup_posts_consumed": 0,
            "total_posts_consumed": 0,
        }

    # -------------------------------------------------------------------------
    # Internal: HTTP GET with automatic rate-limit sleep
    # -------------------------------------------------------------------------
    def _get(self, url: str, params: dict) -> dict:
        """
        Make a GET request to the Twitter API.
        Automatically sleeps and retries once on HTTP 429 (rate limit).
        Raises on other non-200 status codes.
        """
        for attempt in range(2):
            resp = self._session.get(url, params=params, timeout=30)

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 429:
                # Rate limited — sleep until the reset window
                reset_epoch = int(resp.headers.get("x-rate-limit-reset", 0))
                sleep_secs = max(reset_epoch - int(time.time()), 0) + 5
                print(f"  [rate limit] sleeping {sleep_secs}s …", flush=True)
                time.sleep(sleep_secs)
                continue  # retry

            # Any other error
            raise RuntimeError(
                f"Twitter API error {resp.status_code} for {url}: {resp.text[:300]}"
            )

        raise RuntimeError(f"Twitter API still rate-limited after retry for {url}")

    # -------------------------------------------------------------------------
    # Look up the numeric user ID from a username
    # -------------------------------------------------------------------------
    def get_user_id(self, username: str) -> str:
        """
        Resolve a Twitter username to its numeric user ID.
        """
        url = f"{_BASE}/users/by/username/{username.lstrip('@')}"
        data = self._get(url, params={"user.fields": "id,username,name"})
        self.usage["user_lookups"] += 1
        user = data.get("data", {})
        if not user:
            raise RuntimeError(f"User @{username} not found: {data}")
        return user["id"]

    # -------------------------------------------------------------------------
    # Fetch a batch of tweets by ID (for parent / quoted tweet context)
    # -------------------------------------------------------------------------
    def fetch_tweets_by_ids(self, tweet_ids: list[str]) -> dict[str, dict]:
        """
        Batch-fetch up to 100 tweets by ID.
        Returns {tweet_id: {text, author_username}} dict.
        """
        if not tweet_ids:
            return {}

        results: dict[str, dict] = {}

        # Twitter allows up to 100 IDs per request
        for i in range(0, len(tweet_ids), 100):
            batch = tweet_ids[i : i + 100]
            data = self._get(
                f"{_BASE}/tweets",
                params={
                    "ids": ",".join(batch),
                    "tweet.fields": "id,text,author_id",
                    "expansions": "author_id",
                    "user.fields": "username",
                },
            )
            # Build author_id → username map from includes
            author_map: dict[str, str] = {}
            for user in data.get("includes", {}).get("users", []):
                author_map[user["id"]] = user.get("username", "")

            tweets_returned = data.get("data", [])
            self.usage["lookup_posts_consumed"] += len(tweets_returned)
            self.usage["total_posts_consumed"] += len(tweets_returned)

            for tweet in tweets_returned:
                tid = tweet["id"]
                author_username = author_map.get(tweet.get("author_id", ""), "")
                results[tid] = {
                    "text": _clean_text(tweet.get("text", "")),
                    "author_username": author_username,
                }

        return results

    # -------------------------------------------------------------------------
    # Core: paginate through the user's full timeline (streaming, page by page)
    # -------------------------------------------------------------------------
    def iter_user_tweets(
        self,
        username: str = "",
        since_id: str = None,
        min_length: int = 30,
        max_tweets: int = None,
    ) -> Iterator[dict]:
        """
        Yield tweet records from the user's timeline, newest-first per page
        (Twitter returns newest first; each page is yielded as it arrives so
        callers can break early without fetching the entire timeline).

        On the first (backfill) run, since_id should be None — we paginate
        all the way back through the timeline (~3,200 tweets max).
        On subsequent runs, pass the most-recently-seen tweet ID so we only
        fetch tweets posted after that point.

        Args:
            username:   Twitter handle (without @). Defaults to self.username.
            since_id:   Only return tweets newer than this ID (incremental).
            min_length: Skip tweets shorter than this many characters.
            max_tweets: Stop after yielding this many tweets (None = no limit).

        Yields:
            TweetRecord dicts (see module docstring for schema).
        """
        username = (username or self.username).lstrip("@")
        if not username:
            raise ValueError("username is required")

        user_id = self.get_user_id(username)
        print(f"  Resolved @{username} → user_id {user_id}")

        url = f"{_BASE}/users/{user_id}/tweets"
        next_token: Optional[str] = None
        page = 0
        total_yielded = 0

        while True:
            if max_tweets and total_yielded >= max_tweets:
                break

            page += 1
            params: dict = {
                "max_results": 100,
                "tweet.fields": _TWEET_FIELDS,
                "expansions": _EXPANSIONS,
                "user.fields": _USER_FIELDS,
                "exclude": "retweets",
            }
            if since_id:
                params["since_id"] = since_id
            if next_token:
                params["pagination_token"] = next_token

            print(f"  Fetching page {page} …", end=" ", flush=True)
            data = self._get(url, params)

            tweets_on_page = data.get("data", [])
            self.usage["timeline_posts_consumed"] += len(tweets_on_page)
            self.usage["total_posts_consumed"] += len(tweets_on_page)
            print(f"{len(tweets_on_page)} tweets")

            if not tweets_on_page:
                break

            # Build referenced-tweet map from this page's includes
            page_referenced: dict[str, dict] = {}
            page_users: dict[str, str] = {}

            for user in data.get("includes", {}).get("users", []):
                page_users[user["id"]] = user.get("username", "")

            for ref_tweet in data.get("includes", {}).get("tweets", []):
                tid = ref_tweet["id"]
                author_id = ref_tweet.get("author_id", "")
                page_referenced[tid] = {
                    "text": _clean_text(ref_tweet.get("text", "")),
                    "author_username": page_users.get(author_id, ""),
                }

            # Find any referenced IDs missing from includes and batch-fetch them
            missing_ids: set[str] = set()
            for raw in tweets_on_page:
                for ref in raw.get("referenced_tweets", []):
                    if ref["id"] not in page_referenced:
                        missing_ids.add(ref["id"])

            if missing_ids:
                print(f"    Fetching {len(missing_ids)} referenced tweets …")
                extra = self.fetch_tweets_by_ids(list(missing_ids))
                page_referenced.update(extra)

            # Yield records for this page
            for raw in tweets_on_page:
                if max_tweets and total_yielded >= max_tweets:
                    return

                text = _clean_text(raw.get("text", ""))
                if len(text) < min_length:
                    continue

                created_str, date_obj = _parse_twitter_date(raw.get("created_at", ""))
                tweet_id = raw["id"]

                is_reply = False
                is_quote = False
                reply_to_id: Optional[str] = None
                reply_to_text: Optional[str] = None
                reply_to_author: Optional[str] = None
                quoted_id: Optional[str] = None
                quoted_text: Optional[str] = None
                quoted_author: Optional[str] = None

                for ref in raw.get("referenced_tweets", []):
                    ref_id = ref["id"]
                    ref_info = page_referenced.get(ref_id, {})
                    ref_text = ref_info.get("text", "")
                    ref_author = ref_info.get("author_username", "")
                    if ref_author:
                        ref_author = f"@{ref_author}"

                    if ref["type"] == "replied_to":
                        is_reply = True
                        reply_to_id = ref_id
                        reply_to_text = ref_text or None
                        reply_to_author = ref_author or None
                    elif ref["type"] == "quoted":
                        is_quote = True
                        quoted_id = ref_id
                        quoted_text = ref_text or None
                        quoted_author = ref_author or None

                source_url = f"https://x.com/{username}/status/{tweet_id}"
                metrics = raw.get("public_metrics", {})

                total_yielded += 1
                yield {
                    "id": tweet_id,
                    "text": text,
                    "created_at": created_str,
                    "date_obj": date_obj,
                    "is_reply": is_reply,
                    "is_quote": is_quote,
                    "reply_to_id": reply_to_id,
                    "reply_to_text": reply_to_text,
                    "reply_to_author": reply_to_author,
                    "quoted_id": quoted_id,
                    "quoted_text": quoted_text,
                    "quoted_author": quoted_author,
                    "source_url": source_url,
                    "lang": raw.get("lang", ""),
                    "like_count": int(metrics.get("like_count", 0)),
                    "retweet_count": int(metrics.get("retweet_count", 0)),
                    "reply_count": int(metrics.get("reply_count", 0)),
                    "quote_count": int(metrics.get("quote_count", 0)),
                }

            meta = data.get("meta", {})
            next_token = meta.get("next_token")
            if not next_token:
                break

    # -------------------------------------------------------------------------
    # Count tweets (for progress display before full pull)
    # -------------------------------------------------------------------------
    def count_recent_tweets(self, username: str = "") -> int:
        """
        Quick count of tweets available via the timeline endpoint.
        Does one page fetch (100 tweets) to confirm connectivity.
        Returns the result_count from the first page's meta.
        """
        username = (username or self.username).lstrip("@")
        user_id = self.get_user_id(username)
        url = f"{_BASE}/users/{user_id}/tweets"
        data = self._get(url, params={
            "max_results": 100,
            "tweet.fields": "id",
            "exclude": "retweets",
        })
        tweets_returned = data.get("data", [])
        self.usage["timeline_posts_consumed"] += len(tweets_returned)
        self.usage["total_posts_consumed"] += len(tweets_returned)
        return data.get("meta", {}).get("result_count", 0)


# ---------------------------------------------------------------------------
# Quick connectivity test when run directly
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    (
        bearer_token, username,
        max_results_per_page, min_length
    ) = _get_config()

    client = TwitterAPIClient(bearer_token=bearer_token, username=username)

    print(f"Testing connectivity for @{username} …")
    count = client.count_recent_tweets()
    print(f"First page has {count} tweets (connection OK)")
    print()

    print("First 3 tweets from timeline:")
    for i, tweet in enumerate(client.iter_user_tweets(min_length=min_length)):
        kind = "reply" if tweet["is_reply"] else ("quote" if tweet["is_quote"] else "original")
        print(f"\n[{i+1}] {tweet['created_at'][:10]}  type={kind}")
        print(f"  {tweet['text'][:120]}")
        if tweet["is_reply"]:
            print(f"  → replying to {tweet['reply_to_author']}: {(tweet['reply_to_text'] or '')[:80]}")
        if tweet["is_quote"]:
            print(f"  → quoting {tweet['quoted_author']}: {(tweet['quoted_text'] or '')[:80]}")
        if i >= 2:
            break

    print(f"\nPosts consumed so far: {client.usage['total_posts_consumed']}")
    print(f"User lookups so far: {client.usage['user_lookups']}")
