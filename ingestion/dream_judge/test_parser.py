"""
=============================================================================
SCRIPT NAME: dream_judge/test_parser.py
=============================================================================

PURPOSE:
Unit tests for parse_verdict_response — the function that extracts
{verdict, reason} from a model's response. This is the trickiest piece
of the judge script (LLM output can be wrapped, prefixed, or include
markdown fences).

USAGE:
    python -m unittest ingestion/dream_judge/test_parser.py
=============================================================================
"""

import unittest

from .run import parse_verdict_response


class ParseVerdictResponseTests(unittest.TestCase):

    def test_clean_json_apply(self):
        result = parse_verdict_response('{"verdict": "apply", "reason": "labels match exactly"}')
        self.assertEqual(result, ("apply", "labels match exactly"))

    def test_clean_json_skip(self):
        result = parse_verdict_response('{"verdict": "skip", "reason": "topics differ"}')
        self.assertEqual(result, ("skip", "topics differ"))

    def test_wrapped_in_code_fence_with_lang(self):
        text = '```json\n{"verdict": "apply", "reason": "ok"}\n```'
        result = parse_verdict_response(text)
        self.assertEqual(result, ("apply", "ok"))

    def test_wrapped_in_code_fence_no_lang(self):
        text = '```\n{"verdict": "skip", "reason": "wait"}\n```'
        result = parse_verdict_response(text)
        self.assertEqual(result, ("skip", "wait"))

    def test_preamble_before_json(self):
        text = 'After careful consideration: {"verdict": "apply", "reason": "match"}'
        result = parse_verdict_response(text)
        self.assertEqual(result, ("apply", "match"))

    def test_invalid_verdict_value(self):
        text = '{"verdict": "maybe", "reason": "unclear"}'
        self.assertIsNone(parse_verdict_response(text))

    def test_missing_reason(self):
        text = '{"verdict": "apply"}'
        self.assertIsNone(parse_verdict_response(text))

    def test_empty_reason(self):
        text = '{"verdict": "skip", "reason": ""}'
        self.assertIsNone(parse_verdict_response(text))

    def test_malformed_json(self):
        text = '{"verdict": "apply", "reason": "missing brace"'
        self.assertIsNone(parse_verdict_response(text))

    def test_no_json_at_all(self):
        text = 'I think we should apply this one.'
        self.assertIsNone(parse_verdict_response(text))

    def test_empty_input(self):
        self.assertIsNone(parse_verdict_response(""))
        self.assertIsNone(parse_verdict_response(None))  # type: ignore[arg-type]


class BuildPromptTests(unittest.TestCase):

    def test_prompt_contains_payload_and_rubric(self):
        from .run import build_prompt
        item = {
            "op_type": "duplicate_merge_borderline",
            "rubric": "decide if these two entries are the same memory",
            "payload": {"canonical_id": "ke_a", "duplicate_ids": ["ke_b"]},
        }
        prompt = build_prompt(item)
        self.assertIn("duplicate_merge_borderline", prompt)
        self.assertIn("decide if these two entries", prompt)
        self.assertIn("ke_a", prompt)
        self.assertIn("ke_b", prompt)
        # Should ask for JSON-only reply.
        self.assertIn("JSON object", prompt)


if __name__ == "__main__":
    unittest.main()
