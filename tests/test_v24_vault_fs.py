"""Tests for Galaxy Open Vault Knowledge File System."""

import tempfile
import unittest
from pathlib import Path

from galaxy_core.vault.engine import VaultEngine
from galaxy_core.vault.parser import parse_frontmatter


class VaultKnowledgeFSTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="galaxy_vault_test_")
        self.vault_root = Path(self.temp_dir.name)
        self.engine = VaultEngine(self.vault_root)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_parse_frontmatter(self):
        text = """---
title: OAuth Architecture
tags: [auth, security, backend]
published: true
rating: 4.5
---
# OAuth Overview
Here is the specification.
"""
        fm, body = parse_frontmatter(text)
        self.assertEqual(fm["title"], "OAuth Architecture")
        self.assertEqual(fm["tags"], ["auth", "security", "backend"])
        self.assertTrue(fm["published"])
        self.assertEqual(fm["rating"], 4.5)
        self.assertIn("# OAuth Overview", body)

    def test_write_and_read_note_with_wikilinks(self):
        # Create target note first
        self.engine.write_note(
            "specs/database",
            content="PostgreSQL and SQLite storage schema.",
            title="Database Architecture",
            frontmatter={"tags": ["db", "backend"]},
        )

        # Create note linking to database
        self.engine.write_note(
            "specs/api",
            content="API layer connects to [[specs/database]] and handles JWT auth. #api #auth",
            title="API Specification",
            frontmatter={"tags": ["api"]},
        )

        # Read database note and check calculated backlinks
        db_note = self.engine.read_note("specs/database")
        self.assertIsNotNone(db_note)
        self.assertEqual(db_note["title"], "Database Architecture")
        self.assertIn("db", db_note["tags"])

        backlinks = db_note["backlinks"]
        self.assertEqual(len(backlinks), 1)
        self.assertEqual(backlinks[0]["source_id"], "specs/api")
        self.assertEqual(backlinks[0]["target_id"], "specs/database")
        self.assertIn("connects to [[specs/database]]", backlinks[0]["context_snippet"])

    def test_fts5_search(self):
        self.engine.write_note(
            "notes/crdt",
            content="Yjs and Automerge are state of the art conflict-free replicated data types.",
            title="CRDT Deep Dive",
            frontmatter={"tags": ["crdt", "realtime"]},
        )
        self.engine.write_note(
            "notes/security",
            content="SecOps audit for Mars agent detects SQL injection and secret leaks.",
            title="Security Audit Guide",
            frontmatter={"tags": ["secops"]},
        )

        # Search for crdt
        results = self.engine.search("conflict-free")
        self.assertGreaterEqual(len(results), 1)
        self.assertEqual(results[0]["note_id"], "notes/crdt")
        self.assertIn("<b>conflict-free</b>", results[0]["snippet"])

        # Search for Mars
        sec_results = self.engine.search("injection")
        self.assertGreaterEqual(len(sec_results), 1)
        self.assertEqual(sec_results[0]["note_id"], "notes/security")

    def test_knowledge_graph_topology(self):
        self.engine.write_note("node-a", "Links to [[node-b]] and [[node-c]].")
        self.engine.write_note("node-b", "Links to [[node-c]].")
        self.engine.write_note("node-c", "Terminal node with no outgoing links.")

        graph = self.engine.get_graph()
        self.assertEqual(graph["total_nodes"], 3)
        self.assertEqual(graph["total_edges"], 3)

        edges = graph["edges"]
        edge_pairs = [(e["source"], e["target"]) for e in edges]
        self.assertIn(("node-a", "node-b"), edge_pairs)
        self.assertIn(("node-a", "node-c"), edge_pairs)
        self.assertIn(("node-b", "node-c"), edge_pairs)

    def test_incremental_sync(self):
        # Write directly to disk bypassing engine
        file_path = self.vault_root / "manual-note.md"
        file_path.write_text(
            "---\ntitle: Manual Note\ntags: [disk]\n---\nCreated directly on disk with [[node-a]].",
            encoding="utf-8",
        )

        # Run sync
        sync_res = self.engine.sync()
        self.assertGreaterEqual(sync_res["indexed"], 1)

        note = self.engine.read_note("manual-note")
        self.assertIsNotNone(note)
        self.assertEqual(note["title"], "Manual Note")
        self.assertIn("disk", note["tags"])


if __name__ == "__main__":
    unittest.main()
