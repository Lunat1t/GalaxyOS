import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from galaxy_core.context import ContextCompiler
from galaxy_core.context.capsules import CapsuleStore
from galaxy_core.world import ProjectWorldModel


class CapsuleTests(unittest.TestCase):
    def test_only_changed_component_rebuilds(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for part in ("attention", "world"):
                (root / "galaxy_core" / part).mkdir(parents=True)
            attention = root / "galaxy_core" / "attention" / "rank.py"
            attention.write_text("def rank():\n    return 1\n", encoding="utf-8")
            (root / "galaxy_core" / "world" / "model.py").write_text(
                "class World: pass\n", encoding="utf-8"
            )
            world = ProjectWorldModel(root, root, "demo")
            store = CapsuleStore(root, root, "demo")
            paths = ["galaxy_core/attention/rank.py", "galaxy_core/world/model.py"]
            first, stats = store.select(world.sync(), paths)
            self.assertEqual(stats, {"hits": 0, "built": 2})
            self.assertEqual([c["scope"] for c in first], ["galaxy_core/attention", "galaxy_core/world"])
            again, stats = CapsuleStore(root, root, "demo").select(world.load(), paths)
            self.assertEqual(stats, {"hits": 2, "built": 0})
            self.assertEqual(first, again)

            attention.write_text("def rank_fast():\n    return 2\n", encoding="utf-8")
            updated, stats = store.select(world.sync(), paths)
            self.assertEqual(stats, {"hits": 1, "built": 1})
            self.assertNotEqual(first[0]["fingerprint"], updated[0]["fingerprint"])
            self.assertEqual(first[1]["fingerprint"], updated[1]["fingerprint"])
            self.assertIn("rank_fast", updated[0]["files"][0]["symbols"])

    def test_compiler_exposes_maps_with_provenance_inside_budget(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir()
            (root / "src" / "auth.py").write_text("def login(user):\n    return bool(user)\n", encoding="utf-8")
            compiler = ContextCompiler(root, root, "demo")
            first = compiler.compile("fix login", refresh_world=True, budget_tokens=3500)
            second = compiler.compile("fix login", budget_tokens=3500)
            self.assertTrue(first.capsules)
            self.assertEqual(first.attention["capsule_cache"]["built"], 1)
            self.assertEqual(second.attention["packet_cache"]["status"], "hit")
            self.assertEqual(second.attention["capsule_cache"]["built"], 1)
            self.assertIn("src/auth.py", second.capsules[0]["files"][0]["path"])
            self.assertIn("## Component maps", second.to_markdown())
            self.assertLessEqual(second.estimated_tokens, second.budget_tokens)
            overview = compiler.overview("login auth")
            self.assertEqual(overview["capsules"][0]["scope"], "src")
            self.assertEqual(overview["cache"]["hits"], 1)
            self.assertLess(overview["estimated_tokens"], first.estimated_tokens)
            with patch.object(compiler.capsules, "select", return_value=([], {"hits": 0, "built": 0})):
                without = compiler.compile("fix login", budget_tokens=3500)
            self.assertEqual([f["path"] for f in second.files], [f["path"] for f in without.files])


if __name__ == "__main__":
    unittest.main()
