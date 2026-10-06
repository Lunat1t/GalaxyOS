import asyncio
from dataclasses import asdict
from pathlib import Path
import tempfile
import time
import unittest

from galaxy_core.agents import (
    AgentRegistry, AutomaticTeamBuilder, MoonResult, TeamOutcome, TeamPlan, TeamPolicy, TeamTask,
)
from galaxy_core.engine.autonomy import ModelRoute, NodeResult, WorkNode
from galaxy_core.engine.planning import TeamAwareExecutor


def plan(tasks, gain=.4):
    return TeamPlan(
        "Analyze architecture, migration, security and tests for the integration",
        tuple(tasks), "Independent specialists reduce blind spots and latency", gain,
    )


class AutomaticTeamBuilderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "README.md").write_text("fixture\n", encoding="utf-8")
        self.registry = AgentRegistry(self.root)
        self.registry.bootstrap_defaults()

    def tearDown(self):
        self.tmp.cleanup()

    def builder(self, team_plan, executor, **policy):
        async def planner(**kwargs):
            return team_plan
        return AutomaticTeamBuilder(
            self.registry, planner, executor,
            TeamPolicy(max_tasks=4, max_parallel=4, **policy),
        )

    def test_simple_objective_stays_with_parent_without_planner_call(self):
        called = False

        async def planner(**kwargs):
            nonlocal called
            called = True
            raise AssertionError("planner must not be called")

        builder = AutomaticTeamBuilder(self.registry, planner, lambda **kwargs: None)
        outcome = builder.run("earth", "Rename one local variable")
        self.assertFalse(outcome.delegated)
        self.assertEqual(outcome.status, "SKIPPED")
        self.assertFalse(called)

    def test_independent_moons_execute_in_parallel_and_join_results(self):
        starts = {}
        team_plan = plan([
            TeamTask("architecture", "Architecture", "Inspect architecture", "architecture", ("code.read",)),
            TeamTask("tests", "Tests", "Inspect test coverage", "qa", ("code.read", "tests")),
        ])

        async def executor(*, task, **kwargs):
            starts[task.id] = time.perf_counter()
            await asyncio.sleep(.06)
            return MoonResult(
                "success", f"{task.id} complete", findings=[f"finding:{task.id}"],
                evidence=[f"evidence:{task.id}"], memory_candidates=["Use independent review"],
                confidence=.9, input_tokens=100, output_tokens=25, cost_usd=.01,
            )

        before = time.perf_counter()
        outcome = self.builder(team_plan, executor).run(
            "earth", team_plan.objective, force=True, project="demo")
        elapsed = time.perf_counter() - before
        self.assertTrue(outcome.delegated)
        self.assertEqual(outcome.status, "SUCCEEDED")
        self.assertLess(abs(starts["architecture"] - starts["tests"]), .04)
        self.assertLess(elapsed, .2)
        self.assertEqual(set(outcome.findings), {"finding:architecture", "finding:tests"})
        self.assertEqual(outcome.memory_candidates, ["Use independent review"])
        self.assertAlmostEqual(outcome.cost_usd, .02)
        self.assertEqual(self.builder(team_plan, executor).status(outcome.team_id)["status"], "SUCCEEDED")

    def test_dependencies_wait_for_verified_predecessor(self):
        order = []
        team_plan = plan([
            TeamTask("inspect", "Inspect", "Inspect schema", "research", ("code.read",)),
            TeamTask("review", "Review", "Review findings", "qa", ("code.read",), ("inspect",)),
        ])

        async def executor(*, task, context, **kwargs):
            order.append(task.id)
            if task.id == "review":
                self.assertEqual(context["dependencies"]["inspect"]["status"], "success")
            return MoonResult("success", task.id, confidence=.8)

        outcome = self.builder(team_plan, executor).run("earth", team_plan.objective, force=True)
        self.assertEqual(outcome.status, "SUCCEEDED")
        self.assertEqual(order, ["inspect", "review"])

    def test_failure_stops_pending_team_work(self):
        called = []
        team_plan = plan([
            TeamTask("risk", "Risk", "Inspect risk", "security", ("code.read",)),
            TeamTask("followup", "Follow-up", "Use risk result", "qa", ("code.read",), ("risk",)),
        ])

        async def executor(*, task, **kwargs):
            called.append(task.id)
            return MoonResult("failure", "risk check failed", confidence=.2)

        outcome = self.builder(team_plan, executor).run("mars", team_plan.objective, force=True)
        self.assertEqual(outcome.status, "FAILED")
        self.assertEqual(called, ["risk"])
        statuses = {row["task_id"]: row["status"] for row in outcome.tasks}
        self.assertEqual(statuses["risk"], "FAILED")
        self.assertIn(statuses["followup"], {"BLOCKED", "SKIPPED"})

    def test_plan_cannot_delegate_mutation_or_escalate_tools(self):
        async def executor(**kwargs):
            return MoonResult("success", "unused")

        mutating = plan([
            TeamTask("write", "Write", "Edit code", tools=("code.write",)),
            TeamTask("check", "Check", "Check code", tools=("code.read",)),
        ])
        with self.assertRaises(PermissionError):
            self.builder(mutating, executor).run("earth", mutating.objective, force=True)

        escalated = plan([
            TeamTask("web", "Web", "Search web", tools=("web.search",)),
            TeamTask("check", "Check", "Check code", tools=("code.read",)),
        ])
        with self.assertRaises(PermissionError):
            self.builder(escalated, executor).run("earth", escalated.objective, force=True)

    def test_total_team_reservation_is_bounded_by_parent(self):
        tasks = [TeamTask(f"t{i}", f"T{i}", f"Inspect {i}", tools=("code.read",)) for i in range(4)]
        team_plan = plan(tasks)

        async def executor(**kwargs):
            return MoonResult("success", "ok")

        builder = self.builder(team_plan, executor)
        parent = self.registry.get_agent("earth")
        allocated = builder._allocate(team_plan, parent)
        allocated.validate(parent, builder.policy)
        self.assertLessEqual(sum(x.budget.max_cost_usd for x in allocated.tasks),
                             parent["budget"]["max_cost_usd"])
        self.assertLessEqual(sum(x.budget.max_calls for x in allocated.tasks),
                             parent["budget"]["max_calls"])
        builder._create_team("earth", allocated, 5.0, "first", "demo", None, None)
        with self.assertRaises(RuntimeError):
            builder._create_team("earth", allocated, 5.0, "second", "demo", None, None)

    def test_interrupted_task_is_cancelled_and_resumed(self):
        team_plan = plan([
            TeamTask("a", "A", "Inspect A", tools=("code.read",)),
            TeamTask("b", "B", "Inspect B", tools=("code.read",)),
        ])
        calls = []

        async def executor(*, task, **kwargs):
            calls.append(task.id)
            return MoonResult("success", f"resumed {task.id}", confidence=.9)

        builder = self.builder(team_plan, executor)
        parent = self.registry.get_agent("earth")
        allocated = builder._allocate(team_plan, parent)
        allocated.validate(parent, builder.policy)
        team_id = builder._create_team("earth", allocated, 5.0, "test", "demo", "RUN-1", "node-1")
        first = allocated.tasks[0]
        moon = builder.moons.spawn("earth", first.mission, tools=first.tools,
                                   budget=first.budget, run_id=team_id)
        builder._set_task(team_id, first.id, "RUNNING", moon_run_id=moon["id"],
                          attempts_delta=1, started=True)
        outcome = builder.run("earth", allocated.objective, resume_team_id=team_id)
        self.assertEqual(outcome.status, "SUCCEEDED")
        self.assertEqual(set(calls), {"a", "b"})
        self.assertEqual(builder.moons.get(moon["id"])["status"], "CANCELLED")
        task_a = next(row for row in outcome.tasks if row["task_id"] == "a")
        self.assertEqual(task_a["attempts"], 2)

    def test_registry_sessions_close_and_survive_repeated_team_runs(self):
        team_plan = plan([
            TeamTask("a", "A", "Inspect A", tools=("code.read",)),
            TeamTask("b", "B", "Inspect B", tools=("code.read",)),
        ])

        async def executor(*, task, **kwargs):
            return MoonResult("success", task.id, confidence=.9)

        for _ in range(20):
            outcome = self.builder(team_plan, executor).run(
                "earth", team_plan.objective, force=True)
            self.assertEqual(outcome.status, "SUCCEEDED")
        with self.registry.session() as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM team_runs").fetchone()[0], 20)

    def test_low_expected_gain_rejects_unhelpful_team(self):
        team_plan = plan([
            TeamTask("a", "A", "Inspect A", tools=("code.read",)),
            TeamTask("b", "B", "Inspect B", tools=("code.read",)),
        ], gain=.01)
        builder = self.builder(team_plan, lambda **kwargs: MoonResult("success", "unused"))
        outcome = builder.run("earth", team_plan.objective)
        self.assertFalse(outcome.delegated)
        self.assertEqual(outcome.status, "SKIPPED")

    def test_team_aware_executor_uses_joined_result_for_read_node(self):
        base_called = False

        async def base(*args, **kwargs):
            nonlocal base_called
            base_called = True
            return NodeResult("PASS", "base")

        class FakeBuilder:
            async def run_async(self, *args, **kwargs):
                return TeamOutcome(True, "SUCCEEDED", "joined", "useful", "TEAM-X",
                                   findings=["fact"], evidence=["proof"], confidence=.9)

        wrapper = TeamAwareExecutor(
            self.root, self.registry, base=base,
            builder_factory=lambda route: FakeBuilder())
        node = WorkNode("research", "Research", "Research architecture security migration tests",
                        role="Ceres", capability="research")
        route = ModelRoute("fake", "fake", None, 0, 1)
        evidence = self.root / "evidence"; evidence.mkdir()
        result = asyncio.run(wrapper(node, {"project": "demo"}, route, self.root, evidence))
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.summary, "joined")
        self.assertEqual(result.findings, ["fact"])
        self.assertFalse(base_called)

    def test_team_aware_executor_advises_but_parent_owns_write(self):
        received = {}

        async def base(node, context, route, workdir, evidence):
            received.update(context)
            return NodeResult("PASS", "parent wrote safely", findings=["parent"])

        class FakeBuilder:
            async def run_async(self, *args, **kwargs):
                return TeamOutcome(True, "SUCCEEDED", "advice", "useful", "TEAM-Y",
                                   findings=["moon"], evidence=["review"], confidence=.8)

        wrapper = TeamAwareExecutor(
            self.root, self.registry, base=base,
            builder_factory=lambda route: FakeBuilder())
        node = WorkNode("implementation", "Implement", "Implement migration with tests and security review",
                        role="Earth", capability="implementation", risk="write",
                        write_scopes=("src/*",))
        route = ModelRoute("fake", "fake", None, 0, 1)
        evidence = self.root / "write-evidence"; evidence.mkdir()
        result = asyncio.run(wrapper(node, {"project": "demo"}, route, self.root, evidence))
        self.assertIn("automatic_team", received)
        self.assertEqual(result.findings, ["moon", "parent"])
        self.assertEqual(result.summary, "parent wrote safely")

    def test_shared_model_limit_covers_planners_and_moons_across_nodes(self):
        active = 0
        maximum_active = 0
        calls = 0

        async def provider_call(delay):
            nonlocal active, maximum_active, calls
            active += 1
            calls += 1
            maximum_active = max(maximum_active, active)
            try:
                await asyncio.sleep(delay)
            finally:
                active -= 1

        def planner_factory(route):
            async def planner(**kwargs):
                await provider_call(.01)
                return plan([
                    TeamTask("inspect", "Inspect", "Inspect architecture", tools=()),
                    TeamTask("verify", "Verify", "Verify risks and tests", tools=()),
                ], gain=.5)
            return planner

        def moon_executor_factory(route):
            async def executor(*, task, **kwargs):
                await provider_call(.03)
                return MoonResult("success", task.id, confidence=.9)
            return executor

        wrapper = TeamAwareExecutor(
            self.root, self.registry,
            policy=TeamPolicy(max_parallel=4), profile_limits={"fake": 2},
            planner_factory=planner_factory, moon_executor_factory=moon_executor_factory,
        )
        route = ModelRoute("fake", "fake", None, 0, 1)
        nodes = [
            WorkNode("ceres-node", "Research", "Analyze architecture, migration, security and tests",
                     role="Ceres", capability="research"),
            WorkNode("mars-node", "Design", "Analyze architecture, migration, security and tests",
                     role="Mars", capability="architecture"),
        ]

        async def run_both():
            evidence = []
            for node in nodes:
                path = self.root / f"evidence-{node.id}"
                path.mkdir()
                evidence.append(path)
            return await asyncio.gather(*[
                wrapper(node, {"project": "demo"}, route, self.root, path)
                for node, path in zip(nodes, evidence)
            ])

        results = asyncio.run(run_both())
        self.assertTrue(all(result.status == "PASS" for result in results))
        self.assertEqual(calls, 6)
        self.assertEqual(maximum_active, 2)


if __name__ == "__main__":
    unittest.main()
