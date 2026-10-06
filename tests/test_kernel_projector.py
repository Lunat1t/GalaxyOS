import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from galaxy_core.context import ContextCompiler
from galaxy_core.context.capsules import CapsuleStore
from galaxy_core.kernel import KernelProjector


class KernelProjectorTests(unittest.TestCase):
    def test_projection_prepares_caches_and_advances_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir(); (root / "tests").mkdir()
            source = root / "src" / "auth.py"
            source.write_text("def login(): return 'first'\n", encoding="utf-8")
            (root / "tests" / "test_auth.py").write_text("from src.auth import login\n", encoding="utf-8")
            projector = KernelProjector(root, root, "demo")
            first = projector.observe()
            self.assertEqual(first["events_consumed"], 1)
            self.assertEqual(first["index"]["updated"], 2)
            self.assertEqual(first["capsules"]["built"], 2)
            self.assertGreater(first["cursor"], 0)
            self.assertEqual(projector.observe()["events_consumed"], 0)

            source.write_text("def login(): return 'second'\n", encoding="utf-8")
            updated = KernelProjector(root, root, "demo").observe()
            self.assertEqual(updated["events_consumed"], 1)
            self.assertEqual(updated["index"], {"updated": 1, "reused": 1, "removed": 0})
            self.assertEqual(updated["capsules"]["built"], 1)
            self.assertGreater(updated["cursor"], first["cursor"])
            packet = ContextCompiler(root, root, "demo").compile("login", budget_tokens=3500)
            self.assertEqual(packet.attention["trace"]["index"]["updated"], 0)
            self.assertIn("second", packet.files[0]["excerpt"])

    def test_failure_keeps_cursor_and_replays_coalesced_events(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "a.py"
            source.write_text("value = 1\n", encoding="utf-8")
            projector = KernelProjector(root, root, "demo")
            projector.world.sync()
            source.write_text("value = 2\n", encoding="utf-8")
            projector.world.sync()
            source.write_text("value = 3\n", encoding="utf-8")
            projector.world.sync()
            with patch.object(CapsuleStore, "select", side_effect=RuntimeError("cache write failed")):
                with self.assertRaisesRegex(RuntimeError, "cache write failed"):
                    projector.observe()
            self.assertFalse(projector.path.exists())
            replayed = KernelProjector(root, root, "demo").observe()
            self.assertEqual(replayed["events_consumed"], 3)
            self.assertEqual(replayed["index"]["reused"], 1)
            self.assertEqual(replayed["capsules"]["built"], 1)
            self.assertEqual(projector.observe()["cursor"], replayed["cursor"])

    def test_missing_derived_cache_repairs_without_new_event(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.py").write_text("value = 1\n", encoding="utf-8")
            projector = KernelProjector(root, root, "demo")
            first = projector.observe()
            projector.index.path.unlink()
            repaired = projector.observe()
            self.assertEqual(repaired["events_consumed"], 0)
            self.assertEqual(repaired["index"]["updated"], 1)
            self.assertEqual(repaired["cursor"], first["cursor"])


if __name__ == "__main__":
    unittest.main()
