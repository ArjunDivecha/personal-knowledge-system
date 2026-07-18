import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("planner", ROOT / "scripts" / "semantic_candidate_planner.py")
planner = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(planner)


class SemanticPlannerTests(unittest.TestCase):
    def test_blockwise_matches_expected_and_manifest_fails_closed(self):
        rows = [
            {"id": "a", "vector": [1.0, 0.0]},
            {"id": "b", "vector": [0.99, 0.1]},
            {"id": "c", "vector": [0.0, 1.0]},
        ]
        clusters = planner.plan_candidates(rows, 0.9, 6, block_size=1)
        self.assertEqual(clusters, [["a", "b"]])
        with self.assertRaises(ValueError):
            planner.build_manifest(rows, clusters, threshold=0.9, query_capped=True)

    def test_uses_upstash_cosine_score_scale(self):
        # Raw cosine 0.91 maps to an Upstash COSINE score of 0.955.
        raw_cosine = 0.91
        rows = [
            {"id": "a", "vector": [1.0, 0.0]},
            {"id": "b", "vector": [raw_cosine, (1.0 - raw_cosine**2) ** 0.5]},
        ]
        self.assertEqual(planner.plan_candidates(rows, 0.95, 6), [["a", "b"]])


if __name__ == "__main__":
    unittest.main()
