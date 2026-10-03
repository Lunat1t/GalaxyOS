import json
import tempfile
import unittest
from pathlib import Path

from galaxy_core.benchmark import run_repobench_r


class RepoBenchRTests(unittest.TestCase):
    def test_ranks_candidate_snippets_and_scores_without_querying_next_line(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dataset = root / "repobench.jsonl"
            rows = [
                {"instance_id": "python-cff-1", "repo_name": "demo", "file_path": "src/invoice.py",
                 "import_statement": "from billing import invoice_total",
                 "code": "def calculate_invoice_total(items):\n    return invoice_total(items)",
                 "next_line": "SECRET_TARGET_LINE", "context": [
                     "class Customer:\n    pass",
                     "def invoice_total(items):\n    return sum(items)",
                     "def parse_date(value):\n    return value",
                 ], "gold_snippet_index": 1},
                {"instance_id": "python-cfr-2", "repo_name": "demo", "file_path": "src/auth.py",
                 "import_statement": "from auth_utils import normalize_user",
                 "code": "def login(user):\n    return normalize_user(user)",
                 "next_line": "DO_NOT_READ", "context": [
                     "def normalize_user(user):\n    return user.strip().lower()",
                     "def render_page(template):\n    return template",
                 ], "gold_snippet_idex": 0},
            ]
            dataset.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            report = run_repobench_r(dataset, root / "run")
            self.assertEqual(report["requested"], 2)
            self.assertEqual(report["completed"], 2)
            self.assertEqual(report["failures"], [])
            self.assertTrue(report["query_excludes_next_line"])
            self.assertIn("accuracy_at_1", report["metrics"])
            self.assertIsNone(report["metrics"]["accuracy_at_5"])
            self.assertEqual(report["metric_sample_counts"]["accuracy_at_5"], 0)
            predictions = [json.loads(line) for line in Path(report["predictions"]).read_text().splitlines()]
            self.assertTrue(all(sorted(p["ranked_candidate_indices"]) == list(range(p["candidate_count"]))
                                for p in predictions))
            self.assertNotIn("SECRET_TARGET_LINE", Path(report["predictions"]).read_text())

    def test_missing_gold_index_is_reported_as_a_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dataset = root / "bad.jsonl"
            dataset.write_text(json.dumps({"context": ["def f(): pass"], "code": "x ="}) + "\n")
            report = run_repobench_r(dataset, root / "out")
            self.assertEqual(report["completed"], 0)
            self.assertIn("gold snippet index", report["failures"][0]["error"])


if __name__ == "__main__":
    unittest.main()
