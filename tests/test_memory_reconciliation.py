import hashlib
import tempfile
import unittest
from pathlib import Path

from galaxy_core.brain.reconcile import MemoryReconciler
from galaxy_core.brain.store import BrainStore






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
