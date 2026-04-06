"""
=============================================================================
TWITTER TWEET EXTRACTOR
=============================================================================
Version: 1.1.0
Last Updated: April 2026

PURPOSE:
Use Claude to extract structured knowledge entries from batches of tweets
fetched via the Twitter/X API v2.

Three tweet types are handled with separate prompts:
  1. Original tweets   — standalone posts, batched 25 at a time
  2. Reply threads     — your reply + the tweet you replied to (for context)
  3. Quote-tweets      — your commentary + the tweet you quoted

INPUT:
  - Tweet-record dicts produced by ingestion/twitter/api_client.py

OUTPUT:
  - List of knowledge entry dicts ready for ingestion/core/storage.py

Each entry follows the same schema as entries from Gmail/GitHub ingestion:
  id, type, domain, state, current_view, confidence, positions,
  key_insights, metadata (with source_conversations), etc.
=============================================================================
"""

import hashlib
import json
from datetime import datetime
from typing import Optional

import anthropic

from ..core.config import ANTHROPIC_API_KEY, EXTRACTION_MODEL


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _generate_id(domain: str, current_view: str, source_type: str = "twitter") -> str:
    """Generate a stable knowledge-entry ID from domain + view text."""
    hash_input = f"{source_type}:{domain[:250]}{current_view[:250]}"
    hash_value = hashlib.sha256(hash_input.encode()).hexdigest()[:12]
    return f"ke_{hash_value}"


# ---------------------------------------------------------------------------
# Main extractor class
# ---------------------------------------------------------------------------

class TweetExtractor:
    """
    Extract knowledge entries from tweet batches using Claude.

    Three extraction paths, each with a tailored prompt:
      - extract_from_original_tweets   (batches of standalone tweets)
      - extract_from_reply_threads     (your reply + parent tweet context)
      - extract_from_quote_tweets      (your quote commentary + quoted tweet)
    """

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # -------------------------------------------------------------------------
    # 1. Original tweets
    # -------------------------------------------------------------------------
    def extract_from_original_tweets(self, tweets: list[dict]) -> list[dict]:
        """
        Extract knowledge from a batch of original (non-reply, non-quote) tweets.

        Args:
            tweets: List of TweetRecord dicts where is_reply=False, is_quote=False.

        Returns:
            List of knowledge entry dicts.
        """
        if not tweets:
            return []

        tweet_block = "\n\n".join(
            f"[{t['created_at'][:10]}] (id:{t['id']})\n{t['text']}"
            for t in tweets
        )

        prompt = f"""You are analyzing tweets posted by a person to extract durable knowledge about their views, expertise, and thinking.

TWEETS (chronological):
{tweet_block}

Extract knowledge entries only where the person expresses a genuine opinion, analysis, recommendation, or domain insight.

Rules:
- Skip tweets that are purely logistical, humorous without substance, or too vague to be useful
- Each entry should represent a distinct, self-contained topic or position
- Note the tweet_id that the knowledge came from

Return a JSON array. Return [] if nothing extractable is present.

JSON format:
[
  {{
    "domain": "specific topic (e.g. 'AI safety', 'startup fundraising')",
    "current_view": "1-3 sentences capturing the person's position or insight",
    "confidence": "high|medium|low",
    "evidence_snippet": "key quote from the tweet (verbatim)",
    "tweet_id": "tweet id this came from",
    "as_of": "YYYY-MM-DD"
  }}
]"""

        return self._call_llm_and_build_entries(prompt, tweets, source_label="original")

    # -------------------------------------------------------------------------
    # 2. Reply threads
    # -------------------------------------------------------------------------
    def extract_from_reply_threads(self, threads: list[dict]) -> list[dict]:
        """
        Extract knowledge from reply threads.

        Each thread dict has:
          - reply:         TweetRecord (your reply tweet)
          - parent_text:   str or None  (the tweet you replied to)
          - parent_author: str or None  (@handle of the original author)

        Args:
            threads: List of thread dicts.

        Returns:
            List of knowledge entry dicts.
        """
        if not threads:
            return []

        tweet_lookup: dict[str, dict] = {}
        parts: list[str] = []

        for t in threads:
            reply = t["reply"]
            tweet_lookup[reply["id"]] = reply
            parent_text = t.get("parent_text") or "(original tweet not in archive)"
            parent_author = t.get("parent_author") or "someone"

            parts.append(
                f"[{reply['created_at'][:10]}] (reply_id:{reply['id']})\n"
                f"  {parent_author} said: {parent_text[:300]}\n"
                f"  YOU replied: {reply['text']}"
            )

        thread_block = "\n\n---\n\n".join(parts)

        prompt = f"""You are analyzing a person's Twitter replies to extract durable knowledge about their views, expertise, and thinking.

Each block shows the tweet being replied to, then the person's reply.

REPLY THREADS:
{thread_block}

Extract knowledge from the replies (the replies are the source of knowledge; the original tweet provides context).

Rules:
- The reply must contain a genuine opinion, analysis, or domain expertise
- Skip replies that are just agreement, pleasantries, or jokes without substance
- Use the parent tweet to add context to the entry's description but not as the main content
- Return reply_id (not the parent's id) in the evidence

JSON format:
[
  {{
    "domain": "specific topic",
    "current_view": "1-3 sentences capturing the person's position from their reply",
    "confidence": "high|medium|low",
    "evidence_snippet": "key quote from the reply (verbatim)",
    "reply_id": "the reply tweet id",
    "parent_context": "brief summary of what they were responding to",
    "as_of": "YYYY-MM-DD"
  }}
]"""

        return self._call_llm_and_build_entries(
            prompt,
            list(tweet_lookup.values()),
            source_label="reply",
            parent_context_key="parent_context",
        )

    # -------------------------------------------------------------------------
    # 3. Quote-tweets
    # -------------------------------------------------------------------------
    def extract_from_quote_tweets(self, quotes: list[dict]) -> list[dict]:
        """
        Extract knowledge from quote-tweets.

        Each quote dict has:
          - tweet:          TweetRecord (your quote-tweet)
          - quoted_text:    str or None  (the tweet you quoted)
          - quoted_author:  str or None  (@handle of the original author)

        Args:
            quotes: List of quote-tweet dicts.

        Returns:
            List of knowledge entry dicts.
        """
        if not quotes:
            return []

        tweet_lookup: dict[str, dict] = {}
        parts: list[str] = []

        for q in quotes:
            tweet = q["tweet"]
            tweet_lookup[tweet["id"]] = tweet
            quoted_text = q.get("quoted_text") or "(quoted tweet not available)"
            quoted_author = q.get("quoted_author") or "someone"

            parts.append(
                f"[{tweet['created_at'][:10]}] (quote_id:{tweet['id']})\n"
                f"  {quoted_author} wrote: {quoted_text[:300]}\n"
                f"  YOUR commentary: {tweet['text']}"
            )

        quote_block = "\n\n---\n\n".join(parts)

        prompt = f"""You are analyzing a person's quote-tweets to extract durable knowledge about their views, expertise, and thinking.

Each block shows the original tweet being quoted, then the person's own commentary added when quoting.

QUOTE-TWEETS:
{quote_block}

Extract knowledge from the person's OWN commentary (not the quoted tweet). The quoted tweet is context.

Rules:
- Commentary must contain a genuine perspective, analysis, or domain insight to be worth extracting
- Skip quote-tweets where the commentary is just "interesting" / "great point" / a laugh react
- Return quote_id in the evidence

JSON format:
[
  {{
    "domain": "specific topic",
    "current_view": "1-3 sentences capturing the person's commentary or position",
    "confidence": "high|medium|low",
    "evidence_snippet": "key quote from the commentary (verbatim)",
    "quote_id": "the quote-tweet id",
    "quoted_context": "brief summary of the original tweet being quoted",
    "as_of": "YYYY-MM-DD"
  }}
]"""

        return self._call_llm_and_build_entries(
            prompt,
            list(tweet_lookup.values()),
            source_label="quote",
            parent_context_key="quoted_context",
        )

    # -------------------------------------------------------------------------
    # Internal: call Claude, parse JSON, build entry dicts
    # -------------------------------------------------------------------------
    def _call_llm_and_build_entries(
        self,
        prompt: str,
        tweets: list[dict],
        source_label: str,
        parent_context_key: Optional[str] = None,
    ) -> list[dict]:
        """
        Send prompt to Claude, parse the JSON array, and convert to entry dicts.

        Args:
            prompt:             Full prompt string.
            tweets:             Tweet records in the batch (for evidence refs).
            source_label:       'original', 'reply', or 'quote'.
            parent_context_key: JSON key for the context summary, if any.

        Returns:
            List of knowledge entry dicts.
        """
        try:
            response = self.client.messages.create(
                model=EXTRACTION_MODEL,
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text
            start = text.find("[")
            end = text.rfind("]") + 1
            if start == -1 or end == 0:
                return []
            raw_entries = json.loads(text[start:end])

        except Exception as e:
            print(f"  LLM error: {e}")
            return []

        now = datetime.utcnow().isoformat()
        entries: list[dict] = []

        for raw in raw_entries:
            domain = raw.get("domain", "").strip()
            current_view = raw.get("current_view", "").strip()
            if not domain or not current_view:
                continue

            # Determine which tweet this knowledge came from
            tweet_id = (
                raw.get("tweet_id")
                or raw.get("reply_id")
                or raw.get("quote_id", "")
            )
            as_of = raw.get("as_of", now[:10])
            evidence_snippet = raw.get("evidence_snippet", "")[:200]
            parent_context = raw.get(parent_context_key, "") if parent_context_key else ""

            conversation_id = f"twitter:{source_label}:{tweet_id}"

            # For replies/quotes, prefix the position view with the context
            position_view = current_view
            if parent_context:
                position_view = f"[In response to: {parent_context}] {current_view}"

            entry_id = _generate_id(domain, current_view, "twitter")

            entry = {
                "id": entry_id,
                "type": "knowledge",
                "domain": domain,
                "subdomain": None,
                "state": "active",
                "detail_level": "full",
                "current_view": current_view,
                "confidence": raw.get("confidence", "medium"),
                "positions": [
                    {
                        "view": position_view,
                        "confidence": raw.get("confidence", "medium"),
                        "as_of": as_of,
                        "evidence": {
                            "conversation_id": conversation_id,
                            "message_ids": [tweet_id] if tweet_id else [],
                            "snippet": evidence_snippet,
                        },
                    }
                ],
                "key_insights": [
                    {
                        "insight": current_view,
                        "evidence": {
                            "conversation_id": conversation_id,
                            "message_ids": [tweet_id] if tweet_id else [],
                            "snippet": evidence_snippet,
                        },
                    }
                ],
                "knows_how_to": [],
                "open_questions": [],
                "related_repos": [],
                "related_knowledge": [],
                "evolution": [],
                "metadata": {
                    "created_at": now,
                    "updated_at": now,
                    "source_conversations": [conversation_id],
                    "source_messages": [tweet_id] if tweet_id else [],
                    "access_count": 0,
                    "last_accessed": None,
                },
                "full_content_ref": None,
            }

            entries.append(entry)

        return entries
