import tempfile
import unittest
from pathlib import Path

from galaxy_core.brain.store import BrainStore
from galaxy_core.engine.decisions import DecisionFabric, DecisionPolicy
from galaxy_core.engine.decisions.adapters import LocalLLMDecisionAdapter
from galaxy_core.engine.decisions.engine import DecisionEngine




class LivingMemoryTests(unittest.TestCase):
    def test_stale_memory_leaves_active_context_and_can_be_reactivated(self):
        with tempfile.TemporaryDirectory() as td:
            brain = BrainStore(td)
            mid = brain.remember("decision", "Database", "Use SQLite", project="demo", confirmed=True)
            uid = brain.get(mid)["uid"]
            stale = brain.mark_stale(uid, "repository migrated to PostgreSQL")
            self.assertEqual(stale["status"], "stale")
            self.assertFalse(any(x.get("uid") == uid for x in brain.context("database SQLite", project="demo")))
            active = brain.reactivate(uid, "revalidated")
            self.assertEqual(active["status"], "active")
            self.assertTrue(any(x.get("uid") == uid for x in brain.context("database SQLite", project="demo")))


class DecisionFabricTests(unittest.TestCase):
    def test_low_confidence_microdecision_escalates_by_policy(self):
        engine = DecisionEngine(local_adapter=LocalLLMDecisionAdapter(), prefer_cloud=False)
        fabric = DecisionFabric(engine, DecisionPolicy(auto_confidence=.95, escalate_below=.95))
        result = fabric.classify_task("fix backend authentication bug")
        self.assertIn(result.value, fabric.TASK_TYPES)
        self.assertEqual(result.action, "escalate")
        self.assertLess(result.confidence, .95)

    def test_policy_can_allow_bounded_highest_choice_without_granting_authority(self):
        engine = DecisionEngine(local_adapter=LocalLLMDecisionAdapter(), prefer_cloud=False)
        fabric = DecisionFabric(engine, DecisionPolicy(auto_confidence=.4, escalate_below=.4))
        result = fabric.choose("backend backend backend", "select work type", ["backend", "frontend"])
        self.assertEqual(result.value, "backend")
        self.assertEqual(result.action, "auto")


if __name__ == "__main__":
    unittest.main()
