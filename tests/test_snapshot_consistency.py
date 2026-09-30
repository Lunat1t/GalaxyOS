import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from galaxy_core.attention import AttentionEngine
from galaxy_core.attention.index import AttentionIndex, StaleWorldSnapshot
from galaxy_core.context import ContextCompiler
from galaxy_core.world import ProjectWorldModel


class SnapshotConsistencyTests(unittest.TestCase):
    def test_context_refreshes_changed_source_and_exposes_stable_identity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "session.py"
            source.write_text("def session():\n    return 'old-token'\n", encoding="utf-8")
            compiler = ContextCompiler(root, root, "demo")
            first = compiler.compile("fix session token", budget_tokens=3500)
            self.assertEqual(first.world_snapshot_id, compiler.world.current().snapshot_id)
            self.assertEqual(first.world_snapshot_id, compiler.world.sync(full=True).snapshot_id)

            source.write_text("def session():\n    return 'new-token'\n", encoding="utf-8")
            second = compiler.compile("fix session token", budget_tokens=3500)
            self.assertNotEqual(first.world_snapshot_id, second.world_snapshot_id)
            self.assertIn("new-token", second.files[0]["excerpt"])
            self.assertEqual(second.world_snapshot_id, second.attention["trace"]["world_snapshot_id"])
            self.assertIn(second.world_snapshot_id, second.to_markdown())

    def test_uncached_source_mismatch_rejects_stale_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "a.py"
            source.write_text("def current(): pass\n", encoding="utf-8")
            snap = ProjectWorldModel(root, root, "demo").sync()
            source.write_text("def changed(): pass\n", encoding="utf-8")
            with self.assertRaises(StaleWorldSnapshot):
                AttentionEngine(root, "demo").build("changed", snap)

    def test_change_during_index_preparation_retries_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "a.py"
            source.write_text("def alpha(): return 'first'\n", encoding="utf-8")
            compiler = ContextCompiler(root, root, "demo")
            compiler.world.sync()
            original = AttentionIndex.prepare
            calls = 0

            def change_once(index, nodes):
                nonlocal calls
                calls += 1
                if calls == 1:
                    source.write_text("def alpha(): return 'second'\n", encoding="utf-8")
                return original(index, nodes)

            with patch.object(AttentionIndex, "prepare", change_once):
                packet = compiler.compile("alpha", budget_tokens=3500)
            self.assertEqual(calls, 2)
            self.assertIn("second", packet.files[0]["excerpt"])
            self.assertEqual(packet.world_snapshot_id, compiler.world.current().snapshot_id)


if __name__ == "__main__":
    unittest.main()
