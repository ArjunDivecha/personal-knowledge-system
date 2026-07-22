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
from pathlib import Path
from unittest.mock import patch

from . import run
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


class InsightVerdictParseTests(unittest.TestCase):
    """Content-bearing insight_synthesis verdicts (PRD 2026-07-02)."""

    TARGETS = ["ke_a", "ke_b", "ke_c"]

    def test_valid_apply_append(self):
        text = (
            '{"verdict": "apply", "reason": "clear cross-cutting pattern", '
            '"synthesis": {"insight_text": "Turnover limits dominate alpha capture.", '
            '"placement": "append", "anchor_entry_id": "ke_b", '
            '"support_entry_ids": ["ke_a", "ke_b", "ke_c"]}}'
        )
        result = run.parse_insight_verdict_response(text, self.TARGETS)
        self.assertIsNotNone(result)
        verdict, reason, synthesis = result
        self.assertEqual(verdict, "apply")
        self.assertEqual(reason, "clear cross-cutting pattern")
        self.assertEqual(synthesis["placement"], "append")
        self.assertEqual(synthesis["anchor_entry_id"], "ke_b")
        self.assertEqual(synthesis["support_entry_ids"], self.TARGETS)
        self.assertNotIn("domain", synthesis)

    def test_valid_apply_create(self):
        text = (
            '{"verdict": "apply", "reason": "spans entries", '
            '"synthesis": {"insight_text": "A durable pattern.", '
            '"placement": "create", "domain": "cross-domain synthesis", '
            '"support_entry_ids": ["ke_a", "ke_b", "ke_c"]}}'
        )
        result = run.parse_insight_verdict_response(text, self.TARGETS)
        self.assertIsNotNone(result)
        verdict, _reason, synthesis = result
        self.assertEqual(verdict, "apply")
        self.assertEqual(synthesis["domain"], "cross-domain synthesis")
        self.assertNotIn("anchor_entry_id", synthesis)

    def test_skip_needs_no_synthesis(self):
        result = run.parse_insight_verdict_response(
            '{"verdict": "skip", "reason": "no durable insight"}', self.TARGETS
        )
        self.assertEqual(result, ("skip", "no durable insight", None))

    def test_apply_without_synthesis_is_rejected(self):
        self.assertIsNone(
            run.parse_insight_verdict_response(
                '{"verdict": "apply", "reason": "looks good"}', self.TARGETS
            )
        )

    def test_apply_with_anchor_outside_cluster_is_rejected(self):
        text = (
            '{"verdict": "apply", "reason": "ok", '
            '"synthesis": {"insight_text": "x", "placement": "append", "anchor_entry_id": "ke_zzz", '
            '"support_entry_ids": ["ke_a", "ke_b", "ke_c"]}}'
        )
        self.assertIsNone(run.parse_insight_verdict_response(text, self.TARGETS))

    def test_apply_with_overlong_text_is_rejected(self):
        text = (
            '{"verdict": "apply", "reason": "ok", '
            f'"synthesis": {{"insight_text": "{"x" * 501}", "placement": "create", "domain": "d", '
            '"support_entry_ids": ["ke_a", "ke_b", "ke_c"]}}'
        )
        self.assertIsNone(run.parse_insight_verdict_response(text, self.TARGETS))

    def test_apply_with_invalid_placement_is_rejected(self):
        text = (
            '{"verdict": "apply", "reason": "ok", '
            '"synthesis": {"insight_text": "x", "placement": "replace", "anchor_entry_id": "ke_a", '
            '"support_entry_ids": ["ke_a", "ke_b", "ke_c"]}}'
        )
        self.assertIsNone(run.parse_insight_verdict_response(text, self.TARGETS))

    def test_fenced_insight_verdict_parses(self):
        text = (
            "```json\n"
            '{"verdict": "apply", "reason": "ok", "synthesis": {"insight_text": "x", '
            '"placement": "append", "anchor_entry_id": "ke_a", '
            '"support_entry_ids": ["ke_a", "ke_b", "ke_c"]}}\n'
            "```"
        )
        result = run.parse_insight_verdict_response(text, self.TARGETS)
        self.assertIsNotNone(result)


class ValidateSynthesisTests(unittest.TestCase):

    def test_rejection_reasons(self):
        targets = ["ke_a", "ke_b", "ke_c"]
        self.assertEqual(run.validate_synthesis(None, targets), "missing_synthesis_block")
        self.assertEqual(
            run.validate_synthesis({"insight_text": " ", "placement": "create", "domain": "d"}, targets),
            "empty_insight_text",
        )
        self.assertEqual(
            run.validate_synthesis({"insight_text": "x", "placement": "append"}, targets),
            "missing_support_entry_ids",
        )
        self.assertEqual(
            run.validate_synthesis({"insight_text": "x", "placement": "create", "support_entry_ids": targets}, targets),
            "missing_domain",
        )
        self.assertIsNone(
            run.validate_synthesis(
                {"insight_text": "x", "placement": "append", "anchor_entry_id": "ke_a", "support_entry_ids": targets}, targets
            )
        )

    def test_support_set_rejection_reasons(self):
        targets = ["ke_a", "ke_b", "ke_c", "ke_d"]
        self.assertEqual(
            run.validate_synthesis(
                {"insight_text": "x", "placement": "create", "domain": "d", "support_entry_ids": ["ke_a", "ke_b"]}, targets
            ),
            "insufficient_support_entries",
        )
        self.assertEqual(
            run.validate_synthesis(
                {"insight_text": "x", "placement": "create", "domain": "d", "support_entry_ids": ["ke_a", "ke_b", 3]}, targets
            ),
            "invalid_support_entry_id",
        )
        self.assertEqual(
            run.validate_synthesis(
                {"insight_text": "x", "placement": "create", "domain": "d", "support_entry_ids": ["ke_a", "ke_b", "ke_z"]}, targets
            ),
            "support_entry_outside_cluster",
        )
        self.assertEqual(
            run.validate_synthesis(
                {"insight_text": "x", "placement": "append", "anchor_entry_id": "ke_d", "support_entry_ids": ["ke_a", "ke_b", "ke_c"]}, targets
            ),
            "anchor_outside_support_set",
        )


class ParseDispatchTests(unittest.TestCase):

    def test_dispatch_by_op_type(self):
        classic_item = {"op_type": "duplicate_merge_borderline", "target_entry_ids": ["ke_a", "ke_b"]}
        result = run.parse_response_for_item('{"verdict": "apply", "reason": "same memory"}', classic_item)
        self.assertEqual(result, ("apply", "same memory", None))

        insight_item = {"op_type": "insight_synthesis", "target_entry_ids": ["ke_a", "ke_b", "ke_c"]}
        # A classic-shaped apply (no synthesis) must be rejected for insight ops.
        self.assertIsNone(
            run.parse_response_for_item('{"verdict": "apply", "reason": "same memory"}', insight_item)
        )


class InsightPromptTests(unittest.TestCase):

    def test_insight_prompt_asks_for_synthesis_block(self):
        from .run import build_prompt
        item = {
            "op_type": "insight_synthesis",
            "rubric": "decide whether these entries support one durable insight",
            "target_entry_ids": ["ke_a", "ke_b", "ke_c"],
            "payload": {"members": [{"id": "ke_a"}, {"id": "ke_b"}, {"id": "ke_c"}]},
        }
        prompt = build_prompt(item)
        self.assertIn("insight_synthesis", prompt)
        self.assertIn("synthesis", prompt)
        self.assertIn("anchor_entry_id", prompt)
        self.assertIn("support_entry_ids", prompt)
        self.assertIn("placement", prompt)
        self.assertIn("prefer SKIP", prompt)


class ClaudeCliResolverTests(unittest.TestCase):

    def test_resolve_claude_cli_uses_env_path(self):
        with patch.object(run, "CLAUDE_CLI_PATH", "/tmp/fake-claude"), \
                patch.object(Path, "exists", return_value=True), \
                patch("os.access", return_value=True):
            self.assertEqual(run.resolve_claude_cli(), "/tmp/fake-claude")

    def test_resolve_claude_cli_uses_known_candidate_when_path_is_empty(self):
        fake_candidate = Path("/tmp/known-claude")
        with patch.object(run, "CLAUDE_CLI_PATH", ""), \
                patch("shutil.which", return_value=None), \
                patch.object(run, "CLAUDE_CLI_CANDIDATES", [fake_candidate]), \
                patch.object(Path, "exists", return_value=True), \
                patch("os.access", return_value=True):
            self.assertEqual(run.resolve_claude_cli(), str(fake_candidate))


if __name__ == "__main__":
    unittest.main()
