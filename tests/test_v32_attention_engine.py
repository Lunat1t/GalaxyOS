import tempfile
import unittest
from pathlib import Path

from galaxy_core.attention import AdaptiveBudgeter, AttentionEngine
from galaxy_core.context import ContextCompiler
from galaxy_core.world import ProjectWorldModel


class AttentionEngineTests(unittest.TestCase):
    def test_hybrid_retrieval_finds_relevant_evidence_not_only_file_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir()
            padding = "\n".join(f"# unrelated header {i}" for i in range(80))
            (root / "src" / "session.py").write_text(
                padding + "\n\ndef rotate_refresh_token(user_id):\n    return 'rotated-token'\n",
                encoding="utf-8",
            )
            (root / "src" / "billing.py").write_text("def invoice_total():\n    return 42\n", encoding="utf-8")
            snap = ProjectWorldModel(root, root, "demo").sync()
            result = AttentionEngine(root, "demo").build(
                "change refresh token rotation", snap, budget_tokens=3500, max_files=5
            )
            selected = {x.path: x for x in result.selected}
            self.assertIn("src/session.py", selected)
            self.assertIn("rotate_refresh_token", selected["src/session.py"].excerpt)
            self.assertNotIn("invoice_total", selected["src/session.py"].excerpt)

    def test_graph_and_future_impact_pull_connected_tests(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir(); (root / "tests").mkdir()
            (root / "src" / "user.py").write_text("class User:\n    pass\n", encoding="utf-8")
            (root / "src" / "auth.py").write_text("from .user import User\n", encoding="utf-8")
            (root / "tests" / "test_auth.py").write_text("from src.auth import User\n", encoding="utf-8")
            snap = ProjectWorldModel(root, root, "demo").sync()
            result = AttentionEngine(root, "demo").build(
                "change user model", snap, budget_tokens=5000, max_files=8
            )
            selected = {x.path: x for x in result.selected}
            self.assertIn("src/user.py", selected)
            self.assertIn("src/auth.py", selected)
            self.assertIn("tests/test_auth.py", selected)
            self.assertGreater(selected["tests/test_auth.py"].graph_score + selected["tests/test_auth.py"].impact_score, 0)

    def test_code_change_seeds_prefer_implementation_over_docs_that_repeat_keywords(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir()
            (root / "src" / "compiler.py").write_text("def compile_context():\n    return 'packet'\n", encoding="utf-8")
            (root / "README.md").write_text(("context compiler attention retrieval token budgeting\n" * 40), encoding="utf-8")
            snap = ProjectWorldModel(root, root, "demo").sync()
            result = AttentionEngine(root, "demo").build(
                "refactor context compiler attention retrieval token budgeting", snap, budget_tokens=3000, max_files=5
            )
            self.assertEqual(result.trace["seed_paths"][0], "src/compiler.py")
            selected = {x.path: x for x in result.selected}
            self.assertEqual(selected["src/compiler.py"].role, "primary")

    def test_small_project_can_use_bounded_full_context(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
            (root / "b.py").write_text("def beta():\n    return 2\n", encoding="utf-8")
            snap = ProjectWorldModel(root, root, "demo").sync()
            result = AttentionEngine(root, "demo").build("review project", snap, budget_tokens=4000, max_files=5)
            self.assertEqual(result.strategy, "small-project-full-context")
            self.assertEqual({x.path for x in result.selected}, {"a.py", "b.py"})

    def test_adaptive_budget_distinguishes_local_and_architectural_tasks(self):
        stats = {"files": 300, "edges": 900}
        low = AdaptiveBudgeter.recommend("rename typo in readme", stats)
        high = AdaptiveBudgeter.recommend("refactor authentication architecture and database migration", stats)
        self.assertEqual(low.profile, "low")
        self.assertEqual(high.profile, "high")
        self.assertGreater(high.effective_tokens, low.effective_tokens)
        self.assertGreater(high.file_tokens, low.file_tokens)

    def test_attention_signals_are_exposed_in_context_packet(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir(); (root / "tests").mkdir()
            (root / "src" / "auth.py").write_text("def login(user):\n    return bool(user)\n", encoding="utf-8")
            (root / "tests" / "test_auth.py").write_text("from src.auth import login\n", encoding="utf-8")
            packet = ContextCompiler(root, root, "demo").compile(
                "fix authentication login bug and verify tests", budget_tokens=3200, max_files=6, refresh_world=True
            )
            self.assertEqual(packet.attention["strategy"], "small-project-full-context")
            self.assertIn("coverage_signal", packet.attention["quality"])
            self.assertTrue(any("retrieval" in f for f in packet.files))
            self.assertTrue(any(f.get("evidence") for f in packet.files))
            self.assertLessEqual(packet.estimated_tokens, packet.budget_tokens)
            self.assertIn("Attention Engine", packet.to_markdown())


if __name__ == "__main__":
    unittest.main()
