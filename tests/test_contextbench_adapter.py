import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from galaxy_core.benchmark import export_predictions


class ContextBenchAdapterTests(unittest.TestCase):
    def test_exports_pre_fix_files_without_gold_leakage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repositories" / "demo" / "app"
            repo.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "auth.py").write_text("def refresh_token(token):\n    return token\n", encoding="utf-8")
            (repo / "irrelevant.py").write_text("def paint():\n    return 'blue'\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.org",
                            "commit", "-qm", "base"], check=True)
            base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            (repo / "future.py").write_text("future fix", encoding="utf-8")
            dataset = root / "dataset.jsonl"
            dataset.write_text(json.dumps({"instance_id": "demo__app-1", "repo": "demo/app",
                                           "base_commit": base, "problem_statement": "Fix refresh token handling",
                                           "gold_context": {"files": ["future.py"]}}) + "\n", encoding="utf-8")
            output = root / "predictions.jsonl"
            summary = export_predictions(dataset, output, repos_dir=root / "repositories", max_files=2)
            self.assertEqual(summary["completed"], 1, summary["failures"])
            prediction = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(prediction["instance_id"], "demo__app-1")
            self.assertIn("auth.py", prediction["traj_data"]["pred_files"])
            self.assertNotIn("future.py", prediction["traj_data"]["pred_files"])
            self.assertNotIn("gold_context", output.read_text(encoding="utf-8"))

    def test_missing_repository_is_reported_not_scored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dataset = root / "dataset.jsonl"
            dataset.write_text(json.dumps({"instance_id": "demo__missing-1", "repo": "demo/missing",
                                           "base_commit": "a" * 40, "problem_statement": "fix"}) + "\n")
            summary = export_predictions(dataset, root / "out.jsonl", repos_dir=root)
            self.assertEqual(summary["completed"], 0)
            self.assertEqual(len(summary["failures"]), 1)
            self.assertEqual((root / "out.jsonl").read_text(), "")
