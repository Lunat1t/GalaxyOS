import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest

from galaxy_core.engine.autonomy import (
    AdaptiveModelRouter, AutonomousEngine, BudgetLimits, BudgetUsage,
    ExecutionPlan, ModelProfile, NodeResult, WorkNode,
)


def node(node_id, *, deps=(), risk="read", scopes=(), capability="research",
         attempts=1, tokens=100, idempotency=None, commands=()):
    return WorkNode(
        node_id, node_id, f"Complete {node_id}", role="Earth",
        capability=capability, dependencies=tuple(deps), risk=risk,
        write_scopes=tuple(scopes), acceptance_criteria=(f"{node_id} complete",),
        verification_commands=tuple(commands),
        estimated_input_tokens=tokens, estimated_output_tokens=tokens,
        max_attempts=attempts, timeout_seconds=5, idempotency_key=idempotency,
    )


def plan(*nodes):
    return ExecutionPlan("Build the thing", "test", tuple(nodes), ("All checks pass",))


def profile(**updates):
    values = dict(name="test-model", provider="fake",
                  capabilities=("general", "research", "implementation", "verification", "qa"),
                  quality=.8, latency_score=.8, max_parallel=8)
    values.update(updates)
    return ModelProfile(**values)


class PlanTests(unittest.TestCase):
    def test_cycle_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "cycle"):
            plan(node("a", deps=("b",)), node("b", deps=("a",))).validate()

    def test_unknown_dependency_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown dependencies"):
            plan(node("a", deps=("missing",))).validate()

    def test_external_requires_idempotency_key(self):
        with self.assertRaisesRegex(ValueError, "idempotency_key"):
            plan(node("notify", risk="external")).validate()

    def test_unknown_role_and_read_only_role_write_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown role"):
            plan(WorkNode("alien", "Alien", "Unknown", role="Alien")).validate()
        with self.assertRaisesRegex(ValueError, "no write authority"):
            WorkNode("report", "Report", "Write report", role="Mercury", risk="write",
                     write_scopes=("report.md",)).validate()

    def test_write_requires_downstream_verification(self):
        writer = node("write", risk="write", scopes=("src/*.py",), capability="implementation")
        with self.assertRaisesRegex(ValueError, "verification"):
            plan(writer).validate()
        unrelated_qa = node("qa", capability="verification", commands=('python -c "pass"',))
        with self.assertRaisesRegex(ValueError, "depend"):
            plan(writer, unrelated_qa).validate()

    def test_layers_expose_parallel_frontier(self):
        graph = plan(node("a"), node("b"), node("merge", deps=("a", "b"), capability="qa"))
        self.assertEqual(graph.layers(), [["a", "b"], ["merge"]])


class RouterTests(unittest.TestCase):
    def test_router_uses_capability_then_quality(self):
        profiles = [
            profile(name="cheap", quality=.6, capabilities=("research",)),
            profile(name="strong", quality=.95, capabilities=("implementation",)),
        ]
        selected = AdaptiveModelRouter(profiles).select(
            node("code", capability="implementation"), remaining_cost=10)
        self.assertEqual(selected.profile, "strong")

    def test_router_respects_cost_budget(self):
        costly = profile(input_cost_per_million=1000, output_cost_per_million=1000)
        with self.assertRaisesRegex(RuntimeError, "within budget"):
            AdaptiveModelRouter([costly]).select(node("a", tokens=1000), remaining_cost=.01)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "src").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def engine(self, executor, parallel=4, model=None):
        return AutonomousEngine(self.root, [model or profile()], executor, max_parallel=parallel)

    def test_independent_nodes_execute_in_parallel(self):
        starts = {}

        async def execute(work, context, route, workdir, evidence):
            starts[work.id] = time.monotonic()
            if work.id in {"a", "b"}:
                await asyncio.sleep(.12)
            return NodeResult("PASS", f"{work.id} done")

        graph = plan(node("a"), node("b"), node("join", deps=("a", "b"), capability="qa"))
        runner = self.engine(execute)
        run_id = runner.start(graph, approval_mode="off")
        before = time.monotonic(); result = runner.run(run_id); elapsed = time.monotonic() - before
        self.assertEqual(result["status"], "DONE")
        self.assertLess(abs(starts["a"] - starts["b"]), .06)
        self.assertLess(elapsed, .24)
        self.assertGreaterEqual(starts["join"], max(starts["a"], starts["b"]))

    def test_model_parallel_limit_is_enforced(self):
        active = 0
        peak = 0

        async def execute(work, context, route, workdir, evidence):
            nonlocal active, peak
            active += 1; peak = max(peak, active)
            await asyncio.sleep(.03)
            active -= 1
            return NodeResult("PASS", "done")

        runner = self.engine(execute, parallel=4, model=profile(max_parallel=1))
        run_id = runner.start(plan(node("a"), node("b")), approval_mode="off")
        self.assertEqual(runner.run(run_id)["status"], "DONE")
        self.assertEqual(peak, 1)

    def test_write_waits_for_approval_then_promotes_and_verifies(self):
        calls = []

        async def execute(work, context, route, workdir, evidence):
            calls.append(work.id)
            if work.id == "write":
                target = workdir / "src" / "answer.txt"
                target.write_text("42\n", encoding="utf-8")
                return NodeResult("PASS", "answer written", ["src/answer.txt"])
            exists = (workdir / "src" / "answer.txt").read_text(encoding="utf-8") == "42\n"
            return NodeResult("PASS" if exists else "FAIL", "verified" if exists else "missing")

        graph = plan(
            node("write", risk="write", scopes=("src/*.txt",), capability="implementation"),
            node("qa", deps=("write",), capability="verification", commands=('python -c "pass"',)),
        )
        runner = self.engine(execute)
        run_id = runner.start(graph)
        waiting = runner.run(run_id)
        self.assertEqual(waiting["status"], "AWAITING_APPROVAL")
        self.assertEqual(calls, [])
        runner.approve(run_id, "write", "approved test write")
        finished = runner.run(run_id)
        self.assertEqual(finished["status"], "DONE")
        self.assertEqual(calls, ["write", "qa"])
        self.assertEqual((self.root / "src" / "answer.txt").read_text(), "42\n")

    def test_out_of_scope_artifact_is_not_promoted(self):
        async def execute(work, context, route, workdir, evidence):
            (workdir / "outside.txt").write_text("bad", encoding="utf-8")
            return NodeResult("PASS", "wrote outside", ["outside.txt"])

        graph = plan(
            node("write", risk="write", scopes=("src/*.txt",), capability="implementation"),
            node("qa", deps=("write",), capability="verification", commands=('python -c "pass"',)),
        )
        runner = self.engine(execute)
        run_id = runner.start(graph, approval_mode="off")
        finished = runner.run(run_id)
        self.assertEqual(finished["status"], "FAILED")
        self.assertFalse((self.root / "outside.txt").exists())

    def test_parallel_write_conflict_fails_instead_of_overwriting(self):
        async def execute(work, context, route, workdir, evidence):
            await asyncio.sleep(.03)
            (workdir / "src" / "shared.txt").write_text(work.id, encoding="utf-8")
            return NodeResult("PASS", "candidate ready", ["src/shared.txt"])

        graph = plan(
            node("left", risk="write", scopes=("src/*.txt",), capability="implementation"),
            node("right", risk="write", scopes=("src/*.txt",), capability="implementation"),
            node("qa", deps=("left", "right"), capability="verification",
                 commands=('python -c "pass"',)),
        )
        runner = self.engine(execute)
        run_id = runner.start(graph, approval_mode="off")
        result = runner.run(run_id)
        self.assertEqual(result["status"], "FAILED")
        self.assertIn((self.root / "src" / "shared.txt").read_text(), {"left", "right"})

    def test_deterministic_verification_overrides_model_pass(self):
        async def execute(work, context, route, workdir, evidence):
            if work.id == "write":
                (workdir / "src" / "value.txt").write_text("value", encoding="utf-8")
                return NodeResult("PASS", "written", ["src/value.txt"])
            return NodeResult("PASS", "model claims tests passed")

        graph = plan(
            node("write", risk="write", scopes=("src/*.txt",), capability="implementation"),
            node("qa", deps=("write",), capability="verification",
                 commands=('python -c "import sys; sys.exit(3)"',)),
        )
        runner = self.engine(execute)
        run_id = runner.start(graph, approval_mode="off")
        result = runner.run(run_id)
        self.assertEqual(result["status"], "FAILED")
        qa = next(row for row in result["nodes"] if row["node_id"] == "qa")
        self.assertIn("verification command failed", qa["error"])

    def test_retry_succeeds_for_isolated_node(self):
        attempts = {"a": 0}

        async def execute(work, context, route, workdir, evidence):
            attempts["a"] += 1
            if attempts["a"] == 1:
                return NodeResult("FAIL", "transient")
            return NodeResult("PASS", "recovered")

        runner = self.engine(execute)
        run_id = runner.start(plan(node("a", attempts=2)), approval_mode="off")
        result = runner.run(run_id)
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(attempts["a"], 2)

    def test_budget_stops_before_executor(self):
        calls = []

        async def execute(*args):
            calls.append(1)
            return NodeResult("PASS", "done")

        costly = profile(input_cost_per_million=1000, output_cost_per_million=1000)
        runner = self.engine(execute, model=costly)
        run_id = runner.start(plan(node("a", tokens=1000)), BudgetLimits(max_cost_usd=.01), "off")
        result = runner.run(run_id)
        self.assertEqual(result["status"], "BUDGET_EXHAUSTED")
        self.assertEqual(calls, [])

    def test_interrupted_isolated_node_is_resumed(self):
        async def execute(work, context, route, workdir, evidence):
            return NodeResult("PASS", "resumed")

        runner = self.engine(execute)
        run_id = runner.start(plan(node("a")), approval_mode="off")
        runner.store.set_node(run_id, "a", "RUNNING", attempts=1, started=True)
        result = runner.run(run_id)
        self.assertEqual(result["status"], "DONE")

    def test_external_failure_is_not_retried(self):
        calls = []

        async def execute(work, context, route, workdir, evidence):
            calls.append(1)
            return NodeResult("FAIL", "remote timeout")

        graph = plan(node("send", risk="external", attempts=3, idempotency="send-123"))
        runner = self.engine(execute)
        run_id = runner.start(graph, approval_mode="off")
        waiting = runner.run(run_id)
        self.assertEqual(waiting["status"], "AWAITING_APPROVAL")
        runner.approve(run_id, "send")
        finished = runner.run(run_id)
        self.assertEqual(finished["status"], "FAILED")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
