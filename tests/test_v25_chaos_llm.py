"""Chaos LLM and Adversarial Resilience Tests for Galaxy 3.0 Moons OS.

Validates that Galaxy Agent Runtime, Automatic Team Builder, DAG Engine,
and Mars SecOps behave deterministically, safely, and without state corruption
when receiving malformed, hallucinated, incomplete, or hostile LLM outputs.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest

from galaxy_core.agents import (
    AgentRegistry,
    AutomaticTeamBuilder,
    MoonBudget,
    MoonResult,
    TeamOutcome,
    TeamPlan,
    TeamPolicy,
    TeamTask,
)
from galaxy_core.discovery.grill_dag import GrillDAG
from galaxy_core.discovery.grill_models import GrillNode, GrillSessionMeta
from galaxy_core.discovery.grill_provider import GrillProviderAdapter
from galaxy_core.engine.autonomy import (
    AutonomousEngine,
    AutonomyStore,
    BudgetLimits,
    ExecutionPlan,
    ModelProfile,
    NodeResult,
    WorkNode,
)
from galaxy_core.engine.decisions import MarsDecisionGuardrail
from galaxy_core.engine.planning import (
    CLIMoonExecutor,
    parse_one_object,
)
from galaxy_core.providers.base import (
    GenerationRequest,
    GenerationResponse,
    ModelProvider,
)
from galaxy_core.providers.registry import ProviderRegistry


class ChaosLLMProvider(ModelProvider):
    """Configurable model provider that emits corrupted, truncated, or hostile payloads."""

    def __init__(self, mode: str = "corrupted_json", **kwargs):
        super().__init__(name="chaos", **kwargs)
        self.mode = mode

    def health_check(self) -> bool:
        return True

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        if self.mode == "corrupted_json":
            return GenerationResponse(content="{ 'invalid': json, truncated: ")
        elif self.mode == "markdown_mixed":
            return GenerationResponse(
                content="Here is your requested plan:\n```json\n{\n  \"status\": \"PASS\",\n  \"summary\": \"ok\"\n}\n```\nHope that helps!"
            )
        elif self.mode == "truncated":
            return GenerationResponse(content='{"status": "PASS", "summary": "incom')
        elif self.mode == "empty":
            return GenerationResponse(content="")
        elif self.mode == "hallucinated_schema":
            return GenerationResponse(content=json.dumps({"random_key": 42, "hallucinated": True}))
        raise ValueError(f"Unknown chaos mode: {self.mode}")


class ChaosLLMResilienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "README.md").write_text("# Project\n", encoding="utf-8")
        self.registry = AgentRegistry(self.root)
        self.registry.bootstrap_defaults()

    def tearDown(self):
        self.tmp.cleanup()

    def test_json_extractor_handles_mixed_markdown_and_broken_blocks(self):
        """Test parse_one_object resilience against raw text, fences, and noisy LLM responses."""
        fenced = "Thought: let's return json\n```json\n{\"goal\": \"build\", \"valid\": true}\n```"
        obj = parse_one_object(fenced)
        self.assertEqual(obj.get("goal"), "build")

        with self.assertRaises((json.JSONDecodeError, ValueError)):
            parse_one_object("Sorry, I cannot fulfill this request as an AI.")

    def test_team_builder_handles_llm_hallucinating_tool_escalation(self):
        """When an LLM attempts to give an automatic Moon dangerous mutating tools, validate rejects."""
        parent = self.registry.get_agent("earth")
        escalated_plan = TeamPlan(
            objective="Audit code and update config",
            tasks=(
                TeamTask("t1", "Audit", "Inspect code", "code.read", ("code.read",)),
                TeamTask("t2", "Mutate", "Push files to git", "code.write", ("code.read", "code.write")),  # mutating!
            ),
            rationale="Parallelize auditing",
            expected_gain=0.6,
        )

        with self.assertRaises(PermissionError) as ctx:
            escalated_plan.validate(parent, TeamPolicy())
        self.assertIn("automatic Moons are read-only", str(ctx.exception))

    def test_team_builder_handles_llm_generating_cyclic_dependencies(self):
        """When an LLM produces a cyclic dependency between subtasks, validation catches it."""
        parent = self.registry.get_agent("earth")
        cyclic_plan = TeamPlan(
            objective="Analyze modules",
            tasks=(
                TeamTask("a", "Task A", "Do A", "general", ("code.read",), dependencies=("b",)),
                TeamTask("b", "Task B", "Do B", "general", ("code.read",), dependencies=("a",)),
            ),
            rationale="Deadlock test",
            expected_gain=0.5,
        )

        with self.assertRaises(ValueError) as ctx:
            cyclic_plan.validate(parent, TeamPolicy())
        self.assertIn("team task cycle", str(ctx.exception))

    def test_team_builder_handles_llm_exceeding_parent_budget(self):
        """When an LLM proposes subtasks whose total budget exceeds parent, validate rejects."""
        parent = self.registry.get_agent("earth")
        excessive_plan = TeamPlan(
            objective="Analyze modules",
            tasks=(
                TeamTask(
                    "a", "Task A", "Do A", "general", ("code.read",),
                    budget=MoonBudget(max_cost_usd=99999.0)
                ),
                TeamTask("b", "Task B", "Do B", "general", ("code.read",)),
            ),
            rationale="Excessive cost test",
            expected_gain=0.5,
        )

        with self.assertRaises(ValueError) as ctx:
            excessive_plan.validate(parent, TeamPolicy())
        self.assertIn("exceeds parent budget", str(ctx.exception))

    def test_cli_moon_executor_handles_corrupted_llm_output_gracefully(self):
        """CLIMoonExecutor handles corrupted JSON by returning structured MoonResult failure without crash."""
        ProviderRegistry.register("chaos_corrupt", lambda **kw: ChaosLLMProvider("corrupted_json", **kw))
        executor = CLIMoonExecutor(provider="chaos_corrupt")

        task = TeamTask("t1", "Audit", "Inspect code", "code.read", ("code.read",))
        evidence = self.root / "evidence"
        evidence.mkdir(parents=True, exist_ok=True)

        result = asyncio.run(executor(task=task, context={}, workdir=self.root, evidence=evidence))
        self.assertEqual(result.status, "failure")
        self.assertIn("failed", result.summary)
        self.assertEqual(result.confidence, 0.0)

    def test_grill_provider_recovers_to_deterministic_fallback_on_chaos(self):
        """GrillProviderAdapter falls back to deterministic questions when model emits garbage or cuts off."""
        ProviderRegistry.register("chaos_empty", lambda **kw: ChaosLLMProvider("empty", **kw))
        adapter = GrillProviderAdapter(root=self.root, provider="chaos_empty")

        from galaxy_core.discovery.grill_profiles import build_dag_for_profile, get_profile

        meta = GrillSessionMeta(
            session_id="S_CHAOS_01",
            initial_idea="Build secure microservice",
            profile="engineering",
        )
        dag = build_dag_for_profile(get_profile("engineering"))
        frontier = dag.get_independent_frontier_batch()

        questions, facts, conflicts, pause_reason = adapter.generate_proposals(meta, dag, frontier)
        # Should gracefully return deterministic fallback questions without crashing or pausing
        self.assertIsNone(pause_reason)
        self.assertGreater(len(questions), 0)

    def test_mars_secops_blocks_hostile_llm_injections_and_fork_bombs(self):
        """Mars SecOps blocks or escalates destructive/sneaky bash commands."""
        guard = MarsDecisionGuardrail()

        destructive_payloads = [
            ":(){ :|:& };:",
            "rm -rf /home/bah/galaxy/data",
            "sqlite3 galaxy.db 'DROP TABLE memory_blocks;'",
        ]

        for payload in destructive_payloads:
            verdict = guard.inspect_command(payload)
            self.assertIn(
                verdict.action,
                {"BLOCK", "AWAITING_APPROVAL"},
                f"Failed to catch dangerous payload: {payload}"
            )
            self.assertTrue(verdict.is_destructive or verdict.risk_score >= 3.5)

    def test_dag_engine_rejects_hallucinated_write_outside_scope(self):
        """AutonomousEngine workspace promotion raises error if a node writes outside declared scopes."""
        plan = ExecutionPlan(
            goal="Update documentation",
            project="test",
            success_criteria=("Docs updated",),
            constraints=(),
            nodes=(
                WorkNode(
                    id="doc_node",
                    title="Write docs",
                    objective="Write to README.md",
                    role="Earth",
                    risk="write",
                    write_scopes=("README.md",),
                    verification_commands=(),
                ),
                WorkNode(
                    id="verify_node",
                    title="Verify docs",
                    objective="Check docs exist",
                    role="Moon",
                    capability="verification",
                    risk="read",
                    dependencies=("doc_node",),
                    verification_commands=("test -f README.md",),
                ),
            ),
        )
        profile = ModelProfile(
            name="test_profile",
            provider="fake",
            model="mock",
            capabilities=("implementation", "verification"),
        )

        def hallucinating_executor(node, context, route, workdir, evidence):
            if node.id == "doc_node":
                unauthorized = workdir / "secret_override.sh"
                unauthorized.write_text("# malicious\n")
                return NodeResult(
                    status="PASS",
                    summary="Wrote unauthorized file",
                    artifacts=["secret_override.sh"],
                )
            return NodeResult(status="PASS", summary="Verified")

        engine = AutonomousEngine(self.root, [profile], hallucinating_executor)
        run_id = engine.start(plan, approval_mode="off")
        run_result = engine.run(run_id)

        self.assertEqual(run_result["status"], "FAILED")
        self.assertFalse((self.root / "secret_override.sh").exists())


if __name__ == "__main__":
    unittest.main()
