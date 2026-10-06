import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from galaxy_core.kernel import KernelProjector, KernelWatcher


class KernelWatcherTests(unittest.TestCase):
    def test_debounce_coalesces_writes_and_ignores_runtime_cache(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "a.py"
            source.write_text("value = 1\n", encoding="utf-8")
            watcher = KernelWatcher(KernelProjector(root, root, "demo"), interval=0.1, debounce=0.5)
            self.assertEqual(watcher.start(now=0)["events_consumed"], 1)
            self.assertIsNone(watcher.tick(now=0.1))  # cache files do not trigger another projection

            source.write_text("value = 2\n", encoding="utf-8")
            self.assertIsNone(watcher.tick(now=1.0))
            source.write_text("value = 3\n", encoding="utf-8")
            self.assertIsNone(watcher.tick(now=1.3))
            self.assertIsNone(watcher.tick(now=1.7))
            result = watcher.tick(now=1.8)
            self.assertIsNotNone(result)
            self.assertEqual(result["events_consumed"], 1)
            self.assertEqual(result["index"]["updated"], 1)
            self.assertIsNone(watcher.tick(now=2.0))

    def test_failed_projection_retries_at_next_tick(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "a.py"
            source.write_text("value = 1\n", encoding="utf-8")
            projector = KernelProjector(root, root, "demo")
            watcher = KernelWatcher(projector, debounce=0.2)
            watcher.start(now=0)
            source.write_text("value = 2\n", encoding="utf-8")
            watcher.tick(now=1.0)
            with patch.object(projector, "observe", side_effect=RuntimeError("temporary failure")):
                with self.assertRaisesRegex(RuntimeError, "temporary failure"):
                    watcher.tick(now=1.3)
            self.assertEqual(watcher.tick(now=1.4)["events_consumed"], 1)

    def test_added_and_deleted_paths_trigger_projection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.py").write_text("value = 1\n", encoding="utf-8")
            watcher = KernelWatcher(KernelProjector(root, root, "demo"), debounce=0.1)
            watcher.start(now=0)
            added = root / "b.py"
            added.write_text("new_value = 2\n", encoding="utf-8")
            self.assertIsNone(watcher.tick(now=1.0))
            self.assertEqual(watcher.tick(now=1.2)["index"]["updated"], 1)
            added.unlink()
            self.assertIsNone(watcher.tick(now=2.0))
            result = watcher.tick(now=2.2)
            self.assertEqual(result["events_consumed"], 1)
            self.assertEqual(result["index"]["removed"], 1)


if __name__ == "__main__":
    unittest.main()
