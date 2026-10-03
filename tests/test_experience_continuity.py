import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from galaxy import learn_from_run
from galaxy_core.agents import AgentRegistry
from galaxy_core.brain import BrainStore
from galaxy_core.context import ContextCompiler
from galaxy_core.context.experience import ExperienceStore
from galaxy_core.benchmark.continuity import evaluate_continuity


class ExperienceContinuityTests(unittest.TestCase):
    def test_second_agent_receives_sourced_experience_and_cache_invalidates(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "repo"
            repo.mkdir()
            (repo / "auth.py").write_text("def rotate_token(token): return token\n", encoding="utf-8")
            compiler = ContextCompiler(base / "home", repo, "demo")
            first = compiler.compile("fix rotate token", budget_tokens=3500, role="Earth")
            self.assertEqual(first.experiences, [])
            eid = compiler.experience.record(project="demo", task="fix rotate token", role="Earth",
                agent_id="earth-run-1", run_id="run-1", node_id="auth", outcome="success",
                summary="Rotated the token once", lesson="Verify token rotation with the auth test",
                evidence=["run-1: pytest tests/test_auth.py passed"], verified=True)
            second = compiler.compile("fix rotate token", budget_tokens=3500, role="Moon")
            self.assertEqual(second.attention["packet_cache"]["status"], "miss")
            self.assertIn(eid, [x["id"] for x in second.experiences])
            self.assertIn("Relevant prior experience", second.to_markdown())
            self.assertLessEqual(second.estimated_tokens, second.budget_tokens)
            third = compiler.compile("fix rotate token", budget_tokens=3500, role="Moon")
            self.assertEqual(third.attention["packet_cache"]["status"], "hit")
            compiler.experience.retract(eid)
            fourth = compiler.compile("fix rotate token", budget_tokens=3500, role="Moon")
            self.assertEqual(fourth.experiences, [])
            self.assertEqual(fourth.attention["packet_cache"]["status"], "miss")

    def test_scopes_and_consolidation_require_distinct_verified_runs(self):
        with tempfile.TemporaryDirectory() as td:
            store = ExperienceStore(td)
            common = dict(project="demo", task="repair auth token", role="Earth", agent_id="earth",
                          outcome="success", summary="Auth test passed", lesson="Test rotation before deployment",
                          evidence=["pytest auth passed"], verified=True)
            store.record(**common, run_id="r1", node_id="a")
            store.record(**common, run_id="r2", node_id="a")
            private = store.record(**common, run_id="r3", node_id="a", visibility="private", owner_id="alice")
            team = store.record(**common, run_id="r4", node_id="a", visibility="team", team_id="crew")
            visible = store.relevant("repair auth token", project="demo", role="Moon", limit=10)
            self.assertNotIn(private, [x["id"] for x in visible])
            self.assertNotIn(team, [x["id"] for x in visible])
            self.assertIn(private, [x["id"] for x in store.relevant("repair auth token", project="demo", owner_id="alice", limit=10)])
            self.assertIn(team, [x["id"] for x in store.relevant("repair auth token", project="demo", team_id="crew", limit=10)])
            patterns = store.patterns(project="demo")
            self.assertEqual(patterns[0]["evidence_count"], 2)
            store.record(project="demo", task="repair auth token", role="Mars", agent_id="mars",
                         run_id="r5", node_id="a", outcome="failure", summary="Regression found",
                         lesson="Test rotation before deployment")
            self.assertEqual(store.patterns(project="demo"), [])

    def test_unverified_or_duplicate_experience_does_not_become_a_pattern(self):
        with tempfile.TemporaryDirectory() as td:
            store = ExperienceStore(td)
            common = dict(project="demo", task="fix parser", role="Earth", agent_id="earth",
                          outcome="success", summary="Parser fixed", lesson="Check UTF-8 input")
            eid = store.record(**common, run_id="r1", node_id="a", verified=True)
            store.record(**common, run_id="r1", node_id="a", verified=True)
            self.assertFalse(store.relevant("fix parser", project="demo")[0]["verified"])
            self.assertEqual(store.patterns(project="demo"), [])
            self.assertEqual(len(store.relevant("fix parser", project="demo")), 1)
            self.assertEqual(eid, store.relevant("fix parser", project="demo")[0]["id"])

    def test_same_project_name_in_separate_repositories_does_not_share_episodes(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            left = Path(td) / "left"
            right = Path(td) / "right"
            left.mkdir(); right.mkdir()
            one = ExperienceStore(home, left)
            two = ExperienceStore(home, right)
            one.record(project="demo", task="fix parser", role="Earth", agent_id="earth",
                       run_id="r1", node_id="parse", outcome="success", summary="parser fixed")
            self.assertEqual(two.relevant("fix parser", project="demo"), [])

    def test_ordered_replay_rejects_future_gold_and_reports_retrieval_only(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            (repo / "parser.py").write_text("def parse_utf8(value): return value\n", encoding="utf-8")
            first = {"task": "fix utf8 parser", "role": "Earth", "observation": {
                "run_id": "one", "node_id": "parser", "outcome": "success",
                "summary": "UTF-8 parser repaired", "lesson": "Test utf8 parser inputs",
                "evidence": ["test_parser passed"], "verified": True}}
            second = {"task": "test utf8 parser", "role": "Moon", "gold_prior": [["one", "parser"]]}
            with self.assertRaises(ValueError):
                evaluate_continuity({"tasks": [second, first]}, repo)
            report = evaluate_continuity({"tasks": [first, second]}, repo)
            self.assertEqual(report["retrieval_recall"], 1.0)
            self.assertEqual(report["tasks"], 2)
            self.assertIn("no task success", report["limitations"])

    def test_learning_hook_does_not_assert_unverified_success_or_failure_as_qa(self):
        with tempfile.TemporaryDirectory() as td:
            brain = BrainStore(td)
            agents = AgentRegistry(td)
            agents.bootstrap_defaults()
            experience = ExperienceStore(td)
            node = SimpleNamespace(id="auth", role="Earth", capability="implementation",
                                   objective="fix token rotation", verification_commands=[])
            plan = SimpleNamespace(goal="auth repair", project="demo", nodes=[node])
            result = {"status": "PASS", "summary": "Implemented auth", "confidence": .7,
                      "evidence": ["model says done"]}
            learn_from_run("run-pass", plan, {"nodes": [{"node_id": "auth", "status": "SUCCEEDED", "result": result}]},
                           brain, agents, experience)
            self.assertFalse(experience.relevant("fix token rotation", project="demo")[0]["verified"])
            memories = brain.search("auth repair", project="demo")
            self.assertTrue(memories)
            self.assertFalse(memories[0]["qa_pass"])
            failed = {"status": "FAIL", "summary": "Auth regression", "confidence": .9,
                      "memory_candidates": ["Do not skip token rotation tests"]}
            learn_from_run("run-fail", plan, {"nodes": [{"node_id": "auth", "status": "FAILED", "result": failed}]},
                           brain, agents, experience)
            episodes = experience.relevant("fix token rotation", project="demo")
            self.assertEqual({e["outcome"] for e in episodes}, {"success", "failure"})
            self.assertEqual(len(brain.search("auth repair", project="demo")), 1)


if __name__ == "__main__":
    unittest.main()
