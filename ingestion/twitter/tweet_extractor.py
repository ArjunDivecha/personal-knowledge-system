"""
=============================================================================
TWITTER TWEET EXTRACTOR
=============================================================================
Version: 1.0.0
Last Updated: April 2026

PURPOSE:
Use Claude to extract structured knowledge entries from batches of tweets and
reply threads. Follows the same pattern as ingestion/core/extractor.py but is
tailored for the specific context a tweet provides:
  - Short-form opinions and observations
  - Replies that only make sense alongside the original tweet
  - Threads (chains of self-replies)

INPUT:
- Batches of tweet records produced by ingestion/twitter/parser.py

OUTPUT:
- List of knowledge entry dicts ready for storage/core/storage.py

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

def _generate_id(content: str, source_type: str = "twitter") -> str:
    """Generate a stable knowledge-entry ID from content."""
    hash_input = f"{source_type}:{content[:500]}"
    hash_value = hashlib.sha256(hash_input.encode()).hexdigest()[:12]
    return f"ke_{hash_value}"


# ---------------------------------------------------------------------------
# Main extractor class
# ---------------------------------------------------------------------------

class TweetExtractor:
    """
    Extract knowledge entries from tweet batches using Claude.

    Batching strategy:
      - Original tweets are batched together (20-30 per call) because each
        tweet is short.  Claude sees the full batch and decides which ones
        contain extractable knowledge.
      - Replies are sent as mini-threads: the parent tweet text + your reply.
        These are also batched (10-15 per call).
    """

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # -------------------------------------------------------------------------
    # Public: extract from a batch of original (non-reply) tweets
    # -------------------------------------------------------------------------
    def extract_from_original_tweets(self, tweets: list[dict]) -> list[dict]:
        """
        Extract knowledge entries from a batch of original tweets (no reply context).

        Args:
            tweets: List of TweetRecord dicts from the parser (is_reply == False).

        Returns:
            List of knowledge entry dicts.
        """
        if not tweets:
            return []

        # Format tweets for the prompt
        tweet_block = "\n\n".join(
            f"[{t['created_at'][:10]}] (id:{t['id']})\n{t['text']}"
            for t in tweets
        )

        prompt = f"""You are analyzing tweets posted by a person to extract durable knowledge about their views, expertise, and thinking.

TWEETS (chronological order):
{tweet_block}

Extract knowledge entries for views, positions, or insights that are clearly and substantively expressed.

Rules:
- Capture genuine opinions, analyses, recommendations, or domain expertise
- Skip tweets that are purely logistical, humorous with no substance, or too vague
- Each entry should represent a distinct topic or position
- For each entry, note which tweet_id it came from in the evidence

Return a JSON array. Return [] if nothing substantive is present.

JSON format:
[
  {{
    "domain": "specific topic (e.g. 'AI safety', 'startup fundraising')",
    "current_view": "1-3 sentences capturing the person's position or insight",
    "confidence": "high|medium|low",
    "evidence_snippet": "key quote from the tweet",
    "tweet_id": "the tweet id this came from",
    "as_of": "YYYY-MM-DD date of the tweet"
  }}
]"""

        return self._call_llm_and_build_entries(prompt, tweets, source_label="original")

    # -------------------------------------------------------------------------
    # Public: extract from a batch of reply threads
    # -------------------------------------------------------------------------
    def extract_from_reply_threads(self, threads: list[dict]) -> list[dict]:
        """
        Extract knowledge entries from reply threads.

        Each thread dict has:
          - reply: TweetRecord (your reply)
          - parent_text: str or None (the tweet you were replying to)
          - parent_author: str or None (their @handle)

        Args:
            threads: List of thread dicts.

        Returns:
            List of knowledge entry dicts.
        """
        if not threads:
            return []

        thread_block_parts = []
        tweet_lookup = {}  # reply_id → reply tweet record for evidence building

        for t in threads:
            reply = t["reply"]
            tweet_lookup[reply["id"]] = reply
            parent_text = t.get("parent_text") or "(original tweet not available)"
            parent_author = t.get("parent_author") or "someone"

            thread_block_parts.append(
                f"[{reply['created_at'][:10]}] (reply_id:{reply['id']})\n"
                f"  {parent_author} said: {parent_text[:300]}\n"
                f"  YOU replied: {reply['text']}"
            )

        thread_block = "\n\n---\n\n".join(thread_block_parts)

        prompt = f"""You are analyzing a person's Twitter replies to extract durable knowledge about their views, expertise, and thinking.

Each entry below shows the tweet being replied to and the person's reply.

REPLY THREADS:
{thread_block}

Extract knowledge entries from the replies (YOUR replies are the source of knowledge — the original tweets provide context).

Rules:
- The reply must contain genuine opinions, analysis, or expertise — not just agreement, jokes, or small talk
- Consider the context the original tweet provides when summarizing the knowledge
- Return the reply_id (not the parent tweet's id) in evidence
- Return [] if none of the replies contain extractable knowledge

JSON format:
[
  {{
    "domain": "specific topic",
    "current_view": "1-3 sentences capturing the person's position or insight from their reply",
    "confidence": "high|medium|low",
    "evidence_snippet": "key quote from the reply",
    "reply_id": "the reply tweet id",
    "parent_context": "brief summary of what they were responding to",
    "as_of": "YYYY-MM-DD date of the reply"
  }}
]"""

        return self._call_llm_and_build_entries(
            prompt, list(tweet_lookup.values()), source_label="reply"
        )

    # -------------------------------------------------------------------------
    # Internal: call Claude, parse JSON, build entry dicts
    # -------------------------------------------------------------------------
    def _call_llm_and_build_entries(
        self,
        prompt: str,
        tweets: list[dict],
        source_label: str,
    ) -> list[dict]:
        """
        Send prompt to Claude, parse JSON array, and convert to entry dicts.

        Args:
            prompt:       The full prompt string.
            tweets:       The tweet records in the batch (for building evidence refs).
            source_label: 'original' or 'reply' — used in source_conversations.

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

            # Extract JSON array from response
            start = text.find("[")
            end = text.rfind("]") + 1
            if start == -1 or end == 0:
                return []

            raw_entries = json.loads(text[start:end])

        except Exception as e:
            print(f"  Error calling LLM: {e}")
            return []

        now = datetime.utcnow().isoformat()
        entries = []

        for raw in raw_entries:
            domain = raw.get("domain", "").strip()
            current_view = raw.get("current_view", "").strip()

            if not domain or not current_view:
                continue

            # Determine which tweet this knowledge came from
            tweet_id = raw.get("tweet_id") or raw.get("reply_id", "")
            as_of = raw.get("as_of", now[:10])
            evidence_snippet = raw.get("evidence_snippet", "")[:200]
            parent_context = raw.get("parent_context", "")

            conversation_id = f"twitter:{source_label}:{tweet_id}"

            # Build the position (includes parent context for replies)
            position_view = current_view
            if parent_context:
                position_view = f"[In response to: {parent_context}] {current_view}"

            entry_id = _generate_id(domain + current_view, "twitter")

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
