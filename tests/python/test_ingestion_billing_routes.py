from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

INGESTION_WORKFLOWS = [
    REPO_ROOT / ".github/workflows/twitter-ingestion.yml",
    REPO_ROOT / ".github/workflows/github-ingestion.yml",
    REPO_ROOT / ".github/workflows/agent-session-ingestion.yml",
]


class IngestionBillingRouteTests(unittest.TestCase):
    def test_ingestion_workflows_route_to_api_fallback_instead_of_skipping(self) -> None:
        for workflow in INGESTION_WORKFLOWS:
            with self.subTest(workflow=workflow.name):
                text = workflow.read_text(encoding="utf-8")
                self.assertIn("ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}", text)
                self.assertIn("PKS_API_FALLBACK_RESERVE_USD", text)
                self.assertIn("PKS_API_FALLBACK_RUN_MAX_BUDGET_USD", text)
                self.assertIn("PKS_API_FALLBACK_MAX_CALLS", text)
                self.assertIn("PKS_API_FALLBACK_BUDGET_FILE", text)
                self.assertIn("Select Claude billing route", text)
                self.assertIn("billing_source=api_fallback", text)
                self.assertIn("PKS_ALLOW_ANTHROPIC_API_FALLBACK=1", text)
                self.assertIn("Validate API fallback key", text)
                self.assertIn("using Anthropic API fallback for this ingestion run", text)
                self.assertNotIn("steps.sdk_auth.outputs.available", text)
                self.assertNotIn("Report skipped ingestion", text)
                self.assertNotIn("intentionally skipped because this runner cannot", text)

    def test_local_nightly_wrapper_falls_back_without_skipping(self) -> None:
        text = (REPO_ROOT / "scripts/run_nightly_ingestion.sh").read_text(encoding="utf-8")
        self.assertIn("using Anthropic API fallback for this overnight run", text)
        self.assertIn("ANTHROPIC_API_KEY is not set", text)
        self.assertIn("PKS_API_FALLBACK_RESERVE_USD", text)
        self.assertIn("PKS_API_FALLBACK_RUN_MAX_BUDGET_USD", text)
        self.assertIn("PKS_API_FALLBACK_MAX_CALLS", text)
        self.assertIn("PKS_API_FALLBACK_BUDGET_FILE", text)
        self.assertIn('DREAM_ALLOW_ANTHROPIC_API_FALLBACK="${DREAM_ALLOW_ANTHROPIC_API_FALLBACK:-0}"', text)
        self.assertNotIn("This overnight runner must execute on the local Mac context", text)


if __name__ == "__main__":
    unittest.main()
