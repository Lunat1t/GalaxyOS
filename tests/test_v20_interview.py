from pathlib import Path
import tempfile
import unittest

from galaxy_core.discovery.interview import SunInterview
from galaxy import parser


DETAILED = "Нужен конкретный проверяемый результат для основного пользователя"


class SunInterviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sun = SunInterview(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_start_is_durable_and_has_one_pending_question(self):
        session = self.sun.start("Сделать полезное приложение", "demo", "quick")
        self.assertEqual(session["status"], "DISCOVERY")
        self.assertEqual(session["pending"]["category"], "outcome")
        restored = SunInterview(self.root).require(session["id"])
        self.assertEqual(restored["pending"], session["pending"])

    def test_vague_answer_triggers_one_guided_followup(self):
        session = self.sun.start("Сделать приложение", depth="quick")
        updated = self.sun.answer(session["id"], "удобно")
        self.assertEqual(updated["pending"]["followup_of"], "Q01")
        self.assertIn("Варианты", updated["pending"]["text"])
        completed = self.sun.answer(session["id"], "не знаю")
        self.assertEqual(completed["answers"]["outcome"]["status"], "complete")
        self.assertTrue(completed["answers"]["outcome"]["unknown"])
        self.assertTrue(any(x.startswith("outcome:") for x in completed["assumptions"]))

    def test_quick_interview_builds_brief_and_requires_confirmation(self):
        session = self.sun.start("Создать систему задач", "tasks", "quick")
        while session["status"] == "DISCOVERY":
            session = self.sun.answer(session["id"], DETAILED)
        self.assertEqual(session["status"], "DRAFT_READY")
        self.assertEqual(len(session["brief"]["requirements"]), 8)
        with self.assertRaisesRegex(ValueError, "confirm"):
            self.sun.planning_prompt(session["id"])
        confirmed = self.sun.confirm(session["id"])
        self.assertEqual(confirmed["status"], "CONFIRMED")
        prompt = self.sun.planning_prompt(session["id"])
        self.assertIn("Создать систему задач", prompt)
        self.assertIn("Do not silently invent", prompt)

    def test_question_limit_records_unasked_sections_as_assumptions(self):
        session = self.sun.start("Большой проект", depth="exhaustive", max_questions=4)
        while session["status"] == "DISCOVERY":
            session = self.sun.answer(session["id"], DETAILED)
        self.assertEqual(session["status"], "DRAFT_READY")
        self.assertTrue(any("не был задан" in item for item in session["brief"]["assumptions"]))

    def test_pause_and_resume_keep_the_same_question(self):
        session = self.sun.start("Проект", depth="quick")
        pending = session["pending"]
        paused = self.sun.pause(session["id"])
        self.assertEqual(paused["status"], "PAUSED")
        resumed_question = self.sun.next_question(session["id"])
        self.assertEqual(resumed_question, pending)
        self.assertEqual(self.sun.require(session["id"])["status"], "DISCOVERY")

    def test_user_can_revise_one_category_after_draft(self):
        session = self.sun.start("Проект", depth="quick")
        while session["status"] == "DISCOVERY":
            session = self.sun.answer(session["id"], DETAILED)
        revised = self.sun.revise(session["id"], "users")
        self.assertEqual(revised["status"], "DISCOVERY")
        self.assertEqual(revised["pending"]["category"], "users")
        self.assertNotIn("users", revised["answers"])

    def test_markdown_exposes_assumptions_before_confirmation(self):
        session = self.sun.start("Проект", depth="quick", max_questions=4)
        while session["status"] == "DISCOVERY":
            session = self.sun.answer(session["id"], DETAILED)
        text = self.sun.markdown(session["id"])
        self.assertIn("Goal Brief", text)
        self.assertIn("Предположения и открытые вопросы", text)
        self.assertIn("Sun обязан", text)

    def test_plan_uses_interview_by_default_and_direct_is_explicit(self):
        normal = parser().parse_args(["plan", "idea"])
        direct = parser().parse_args(["plan", "complete specification", "--direct"])
        self.assertFalse(normal.direct)
        self.assertEqual(normal.depth, "deep")
        self.assertTrue(direct.direct)

    def test_noninteractive_interview_commands_are_exposed(self):
        start = parser().parse_args(["interview-start", "idea", "--depth", "exhaustive"])
        answer = parser().parse_args(["interview-answer", "INT-123", "подробный ответ пользователя"])
        self.assertEqual(start.depth, "exhaustive")
        self.assertEqual(answer.interview_id, "INT-123")


if __name__ == "__main__":
    unittest.main()
