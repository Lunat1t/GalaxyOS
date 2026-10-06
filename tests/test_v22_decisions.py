"""Tests for Galaxy 2.2 System One Decision & Guardrail Layer (Jev)."""
import tempfile
import unittest
from pathlib import Path

from galaxy_core.engine.decisions import (
    Choice,
    DAGDecisionRouter,
    DecisionEngine,
    DecisionRequest,
    DecisionResponse,
    GuardrailVerdict,
    LocalLLMDecisionAdapter,
    MarsDecisionGuardrail,
    Noul,
    Score,
)


class SystemOnePrimitivesTests(unittest.TestCase):
    def test_noul_validation_and_serialization(self):
        noul = Noul(instructions="Is this safe?", value=True, probability=0.92)
        noul.validate()
        d = noul.to_dict()
        self.assertEqual(d["type"], "noul")
        self.assertEqual(d["probability"], 0.92)
        self.assertTrue(d["value"])

        restored = Noul.from_dict(d)
        self.assertEqual(restored.probability, 0.92)
        self.assertTrue(restored.value)

        # Invalid probability
        with self.assertRaises(ValueError):
            Noul(instructions="Test", probability=1.5).validate()

    def test_choice_distribution_and_selection(self):
        choice = Choice(
            instructions="Select target department",
            options=["billing", "support", "sales"],
            distribution={"billing": 0.7, "support": 0.2, "sales": 0.1},
            selected="billing",
            confidence=0.7,
        )
        choice.validate()
        self.assertEqual(choice.selected, "billing")

        d = choice.to_dict()
        self.assertEqual(d["type"], "choice")
        restored = Choice.from_dict(d)
        self.assertEqual(restored.selected, "billing")

        # Invalid option not in options list
        with self.assertRaises(ValueError):
            Choice(
                instructions="Test",
                options=["a", "b"],
                distribution={"a": 0.5, "c": 0.5},
            ).validate()

    def test_score_levels_and_expected_value(self):
        score = Score(
            instructions="Rate urgency from 1 to 5",
            levels=5,
            distribution={1: 0.1, 2: 0.1, 3: 0.2, 4: 0.4, 5: 0.2},
        )
        score.validate()
        exp = score.compute_expected_value()
        # 1*0.1 + 2*0.1 + 3*0.2 + 4*0.4 + 5*0.2 = 0.1 + 0.2 + 0.6 + 1.6 + 1.0 = 3.5
        self.assertAlmostEqual(exp, 3.5, places=2)
        self.assertEqual(score.expected_score, 3.5)

        # Invalid levels (< 2 or > 10)
        with self.assertRaises(ValueError):
            Score(instructions="Test", levels=1).validate()
        with self.assertRaises(ValueError):
            Score(instructions="Test", levels=12).validate()


class DecisionEngineAndSpeculativeFanOutTests(unittest.TestCase):
    def setUp(self):
        self.engine = DecisionEngine(prefer_cloud=False)

    def test_speculative_fan_out_multiple_questions(self):
        req = DecisionRequest(
            state="Command: git status\nUser wants to check repository status.",
            questions={
                "is_safe": Noul(instructions="Is this operation safe to execute?"),
                "risk": Score(instructions="Rate security risk", levels=5),
                "action": Choice(
                    instructions="Select action",
                    options=["allow", "awaiting_approval", "block"],
                ),
            },
        )
        res = self.engine.decide(req)
        self.assertIn("is_safe", res.answers)
        self.assertIn("risk", res.answers)
        self.assertIn("action", res.answers)
        self.assertTrue(res.answers["is_safe"])
        self.assertEqual(res.answers["action"], "allow")
        self.assertLessEqual(res.answers["risk"], 2.0)
        self.assertGreater(res.latency_ms, 0.0)

    def test_caching_and_lru(self):
        req = DecisionRequest(
            state="Same query state",
            questions={"q": Noul(instructions="Is read-only?")},
        )
        res1 = self.engine.decide(req)
        self.assertEqual(self.engine.metrics["cache_hits"], 0)

        res2 = self.engine.decide(req)
        self.assertEqual(self.engine.metrics["cache_hits"], 1)
        self.assertEqual(res1.answers, res2.answers)


class MarsSecOpsGuardrailTests(unittest.TestCase):
    def setUp(self):
        self.guardrail = MarsDecisionGuardrail()

    def test_safe_read_command_is_allowed(self):
        verdict = self.guardrail.inspect_command("git diff HEAD~1")
        self.assertEqual(verdict.action, "ALLOW")
        self.assertFalse(verdict.is_destructive)
        self.assertFalse(verdict.requires_human_approval)
        self.assertLess(verdict.risk_score, 2.5)
        self.assertGreaterEqual(verdict.confidence, 0.85)

    def test_destructive_command_escalates_to_human_approval(self):
        verdict = self.guardrail.inspect_command("rm -rf /home/bah/galaxy/data")
        self.assertEqual(verdict.action, "AWAITING_APPROVAL")
        self.assertTrue(verdict.is_destructive)
        self.assertTrue(verdict.requires_human_approval)
        self.assertIn("destructive", verdict.reason.lower())
        self.assertGreaterEqual(verdict.risk_score, 3.5)

    def test_sql_drop_table_escalates_to_human_approval(self):
        verdict = self.guardrail.inspect_command("sqlite3 galaxy.db 'DROP TABLE memory_blocks;'")
        self.assertEqual(verdict.action, "AWAITING_APPROVAL")
        self.assertTrue(verdict.is_destructive)
        self.assertTrue(verdict.requires_human_approval)

    def test_fork_bomb_blocked_or_escalated(self):
        verdict = self.guardrail.inspect_command(":(){ :|:& };:")
        self.assertIn(verdict.action, {"BLOCK", "AWAITING_APPROVAL"})
        self.assertTrue(verdict.is_destructive or verdict.risk_score >= 3.5)

    def test_sub_second_latency(self):
        verdict = self.guardrail.inspect_command("ls -la")
        # Decision should happen in < 50ms on local system
        self.assertLess(verdict.decision_ms, 500.0)


class DAGDecisionRouterTests(unittest.TestCase):
    def setUp(self):
        self.router = DAGDecisionRouter()

    def test_single_candidate_instant_route(self):
        route = self.router.route_next_branch(
            current_state="Implementation complete",
            candidate_nodes=["verify_moon"],
        )
        self.assertEqual(route.selected_node, "verify_moon")
        self.assertEqual(route.confidence, 1.0)

    def test_multi_branch_route_selection(self):
        route = self.router.route_next_branch(
            current_state="Code written, need to execute unit tests and deterministic verification",
            candidate_nodes=["code_write", "verify_moon", "deploy_staging"],
            objective="Deliver verified feature",
        )
        self.assertEqual(route.selected_node, "verify_moon")
        self.assertIn("verify_moon", route.distribution)
        self.assertGreaterEqual(route.confidence, 0.4)


if __name__ == "__main__":
    unittest.main()
