import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from galaxy_core.brain import BrainStore
from galaxy_core.context import ContextCompiler


class ContextPacketCacheTests(unittest.TestCase):
    def test_reuses_identical_packet_and_invalidates_on_source_or_memory_change(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "repo"
            home = base / "galaxy-home"
            repo.mkdir()
            source = repo / "auth.py"
            source.write_text("def refresh_token(token):\n    return token\n", encoding="utf-8")
            compiler = ContextCompiler(home, repo, "demo")

            first = compiler.compile("fix refresh token", budget_tokens=3000, max_files=4)
            self.assertEqual(first.attention["packet_cache"]["status"], "miss")
            with patch.object(compiler, "_compile_uncached", wraps=compiler._compile_uncached) as build:
                second = compiler.compile("fix refresh token", budget_tokens=3000, max_files=4)
                build.assert_not_called()
            self.assertEqual(second.attention["packet_cache"]["status"], "hit")
            self.assertEqual(first.world_snapshot_id, second.world_snapshot_id)
            self.assertEqual([f["path"] for f in first.files], [f["path"] for f in second.files])

            source.write_text("def refresh_token(token):\n    return token.strip()\n", encoding="utf-8")
            changed_source = compiler.compile("fix refresh token", budget_tokens=3000, max_files=4)
            self.assertEqual(changed_source.attention["packet_cache"]["status"], "miss")
            self.assertNotEqual(first.world_snapshot_id, changed_source.world_snapshot_id)

            brain = BrainStore(home)
            brain.remember("decision", "Refresh token rotation policy", "Refresh tokens rotate exactly once",
                           agent="Earth", project="demo", confirmed=True)
            changed_memory = compiler.compile("fix refresh token", budget_tokens=3000, max_files=4)
            self.assertEqual(changed_memory.attention["packet_cache"]["status"], "miss")
            self.assertTrue(any("exactly once" in (item.get("summary") or "")
                                for item in changed_memory.memories))

    def test_cache_key_separates_budget_and_max_file_settings(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "repo"
            repo.mkdir()
            (repo / "a.py").write_text("def alpha(): return 1\n", encoding="utf-8")
            (repo / "b.py").write_text("def beta(): return 2\n", encoding="utf-8")
            compiler = ContextCompiler(base / "home", repo, "demo")
            compiler.compile("review code", budget_tokens=3000, max_files=2)
            with patch.object(compiler, "_compile_uncached", wraps=compiler._compile_uncached) as build:
                result = compiler.compile("review code", budget_tokens=3200, max_files=1)
                build.assert_called_once()
            self.assertEqual(result.attention["packet_cache"]["status"], "miss")

    def test_invalid_cached_packet_is_treated_as_a_miss(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "repo"
            repo.mkdir()
            (repo / "a.py").write_text("def alpha(): return 1\n", encoding="utf-8")
            compiler = ContextCompiler(base / "home", repo, "demo")
            compiler.compile("review code", budget_tokens=3000, max_files=2)
            with patch.object(compiler.packet_cache, "get", return_value={"invalid": True}):
                packet = compiler.compile("review code", budget_tokens=3000, max_files=2)
            self.assertEqual(packet.attention["packet_cache"]["status"], "miss")
            self.assertEqual(packet.project, "demo")

    def test_cache_write_failure_does_not_fail_context_compilation(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "repo"
            repo.mkdir()
            (repo / "a.py").write_text("def alpha(): return 1\n", encoding="utf-8")
            compiler = ContextCompiler(base / "home", repo, "demo")
            with patch.object(compiler.packet_cache, "put", return_value=False):
                packet = compiler.compile("review code", budget_tokens=3000, max_files=2)
            self.assertEqual(packet.attention["packet_cache"]["status"], "miss")
            self.assertFalse(packet.attention["packet_cache"]["stored"])
            self.assertLessEqual(packet.estimated_tokens, packet.budget_tokens)


if __name__ == "__main__":
    unittest.main()
