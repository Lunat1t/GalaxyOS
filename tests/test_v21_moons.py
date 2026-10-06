import tempfile
import unittest
from pathlib import Path

from galaxy_core.agents import AgentRegistry, MoonBudget, MoonResult, SubAgentManager


class PersistentAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.registry = AgentRegistry(self.root)
        self.registry.bootstrap_defaults()
        self.moons = SubAgentManager(self.registry)

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_organization_is_persistent_and_bounded(self):
        self.registry.bootstrap_defaults()
        earth = self.registry.get_agent("earth")
        self.assertEqual(earth["parent_id"], "sun")
        self.assertEqual(earth["depth"], 1)
        self.assertIn("code.write", earth["tools"])
        self.assertEqual(earth["max_parallel"], 4)
        with self.registry.session() as db:
            count = db.execute("SELECT COUNT(*) count FROM memory_blocks WHERE label='current_goals'").fetchone()["count"]
        self.assertEqual(count, 1)

    def test_shared_block_permissions_and_optimistic_version(self):
        block = self.registry.ensure_block("architecture", "project", "SQLite", project="demo")
        self.registry.attach_block("earth", block.id, "rw")
        self.registry.attach_block("mars", block.id, "r")
        changed = self.registry.write_block("earth", block.id, "PostgreSQL", expected_version=1)
        self.assertEqual(changed.version, 2)
        blocks = self.registry.context("mars", project="demo")["memory_blocks"]
        self.assertEqual(next(x for x in blocks if x["id"] == block.id)["value"], "PostgreSQL")
        with self.assertRaises(PermissionError):
            self.registry.write_block("mars", block.id, "wrong", expected_version=2)
        with self.assertRaises(RuntimeError):
            self.registry.write_block("earth", block.id, "stale", expected_version=1)

    def test_moon_inherits_only_explicit_safe_subset(self):
        moon = self.moons.spawn("earth", "Run regression tests", tools=["code.read", "tests"])
        agent = self.registry.get_agent(moon["agent_id"])
        self.assertEqual(agent["tools"], ["code.read", "tests"])
        with self.assertRaises(PermissionError):
            self.moons.spawn("earth", "Escalate", tools=["git.push"])
        with self.assertRaises(PermissionError):
            self.moons.spawn("earth", "Unknown tool", tools=["web.search"])

    def test_depth_parallel_and_aggregate_budget_are_hard_limits(self):
        first = self.moons.spawn(
            "mars", "Audit A", tools=["code.read"],
            budget=MoonBudget(1, 20_000, 5_000, 5))
        with self.assertRaises(PermissionError):
            self.moons.spawn(first["agent_id"], "Nested Moon")
        self.moons.spawn("mars", "Audit B", tools=["code.read"],
                         budget=MoonBudget(1, 20_000, 5_000, 5))
        self.moons.spawn("mars", "Audit C", tools=["code.read"],
                         budget=MoonBudget(1, 20_000, 5_000, 5))
        with self.assertRaises(RuntimeError):
            self.moons.spawn("mars", "Fourth active Moon", tools=["code.read"],
                             budget=MoonBudget(.1, 1_000, 1_000, 1))

    def test_structured_handoff_records_parent_experience(self):
        moon = self.moons.spawn("earth", "Test migration", tools=["code.read", "tests"])
        result = self.moons.complete(moon["id"], MoonResult(
            "success", "Migration tests pass", findings=["Schema is backward compatible"],
            memory_candidates=["Always test migration rollback"], evidence=["42 tests passed"],
            confidence=.93, input_tokens=1200, output_tokens=300, cost_usd=.02,
        ))
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(self.registry.get_agent(moon["agent_id"])["status"], "COMPLETED")
        for _ in range(2):
            self.registry.record_experience("earth", "success", "Always test migration rollback", confidence=.9)
        lessons = self.registry.consolidate_experience("earth")
        self.assertEqual(lessons[0]["evidence"], 3)



if __name__ == "__main__":
    unittest.main()
