import hashlib
import tempfile
import unittest
from pathlib import Path

from galaxy_core.brain.reconcile import MemoryReconciler
from galaxy_core.brain.store import BrainStore
from galaxy_core.context import ContextCompiler
from galaxy_core.world import ProjectWorldModel


class WorldDriftTests(unittest.TestCase):
    def test_drift_detects_file_symbol_and_edge_changes_without_mutating_baseline(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir()
            (root / "src" / "user.py").write_text("class User:\n    pass\n", encoding="utf-8")
            (root / "src" / "auth.py").write_text("from .user import User\n\ndef login():\n    return User()\n", encoding="utf-8")
            world = ProjectWorldModel(root, root, "demo")
            baseline = world.sync()
            self.assertGreaterEqual(baseline.stats["files"], 2)

            (root / "src" / "user.py").write_text("class User:\n    pass\n\nclass Session:\n    pass\n", encoding="utf-8")
            (root / "src" / "session.py").write_text("from .user import Session\n", encoding="utf-8")
            report = world.drift()
            data = report.to_dict()
            self.assertTrue(data["has_drift"])
            self.assertTrue(any(x["path"] == "src/user.py" and x["change"] == "modified" for x in data["changes"]))
            self.assertTrue(any(x["path"] == "src/session.py" and x["change"] == "added" for x in data["changes"]))
            self.assertGreater(data["drift_score"], 0)
            # Preview does not consume the drift.
            self.assertTrue(world.drift().has_drift)
            world.sync_with_drift()
            self.assertFalse(world.drift().has_drift)


class FutureGraphTests(unittest.TestCase):
    def test_future_graph_propagates_reverse_dependency_blast_radius(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir(); (root / "tests").mkdir()
            (root / "src" / "user.py").write_text("class User:\n    pass\n", encoding="utf-8")
            (root / "src" / "auth.py").write_text("from .user import User\n", encoding="utf-8")
            (root / "tests" / "test_auth.py").write_text("from src.auth import User\n", encoding="utf-8")
            world = ProjectWorldModel(root, root, "demo")
            world.sync()
            scenario = world.future("change user model", seeds=["src/user.py"], depth=3)
            paths = {x.path: x for x in scenario.impacted}
            self.assertIn("src/user.py", paths)
            self.assertIn("src/auth.py", paths)
            self.assertIn("tests/test_auth.py", paths)
            self.assertEqual(paths["src/auth.py"].impact, "dependent")
            self.assertIn("tests/test_auth.py", scenario.affected_tests)
            self.assertGreater(scenario.structural_risk, 0)

    def test_context_packet_contains_future_impact(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir(); (root / "tests").mkdir()
            (root / "src" / "auth.py").write_text("def login():\n    return True\n", encoding="utf-8")
            (root / "tests" / "test_auth.py").write_text("from src.auth import login\n", encoding="utf-8")
            packet = ContextCompiler(root, root, "demo").compile("change auth login", refresh_world=True, budget_tokens=5000)
            self.assertTrue(packet.impact)
            self.assertIn("structural_risk", packet.impact)
            self.assertTrue(packet.impact.get("seeds"))
            self.assertIn("Predicted structural impact", packet.to_markdown())


class MemoryReconciliationTests(unittest.TestCase):
    def test_repository_source_change_is_detected_and_can_be_applied(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "config.txt"
            source.write_text("database=sqlite\n", encoding="utf-8")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            brain = BrainStore(root)
            mid = brain.remember("fact", "Database config", "Database is SQLite", project="demo",
                                 source_type="repository_file", source_ref="config.txt", source_hash=digest,
                                 confidence=.95, confirmed=True)
            uid = brain.get(mid)["uid"]
            source.write_text("database=postgresql\n", encoding="utf-8")
            dry = MemoryReconciler(brain, root).scan(project="demo", apply=False)
            self.assertTrue(any(x["uid"] == uid and x["type"] == "source_changed" for x in dry["findings"]))
            self.assertEqual(brain.get(uid)["status"], "active")
            applied = MemoryReconciler(brain, root).scan(project="demo", apply=True)
            self.assertGreaterEqual(applied["counts"]["applied"], 1)
            self.assertEqual(brain.get(uid)["status"], "stale")

    def test_newer_trusted_same_slot_decision_can_contradict_old_assertion(self):
        with tempfile.TemporaryDirectory() as td:
            brain = BrainStore(td)
            old_id = brain.remember("decision", "Primary database", "Use SQLite for all persistent application data",
                                    project="demo", confidence=.45)
            new_id = brain.remember("decision", "Primary database", "Use PostgreSQL as the primary relational database",
                                    project="demo", confidence=.98, confirmed=True)
            old_uid = brain.get(old_id)["uid"]
            new_uid = brain.get(new_id)["uid"]
            report = MemoryReconciler(brain, td).scan(project="demo", apply=True)
            hit = [x for x in report["findings"] if x["uid"] == old_uid and x["related_uid"] == new_uid]
            self.assertTrue(hit)
            self.assertTrue(hit[0]["applied"])
            self.assertEqual(brain.get(old_uid)["status"], "contradicted")
            self.assertEqual(brain.get(new_uid)["status"], "active")


if __name__ == "__main__":
    unittest.main()
