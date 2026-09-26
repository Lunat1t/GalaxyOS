import tempfile
import unittest
from pathlib import Path

from galaxy_core.brain.store import BrainStore
from galaxy_core.context import ContextCompiler
from galaxy_core.engine.decisions import DecisionFabric, DecisionPolicy
from galaxy_core.engine.decisions.adapters import LocalLLMDecisionAdapter
from galaxy_core.engine.decisions.engine import DecisionEngine
from galaxy_core.world import ProjectWorldModel


class ContextOSWorldTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "src" / "user.py").write_text("class User:\n    pass\n", encoding="utf-8")
        (self.root / "src" / "auth.py").write_text("from .user import User\n\ndef login():\n    return User()\n", encoding="utf-8")
        (self.root / "tests" / "test_auth.py").write_text("from src.auth import login\n\ndef test_login():\n    assert login()\n", encoding="utf-8")
        (self.root / "README.md").write_text("# Demo\nAuthentication lives in src/auth.py.\n", encoding="utf-8")
        (self.root / "AGENTS.md").write_text("Always run tests after changes.\n", encoding="utf-8")
        (self.root / "src" / "AGENTS.md").write_text("Authentication changes require security review.\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_world_model_builds_import_graph_and_symbols(self):
        world = ProjectWorldModel(self.root, self.root, "demo")
        snap = world.sync()
        self.assertGreaterEqual(snap.stats["files"], 5)
        nodes = {n.path: n for n in snap.nodes}
        self.assertIn("login", nodes["src/auth.py"].symbols)
        edges = {(e.source, e.target, e.relation) for e in snap.edges}
        self.assertIn(("src/auth.py", "src/user.py", "imports"), edges)
        self.assertIn(("tests/test_auth.py", "src/auth.py", "imports"), edges)
        related = world.related("src/auth.py", depth=1)
        self.assertTrue(any(x["path"] == "src/user.py" for x in related))

    def test_context_compiler_selects_auth_files_and_instructions(self):
        BrainStore(self.root).remember("decision", "Auth policy", "Use explicit session checks", project="demo", confirmed=True)
        packet = ContextCompiler(self.root, self.root, "demo").compile(
            "fix authentication login bug and run tests", budget_tokens=3000, max_files=5, refresh_world=True
        )
        paths = [x["path"] for x in packet.files]
        self.assertIn("src/auth.py", paths)
        self.assertTrue(any(x["path"] == "AGENTS.md" for x in packet.instructions))
        self.assertTrue(any(x["path"] == "src/AGENTS.md" for x in packet.instructions))
        self.assertTrue(any(m.get("title") == "Auth policy" for m in packet.memories))
        self.assertLessEqual(packet.estimated_tokens, packet.budget_tokens)


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
