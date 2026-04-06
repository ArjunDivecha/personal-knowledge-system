"""
=============================================================================
TWITTER ARCHIVE PARSER
=============================================================================
Version: 1.0.0
Last Updated: April 2026

PURPOSE:
Parse tweets and replies from a downloaded Twitter/X data archive.

The Twitter archive is a folder you download from twitter.com/settings/your_account
→ "Download an archive of your data". Inside it you will find a data/ subfolder
containing JavaScript files like tweets.js. Each .js file looks like:

    window.YTD.tweets.part0 = [ { "tweet": {...} }, ... ]

This parser strips that JavaScript wrapper, parses the JSON, and returns
clean tweet records grouped as:
  - Your own original tweets (no reply_to)
  - Your replies, each bundled with the tweet you were replying to
    (the "in_reply_to" context is reconstructed from the archive's own data
     when available, otherwise only the IDs are kept)

INPUT FILES:
- <TWITTER_ARCHIVE_PATH>/data/tweets.js   — your tweets (primary)
- <TWITTER_ARCHIVE_PATH>/tweets.js        — alternate location (older archives)

OUTPUT:
- Python list of tweet-record dicts (see TweetRecord below)
=============================================================================
"""

import json
import re
import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Iterator, Optional

# ---------------------------------------------------------------------------
# Tweet record schema
# ---------------------------------------------------------------------------
# Each record returned by the parser looks like:
#
# {
#   "id":           "123456789",           # tweet id string
#   "text":         "Hello world",         # full tweet text
#   "created_at":   "2023-04-01T14:22:00", # ISO-8601 UTC
#   "date_obj":     datetime(...),         # Python datetime (UTC)
#   "is_reply":     True,                  # True when this is a reply
#   "reply_to_id":  "987654321" or None,   # id of the tweet being replied to
#   "reply_to_text": "..." or None,        # text of that tweet if we have it
#   "reply_to_author": "@handle" or None, # author handle of parent tweet
#   "source_url":   "https://twitter.com/i/web/status/123456789",
#   "lang":         "en",
#   "favorite_count": 12,
#   "retweet_count":  3,
# }
# ---------------------------------------------------------------------------

# Twitter archive date string format
_TWITTER_DATE_FORMAT = "%a %b %d %H:%M:%S +0000 %Y"


def _strip_js_wrapper(raw: str) -> str:
    """
    Remove the JavaScript variable assignment wrapper from a Twitter .js file.

    Twitter wraps every data file like:
        window.YTD.tweets.part0 = [...]
    We need to strip everything before the first '[' or '{'.
    """
    # Find the first [ or {
    bracket_idx = raw.find("[")
    brace_idx = raw.find("{")

    if bracket_idx == -1 and brace_idx == -1:
        raise ValueError("No JSON array or object found in file")

    if bracket_idx == -1:
        start = brace_idx
    elif brace_idx == -1:
        start = bracket_idx
    else:
        start = min(bracket_idx, brace_idx)

    return raw[start:]


def _parse_date(date_str: str) -> Optional[datetime]:
    """
    Parse a Twitter date string into a UTC datetime.
    Twitter uses:  'Mon Apr 01 14:22:00 +0000 2023'
    """
    if not date_str:
        return None
    try:
        dt = datetime.strptime(date_str, _TWITTER_DATE_FORMAT)
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        # Fallback: try ISO-8601 format (some exports)
        try:
            return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except ValueError:
            return None


def _clean_text(text: str) -> str:
    """
    Clean tweet text:
    - Normalize whitespace
    - Remove trailing t.co URLs that just link back to the tweet itself
      (Twitter appends these automatically and they add no meaning)
    """
    # Remove trailing t.co URLs (e.g., https://t.co/xxxxxx)
    text = re.sub(r"\s*https://t\.co/\S+$", "", text).strip()
    # Normalize multiple spaces
    text = re.sub(r" {2,}", " ", text)
    return text


class TwitterArchiveParser:
    """
    Parse a downloaded Twitter/X data archive.

    Usage:
        parser = TwitterArchiveParser("/path/to/twitter-archive")
        for tweet in parser.parse_tweets(since_year=2020):
            print(tweet["text"])
    """

    def __init__(self, archive_path: str | Path):
        """
        Args:
            archive_path: Path to the root of the unzipped Twitter archive folder.
                          Should contain a data/ subfolder with tweets.js inside.
        """
        self.archive_path = Path(archive_path)

        # Locate tweets.js — some archives put it under data/, older ones at root
        candidates = [
            self.archive_path / "data" / "tweets.js",
            self.archive_path / "tweets.js",
        ]
        self.tweets_js_path: Optional[Path] = None
        for candidate in candidates:
            if candidate.exists():
                self.tweets_js_path = candidate
                break

        if self.tweets_js_path is None:
            raise FileNotFoundError(
                f"tweets.js not found in {self.archive_path}. "
                "Expected at data/tweets.js or tweets.js"
            )

    # -------------------------------------------------------------------------
    # Internal: load raw tweet objects from the .js file
    # -------------------------------------------------------------------------
    def _load_raw_tweets(self) -> list[dict]:
        """Load and return the raw list of tweet wrapper objects from tweets.js."""
        raw = self.tweets_js_path.read_text(encoding="utf-8")
        json_str = _strip_js_wrapper(raw)
        data = json.loads(json_str)

        # Each element is {"tweet": {...}} in modern archives
        # Older archives may be the tweet dict directly
        tweets = []
        for item in data:
            if "tweet" in item:
                tweets.append(item["tweet"])
            else:
                tweets.append(item)

        return tweets

    # -------------------------------------------------------------------------
    # Internal: build a lookup map id → tweet for reply reconstruction
    # -------------------------------------------------------------------------
    def _build_id_map(self, raw_tweets: list[dict]) -> dict[str, dict]:
        """Build {tweet_id: raw_tweet} for fast reply-parent lookups."""
        return {t.get("id_str", t.get("id", "")): t for t in raw_tweets}

    # -------------------------------------------------------------------------
    # Internal: convert a raw tweet dict → clean TweetRecord dict
    # -------------------------------------------------------------------------
    def _build_record(
        self,
        raw: dict,
        id_map: dict[str, dict],
        username: str = "",
    ) -> Optional[dict]:
        """
        Convert a raw tweet dict from the archive into a clean record.

        Returns None if the tweet is a retweet of someone else (we only want
        the user's own voice: original tweets and replies they wrote).
        """
        full_text = raw.get("full_text", raw.get("text", ""))

        # Skip retweets — they are not the user's own words
        if full_text.startswith("RT @"):
            return None

        tweet_id = raw.get("id_str", raw.get("id", ""))

        date_obj = _parse_date(raw.get("created_at", ""))
        if date_obj is None:
            return None

        # Reply fields
        reply_to_id: Optional[str] = raw.get("in_reply_to_status_id_str") or raw.get("in_reply_to_status_id")
        reply_to_author: Optional[str] = raw.get("in_reply_to_screen_name")
        is_reply = bool(reply_to_id)

        # Try to find the parent tweet text from our own archive
        reply_to_text: Optional[str] = None
        if reply_to_id and reply_to_id in id_map:
            parent_raw = id_map[reply_to_id]
            parent_text = parent_raw.get("full_text", parent_raw.get("text", ""))
            if parent_text:
                reply_to_text = _clean_text(parent_text)

        # Build the URL to the tweet
        handle = username.lstrip("@") if username else "i"
        source_url = f"https://twitter.com/{handle}/status/{tweet_id}"

        return {
            "id": tweet_id,
            "text": _clean_text(full_text),
            "created_at": date_obj.strftime("%Y-%m-%dT%H:%M:%S"),
            "date_obj": date_obj,
            "is_reply": is_reply,
            "reply_to_id": reply_to_id or None,
            "reply_to_text": reply_to_text,
            "reply_to_author": f"@{reply_to_author}" if reply_to_author else None,
            "source_url": source_url,
            "lang": raw.get("lang", ""),
            "favorite_count": int(raw.get("favorite_count", 0) or 0),
            "retweet_count": int(raw.get("retweet_count", 0) or 0),
        }

    # -------------------------------------------------------------------------
    # Public: parse and yield tweet records
    # -------------------------------------------------------------------------
    def parse_tweets(
        self,
        since_year: int = 0,
        username: str = "",
        min_length: int = 30,
    ) -> Iterator[dict]:
        """
        Yield clean tweet records from the archive.

        Args:
            since_year: Only yield tweets from this year onwards (0 = all).
            username:   Your Twitter handle (used to build source URLs).
            min_length: Minimum cleaned tweet length to include.

        Yields:
            TweetRecord dicts as described at the top of this file.
        """
        raw_tweets = self._load_raw_tweets()
        id_map = self._build_id_map(raw_tweets)

        # Sort oldest-first so callers can track progress chronologically
        raw_tweets.sort(key=lambda t: t.get("created_at", ""))

        for raw in raw_tweets:
            record = self._build_record(raw, id_map, username=username)
            if record is None:
                continue

            # Filter by year
            if since_year and record["date_obj"].year < since_year:
                continue

            # Filter very short tweets (e.g., just a link or a single emoji)
            if len(record["text"]) < min_length:
                continue

            yield record

    # -------------------------------------------------------------------------
    # Public: count stats without full parse
    # -------------------------------------------------------------------------
    def count_tweets(self, since_year: int = 0, min_length: int = 30) -> dict:
        """
        Return basic counts without extracting knowledge.

        Returns dict with keys:
          total, original, replies, by_year
        """
        counts = {"total": 0, "original": 0, "replies": 0, "by_year": {}}

        raw_tweets = self._load_raw_tweets()
        id_map = self._build_id_map(raw_tweets)

        for raw in raw_tweets:
            record = self._build_record(raw, id_map)
            if record is None:
                continue
            if since_year and record["date_obj"].year < since_year:
                continue
            if len(record["text"]) < min_length:
                continue

            counts["total"] += 1
            year = str(record["date_obj"].year)
            counts["by_year"][year] = counts["by_year"].get(year, 0) + 1

            if record["is_reply"]:
                counts["replies"] += 1
            else:
                counts["original"] += 1

        return counts


# ---------------------------------------------------------------------------
# Quick test when run directly
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python parser.py <path-to-twitter-archive>")
        sys.exit(1)

    parser = TwitterArchiveParser(sys.argv[1])
    counts = parser.count_tweets(since_year=2015)
    print(f"Total tweets:    {counts['total']}")
    print(f"  Original:      {counts['original']}")
    print(f"  Replies:       {counts['replies']}")
    print(f"By year: {counts['by_year']}")

    print("\nFirst 3 records:")
    for i, record in enumerate(parser.parse_tweets(since_year=2015)):
        print(f"\n--- Tweet {i+1} ---")
        print(f"  ID:         {record['id']}")
        print(f"  Date:       {record['created_at']}")
        print(f"  Is reply:   {record['is_reply']}")
        if record["is_reply"]:
            print(f"  Replying to: {record['reply_to_author']}")
            print(f"  Parent text: {record['reply_to_text'] or '(not in archive)'}")
        print(f"  Text: {record['text'][:120]}")
        if i >= 2:
            break
