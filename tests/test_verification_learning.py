import sqlite3
import tempfile
import unittest
from pathlib import Path

from galaxy_core.brain.experience import ExperienceStore
from galaxy_core.engine.autonomy import AutonomousEngine, ExecutionPlan, ModelProfile, NodeResult, WorkNode
from galaxy_core.engine.verification import VerificationStore


def plan(command: str, project: str = "demo") -> ExecutionPlan:
    writer = WorkNode("write", "Write", "Fix bug", role="Earth", risk="write",
                      write_scopes=("src/fix.txt",))
    qa = WorkNode("qa", "Verify", "Check bug", role="Earth", capability="qa",
                  dependencies=("write",), verification_commands=(command,))
    return ExecutionPlan("Fix bug", project, (writer, qa), task_type="bugfix")


def profile():
    return ModelProfile(name="fake", provider="fake", capabilities=("implementation", "qa"),
                        quality=.8, latency_score=.8)


class VerificationLearningTests(unittest.TestCase):
    def test_failure_pattern_replay_promotion_gate_and_verification_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir()
            experience = ExperienceStore(root)
            rules = VerificationStore(root)
            for n in range(3):
                experience.record(project="demo", task="fix bug", role="Earth", agent_id="earth",
                    run_id=f"r{n}", node_id="fix", outcome="failure", summary="regression escaped",
                    lesson="Run regression verification", evidence=[f"failure-log-{n}"])
            rid = rules.suggest_from_failures(experience, project="demo", lesson="Run regression verification",
                task_type="bugfix", check_kind="require_command", match_text="python -c")
            self.assertEqual(len(rules.get(rid)["episode_ids"]), 3)
            bad = plan("true")
            good = plan('python -c "print(1)"')
            with self.assertRaises(ValueError):
                rules.promote(rid, by="reviewer")
            report = rules.evaluate(rid, [(bad, True, []), (good, False, [])])
            self.assertEqual(report["false_blocks"], 0)
            self.assertEqual(report["missed_blocks"], 0)
            rules.promote(rid, by="reviewer")
            self.assertEqual(rules.get(rid)["status"], "active")

            def executor(node, context, route, workspace, evidence):
                if rules.active("demo", "bugfix"):
                    self.assertIn(rid, [x["id"] for x in context["verification_contract"]["active_rules"]])
                if node.id == "write":
                    (workspace / "src" / "fix.txt").write_text("fixed", encoding="utf-8")
                    return NodeResult("PASS", "fixed", artifacts=["src/fix.txt"])
                return NodeResult("PASS", "verified")

            engine = AutonomousEngine(root, [profile()], executor)
            with self.assertRaisesRegex(ValueError, rid):
                engine.start(bad, approval_mode="off")
            run_id = engine.start(good, approval_mode="off")
            result = engine.run(run_id)
            self.assertEqual(result["status"], "DONE")
            qa = next(x for x in result["nodes"] if x["node_id"] == "qa")
            self.assertTrue(any("verification-summary.log" in x for x in qa["result"]["evidence"]))
            with sqlite3.connect(rules.path) as db:
                check = db.execute("SELECT status,exit_code,log_path,log_sha256 FROM checks WHERE run_id=?",
                                   (run_id,)).fetchone()
            self.assertEqual(check[:2], ("passed", 0))
            self.assertTrue((root / check[2]).is_file())
            self.assertEqual(len(check[3]), 64)
            (root / check[2]).write_text("tampered", encoding="utf-8")
            audited = engine.run(run_id)
            self.assertEqual(audited["status"], "FAILED")
            self.assertIn("damaged", next(x for x in audited["nodes"] if x["node_id"] == "qa")["error"])
            rules.disable(rid)
            self.assertEqual(rules.active("demo", "bugfix"), [])
            engine.start(bad, approval_mode="off")
            failed = engine.run(engine.start(plan('python -c "import sys; sys.exit(1)"'), approval_mode="off"))
            self.assertEqual(failed["status"], "FAILED")
            failed_qa = next(x for x in failed["nodes"] if x["node_id"] == "qa")
            self.assertTrue(any("verify-1.log" in x for x in failed_qa["result"]["evidence"]))
            with sqlite3.connect(rules.path) as db:
                self.assertEqual(db.execute("SELECT status FROM checks WHERE run_id=?",
                                            (failed["id"],)).fetchone()[0], "failed")

    def test_bad_replay_cannot_promote_and_required_evidence_blocks_done(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rules = VerificationStore(root)
            rid = rules.propose(project="demo", task_type="general", check_kind="require_command",
                                match_text="pytest")
            rules.evaluate(rid, [(plan("true"), False, []), (plan("pytest"), True, [])])
            with self.assertRaisesRegex(ValueError, "classification errors"):
                rules.promote(rid, by="reviewer")

            evidence_rule = rules.propose(project="demo", task_type="general", check_kind="require_evidence")
            read_plan = ExecutionPlan("Inspect", "demo", (WorkNode("qa", "QA", "Review", role="Earth",
                capability="qa"),))
            rules.evaluate(evidence_rule, [(read_plan, True, []), (read_plan, False, ["log"] )])
            rules.promote(evidence_rule, by="reviewer")
            engine = AutonomousEngine(root, [profile()],
                                      lambda *args: NodeResult("PASS", "claimed done", evidence=["fake-proof.log"]))
            result = engine.run(engine.start(read_plan, approval_mode="off"))
            self.assertEqual(result["status"], "FAILED")
            self.assertIn("requires recorded evidence", result["nodes"][0]["error"])

    def test_rule_activated_after_start_is_checked_again_before_execution(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rules = VerificationStore(root)
            engine = AutonomousEngine(root, [profile()], lambda *args: NodeResult("PASS", "claimed done"))
            bad = plan("true")
            run_id = engine.start(bad, approval_mode="off")
            rid = rules.propose(project="demo", task_type="bugfix", check_kind="require_command",
                                match_text="pytest")
            rules.evaluate(rid, [(bad, True, []), (plan("pytest"), False, [])])
            rules.promote(rid, by="reviewer")
            with self.assertRaisesRegex(ValueError, rid):
                engine.run(run_id)


if __name__ == "__main__":
    unittest.main()
