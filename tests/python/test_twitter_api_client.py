from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
INGESTION_DIR = REPO_ROOT / "ingestion"

if str(INGESTION_DIR) not in sys.path:
    sys.path.insert(0, str(INGESTION_DIR))

from twitter.api_client import TwitterAPIClient


class TwitterAPIClientTests(unittest.TestCase):
    def test_iter_user_tweets_zero_limit_does_not_call_api(self) -> None:
        client = TwitterAPIClient(bearer_token="test-token", username="arjundivecha")

        with patch.object(client, "get_user_id", side_effect=AssertionError("should not resolve user")):
            tweets = list(client.iter_user_tweets(max_tweets=0))

        self.assertEqual(tweets, [])


if __name__ == "__main__":
    unittest.main()
