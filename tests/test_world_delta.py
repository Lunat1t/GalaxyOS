import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from galaxy_core.world import ProjectWorldModel
from galaxy_core.world.scanner import ProjectScanner


class IncrementalWorldTests(unittest.TestCase):
    def test_ast_inheritance_edges_are_resolved_and_rebuilt_incrementally(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "fields").mkdir()
            parent = root / "fields" / "__init__.py"
            child = root / "fields" / "related.py"
            parent.write_text("class Field:\n    pass\n", encoding="utf-8")
            child.write_text("from . import Field as ParentField\nclass RelatedField(ParentField):\n    pass\n", encoding="utf-8")
            world = ProjectWorldModel(root, root, "demo")
            first = world.sync()
            edge_set = {(edge.source, edge.target, edge.relation) for edge in first.edges}
            self.assertIn(("fields/related.py", "fields/__init__.py", "inherits"), edge_set)
            self.assertIn(("fields/related.py", "fields/__init__.py", "imports"), edge_set)
            self.assertEqual(next(node for node in first.nodes if node.path.endswith("related.py")).bases, ("Field",))

            parent.write_text("class BaseField:\n    pass\n", encoding="utf-8")
            delta, _ = world.sync_with_drift()
            full = ProjectScanner(root, "demo").scan()
            self.assertEqual(delta.edges, full.edges)
            self.assertNotIn(("fields/related.py", "fields/__init__.py", "inherits"),
                             {(edge.source, edge.target, edge.relation) for edge in delta.edges})

    def test_modified_source_rebuilds_its_edges_and_matches_full_scan(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "pkg").mkdir()
            (root / "pkg" / "a.py").write_text("class Alpha: pass\n", encoding="utf-8")
            (root / "pkg" / "b.py").write_text("class Beta: pass\n", encoding="utf-8")
            source = root / "pkg" / "use.py"
            source.write_text("from .a import Alpha\n", encoding="utf-8")
            world = ProjectWorldModel(root, root, "demo")
            world.sync()
            source.write_text("from .b import Beta\n", encoding="utf-8")

            with patch.object(ProjectScanner, "scan", side_effect=AssertionError("full scan used")):
                delta, drift = world.sync_with_drift()
            full = ProjectScanner(root, "demo").scan()
            self.assertEqual(world.last_sync, {"mode": "delta", "changed_files": 1})
            self.assertEqual(delta.nodes, full.nodes)
            self.assertEqual(delta.edges, full.edges)
            self.assertEqual(delta.stats, full.stats)
            self.assertTrue(drift.has_drift)
            self.assertIn(("pkg/use.py", "pkg/b.py"), {(e.source, e.target) for e in delta.edges})
            self.assertNotIn(("pkg/use.py", "pkg/a.py"), {(e.source, e.target) for e in delta.edges})

    def test_unchanged_sync_does_not_read_source_content(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.py").write_text("def stable(): pass\n", encoding="utf-8")
            world = ProjectWorldModel(root, root, "demo")
            first = world.sync()
            with patch.object(Path, "read_bytes", side_effect=AssertionError("source reread")):
                second = world.sync()
            self.assertEqual(world.last_sync, {"mode": "delta", "changed_files": 0})
            self.assertEqual(first.nodes, second.nodes)
            self.assertEqual(first.edges, second.edges)

    def test_added_path_falls_back_to_full_scan_to_resolve_existing_import(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "use.py").write_text("import missing\n", encoding="utf-8")
            world = ProjectWorldModel(root, root, "demo")
            world.sync()
            (root / "missing.py").write_text("answer = 42\n", encoding="utf-8")
            snapshot, drift = world.sync_with_drift()
            self.assertEqual(world.last_sync["mode"], "full")
            self.assertIn(("use.py", "missing.py"), {(e.source, e.target) for e in snapshot.edges})
            self.assertEqual(drift.to_dict()["summary"]["files_added"], 1)


if __name__ == "__main__":
    unittest.main()
