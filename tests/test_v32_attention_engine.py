import tempfile
import unittest
import sqlite3
import hashlib
import random
from pathlib import Path
from types import SimpleNamespace

from galaxy_core.attention import AdaptiveBudgeter, AdaptiveSemanticSearch, AttentionEngine
from galaxy_core.context import ContextCompiler
from galaxy_core.world import ProjectWorldModel
from galaxy_core.world.models import WorldEdge, WorldNode, WorldSnapshot


class AttentionEngineTests(unittest.TestCase):
    def test_experimental_graph_expansion_is_directed_and_one_hop_only(self):
        snapshot = WorldSnapshot(
            project="demo", root="/tmp/demo", generated_at="now",
            nodes=[WorldNode(id=p, path=p, kind="source") for p in ("seed.py", "imported.py", "base.py", "dependent.py", "transitive.py")],
            edges=[
                WorldEdge("seed.py", "imported.py", "imports", 0.98),
                WorldEdge("seed.py", "base.py", "inherits", 0.95),
                WorldEdge("dependent.py", "seed.py", "imports", 0.98),
                WorldEdge("imported.py", "transitive.py", "imports", 0.98),
            ],
        )
        scores = AttentionEngine._one_hop_expand(snapshot, ["seed.py"])
        self.assertEqual(set(scores), {"imported.py", "base.py"})
        self.assertEqual(scores["base.py"], 0.475)

    def test_adaptive_semantic_policy_selects_exact_and_bounded_modes(self):
        small = AdaptiveSemanticSearch.recommend("fix auth", 400, 72)
        large = AdaptiveSemanticSearch.recommend("fix auth", 100_000, 72)
        broad = AdaptiveSemanticSearch.recommend(" ".join(f"term{i}" for i in range(30)), 100_000, 72)
        forced = AdaptiveSemanticSearch.recommend("fix auth", 400, 72, "ann")
        self.assertEqual(small.mode, "exact")
        self.assertEqual(large.mode, "ann")
        self.assertLess(large.candidate_limit, large.total_vectors)
        self.assertGreater(broad.candidate_limit, large.candidate_limit)
        self.assertEqual(forced.mode, "ann")
        self.assertEqual(forced.candidate_limit, 399)

    def test_old_derived_index_rebuilds_for_postings(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "module.py").write_text("def cache():\n    return 'persistent'\n", encoding="utf-8")
            snap = ProjectWorldModel(root, root, "demo").sync()
            engine = AttentionEngine(root, "demo")
            engine.index.path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(engine.index.path) as db:
                db.execute("CREATE TABLE documents (path TEXT PRIMARY KEY, signature TEXT NOT NULL, payload TEXT NOT NULL)")
                db.execute("INSERT INTO documents VALUES ('old.py', 'old', '{}')")
            result = engine.build("persistent cache", snap)
            self.assertEqual(result.trace["index"]["updated"], 1)
            self.assertIn("module.py", engine.index.bm25("persistent cache"))
            self.assertNotIn("old.py", engine.index.bm25("old"))

    def test_persistent_index_reuses_and_updates_only_changed_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.py").write_text("def alpha():\n    return 'apple'\n", encoding="utf-8")
            (root / "b.py").write_text("def beta():\n    return 'banana'\n", encoding="utf-8")
            world = ProjectWorldModel(root, root, "demo")
            engine = AttentionEngine(root, "demo")
            snap = world.sync()
            first = engine.build("apple alpha", snap)
            second = AttentionEngine(root, "demo").build("apple alpha", world.load())
            self.assertEqual(first.trace["index"], {"updated": 2, "reused": 0, "removed": 0})
            self.assertEqual(second.trace["index"], {"updated": 0, "reused": 2, "removed": 0})
            self.assertEqual(first.trace["ranked_paths"], second.trace["ranked_paths"])
            self.assertEqual(first.selected[0].excerpt, second.selected[0].excerpt)
            engine.index.prepare(snap.nodes)
            baseline = engine._bm25("apple alpha", engine.index.documents([n.path for n in snap.nodes]))
            self.assertEqual(engine.index.bm25("apple alpha"), baseline)
            self.assertEqual(engine.index.semantic("apple alpha"), engine._semantic("apple alpha", snap))

            (root / "a.py").write_text("def alpha():\n    return 'orange'\n", encoding="utf-8")
            (root / "b.py").unlink()
            changed = engine.build("orange alpha", world.sync())
            self.assertEqual(changed.trace["index"], {"updated": 1, "reused": 0, "removed": 1})
            self.assertIn("orange", changed.selected[0].excerpt)
            self.assertNotIn("b.py", changed.trace["ranked_paths"])
            engine.index.prepare(world.load().nodes)
            baseline = engine._bm25("orange alpha", engine.index.documents([n.path for n in world.load().nodes]))
            self.assertEqual(engine.index.bm25("orange alpha"), baseline)

    def test_evidence_loads_only_ranked_candidates(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for i in range(40):
                (root / f"file_{i:02}.py").write_text(f"def unrelated_{i}():\n    return {i}\n", encoding="utf-8")
            (root / "target.py").write_text("def session_refresh():\n    return 'token'\n", encoding="utf-8")
            snap = ProjectWorldModel(root, root, "demo").sync()
            result = AttentionEngine(root, "demo").build("fix session refresh token", snap, max_candidates=5)
            self.assertLessEqual(result.trace["loaded_documents"], 5)
            self.assertIn("target.py", result.trace["ranked_paths"])

    def test_ann_scores_bounded_candidates_with_high_recall_and_removes_buckets(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rng = random.Random(13)
            topics = [
                "authentication session token login", "database schema migration transaction",
                "context compiler attention retrieval", "billing refund invoice payment",
                "cache invalidation performance redis", "watcher snapshot filesystem event",
            ]
            nodes = []
            for i in range(1200):
                body = (f"def function_{i}():\n    # {topics[i % len(topics)]} "
                        + " ".join(f"noise{rng.randrange(1000)}" for _ in range(8))
                        + f"\n    return {i}\n")
                path = root / f"module_{i:04}.py"
                path.write_text(body, encoding="utf-8")
                nodes.append(SimpleNamespace(
                    path=path.name, content_hash=hashlib.sha256(body.encode()).hexdigest()[:16],
                    symbols=(f"function_{i}",), summary=body.splitlines()[1],
                    component="root", kind="source",
                ))
            index = AttentionEngine(root, "ann-quality").index
            index.prepare(nodes)
            query = "fix billing refund invoice payment"
            exact = index.semantic(query)
            approximate, stats = index.semantic_search(query, candidate_limit=256, exact_threshold=0)
            exact_top = {p for p, _ in sorted(exact.items(), key=lambda item: (-item[1], item[0]))[:20]}
            ann_top = {p for p, _ in sorted(approximate.items(), key=lambda item: (-item[1], item[0]))[:20]}
            self.assertEqual(stats["mode"], "ann")
            self.assertEqual(stats["scored_vectors"], 256)
            self.assertGreaterEqual(len(exact_top & ann_top) / 20, 0.90)

            removed = nodes[-1].path
            (root / removed).unlink()
            index.prepare(nodes[:-1])
            with sqlite3.connect(index.path) as db:
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM vector_buckets WHERE path = ?", (removed,)
                ).fetchone()[0], 0)

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
