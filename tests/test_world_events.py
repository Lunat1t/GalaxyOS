import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from galaxy_core.world import ProjectWorldModel


class WorldEventsTests(unittest.TestCase):
    def test_same_project_name_in_two_repositories_has_separate_state(self):
        with tempfile.TemporaryDirectory() as td:
            galaxy = Path(td) / "galaxy"
            one, two = Path(td) / "one", Path(td) / "two"
            one.mkdir(); two.mkdir()
            (one / "a.py").write_text("value = 1\n", encoding="utf-8")
            (two / "b.py").write_text("value = 2\n", encoding="utf-8")
            left = ProjectWorldModel(galaxy, one, "default")
            right = ProjectWorldModel(galaxy, two, "default")
            self.assertNotEqual(left.path, right.path)
            self.assertEqual({n.path for n in left.sync().nodes}, {"a.py"})
            self.assertEqual({n.path for n in right.sync().nodes}, {"b.py"})
            self.assertEqual({n.path for n in left.current().nodes}, {"a.py"})
            self.assertEqual([e["kind"] for e in left.events()], ["WORLD_BASELINE"])
            self.assertEqual([e["kind"] for e in right.events()], ["WORLD_BASELINE"])

    def test_baseline_change_cursor_and_noop_sync(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "a.py"
            source.write_text("def old(): pass\n", encoding="utf-8")
            world = ProjectWorldModel(root, root, "demo")
            first = world.sync()
            baseline = world.events()
            self.assertEqual(len(baseline), 1)
            self.assertEqual(baseline[0]["kind"], "WORLD_BASELINE")
            self.assertEqual(baseline[0]["changes"][0]["path"], "a.py")
            self.assertEqual(baseline[0]["after_snapshot_id"], first.snapshot_id)

            world.sync()
            self.assertEqual(len(world.events()), 1)
            source.write_text("def newer(): pass\n", encoding="utf-8")
            changed = world.sync()
            page = ProjectWorldModel(root, root, "demo").events(after_id=baseline[0]["id"])
            self.assertEqual(len(page), 1)
            self.assertEqual(page[0]["kind"], "WORLD_CHANGED")
            self.assertEqual(page[0]["before_snapshot_id"], first.snapshot_id)
            self.assertEqual(page[0]["after_snapshot_id"], changed.snapshot_id)
            self.assertEqual(page[0]["changes"][0]["change"], "modified")
            self.assertEqual(world.events(after_id=page[0]["id"]), [])

    def test_pending_event_is_replayed_once_after_interrupted_append(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "a.py"
            source.write_text("value = 1\n", encoding="utf-8")
            world = ProjectWorldModel(root, root, "demo")
            world.sync()
            source.write_text("value = 2\n", encoding="utf-8")
            with patch.object(world.event_log, "append", side_effect=RuntimeError("interrupted")):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    world.sync()
            self.assertTrue(world.pending_path.exists())
            resumed = ProjectWorldModel(root, root, "demo")
            events = resumed.events()
            self.assertEqual([e["kind"] for e in events], ["WORLD_BASELINE", "WORLD_CHANGED"])
            self.assertFalse(resumed.pending_path.exists())
            self.assertEqual(resumed.events(), events)


if __name__ == "__main__":
    unittest.main()
