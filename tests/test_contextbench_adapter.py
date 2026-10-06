import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from galaxy_core.benchmark import export_predictions, compare_exact_ann, compare_one_hop_graph
from galaxy_core.benchmark.contextbench import _contextbench_file_metrics


class ContextBenchAdapterTests(unittest.TestCase):
    def test_reads_upstream_per_task_file_metrics(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "scores.jsonl"
            path.write_text("\n".join(json.dumps({
                "instance_id": task,
                "final": {"file": {"coverage": 0.5, "precision": 0.25}},
            }) for task in ("django__django-12406", "django__django-12663")) + "\n")
            metrics = _contextbench_file_metrics(path)
            self.assertEqual(metrics["django__django-12406"], {"recall": 0.5, "precision": 0.25})

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
            self.assertEqual(summary["semantic"]["requested_mode"], "auto")
            self.assertEqual(summary["semantic"]["exact_tasks"], 1)
            prediction = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(prediction["instance_id"], "demo__app-1")
            self.assertIn("auth.py", prediction["traj_data"]["pred_files"])
            self.assertNotIn("future.py", prediction["traj_data"]["pred_files"])
            self.assertNotIn("gold_context", output.read_text(encoding="utf-8"))

            ann_output = root / "predictions-ann.jsonl"
            ann_summary = export_predictions(
                dataset, ann_output, repos_dir=root / "repositories", max_files=2,
                semantic_mode="ann",
            )
            self.assertEqual(ann_summary["semantic"]["ann_tasks"], 1)
            self.assertLess(ann_summary["semantic"]["scored_vectors"],
                            ann_summary["semantic"]["total_vectors"])
            self.assertIn("auth.py", json.loads(ann_output.read_text())["traj_data"]["pred_files"])

            paired = compare_exact_ann(dataset, root / "paired", repos_dir=root / "repositories", max_files=2)
            self.assertEqual(paired["paired"], 1)
            self.assertEqual(paired["selection"]["mean_file_jaccard"], 1.0)
            self.assertEqual(paired["selection"]["tasks_with_different_files"], 0)
            self.assertIn("recall and precision require the upstream evaluator", paired["note"])
            report = json.loads(Path(paired["report"]).read_text(encoding="utf-8"))
            self.assertEqual(report["configuration"]["max_files"], 2)
            self.assertEqual(len(report["dataset_sha256"]), 64)

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

    def test_graph_ablation_uses_the_fixed_three_django_tasks_and_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "django"
            (repo / "django" / "db" / "models" / "fields").mkdir(parents=True)
            (repo / "django" / "forms").mkdir(parents=True)
            (repo / "django" / "db" / "models" / "fields" / "__init__.py").write_text(
                "class Field:\n    pass\n", encoding="utf-8")
            (repo / "django" / "db" / "models" / "fields" / "related.py").write_text(
                "from . import Field as ParentField\nclass RelatedField(ParentField):\n    pass\n", encoding="utf-8")
            (repo / "django" / "forms" / "widgets.py").write_text(
                "from django.db.models.fields.related import RelatedField\nclass ChoiceWidget:\n    pass\n", encoding="utf-8")
            for i in range(18):
                (repo / f"noise_{i}.py").write_text(f"def unrelated_{i}():\n    return {i}\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.org",
                            "commit", "-qm", "base"], check=True)
            base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            instance_ids = ("django__django-12406", "django__django-12663", "django__django-16263")
            dataset = root / "three-tasks.jsonl"
            dataset.write_text("\n".join(json.dumps({
                "instance_id": instance_id, "repo": "django/django", "base_commit": base,
                "problem_statement": "ModelForm ForeignKey blank option and related field behavior",
            }) for instance_id in instance_ids) + "\n", encoding="utf-8")

            report = compare_one_hop_graph(dataset, root / "graph-ablation", repo=repo)
            self.assertEqual(report["paired"], 3)
            self.assertEqual(report["configuration"]["max_files"], 15)
            self.assertEqual(report["configuration"]["token_budget"], 20000)
            self.assertEqual(report["instances"], list(instance_ids))
            self.assertTrue(Path(report["report"]).is_file())
