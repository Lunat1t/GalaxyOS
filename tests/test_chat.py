"""Unit tests for Galaxy 1.7.1 interactive chat module."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import chat


class ChatModuleTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent.parent

    def test_load_role_prompt(self):
        prompt_sun = chat.load_role_prompt(self.root, "Sun")
        self.assertIn("Sun", prompt_sun)
        self.assertIn("orchestrate", prompt_sun)
        self.assertIn("vault.read", prompt_sun)

        prompt_earth = chat.load_role_prompt(self.root, "Earth")
        self.assertIn("Earth", prompt_earth)
        self.assertIn("filesystem.write", prompt_earth)

        prompt_none = chat.load_role_prompt(self.root, None)
        self.assertEqual(prompt_none, "")

    def test_load_task_context(self):
        ctx = chat.load_task_context(self.root, "TASK-002")
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx["id"], "TASK-002")
        self.assertIn("calculator", ctx["text"])

        # Invalid format
        self.assertIsNone(chat.load_task_context(self.root, "invalid_id"))
        # Non-existent task
        self.assertIsNone(chat.load_task_context(self.root, "TASK-9999"))

    def test_provider_resolves_from_local_bin_without_path(self):
        with patch("system.agent_core.agents.shutil.which", return_value=None), \
             patch("system.agent_core.agents.Path.home", return_value=Path("/home/tester")), \
             patch("system.agent_core.agents.Path.is_file", return_value=True), \
             patch("system.agent_core.agents.os.access", return_value=True):
            cmd = chat.resolve_provider_command("codex")
        self.assertEqual(cmd, ["/home/tester/.local/bin/codex", "exec", "--skip-git-repo-check"])

    def test_build_prompt(self):
        history = [("User", "Привет"), ("Assistant", "Приветствую!")]
        task_ctx = {"id": "TASK-001", "path": "tasks/task-001.md", "text": "Task 1 text"}
        prompt = chat.build_prompt(
            root=self.root,
            role="Mars",
            task_ctx=task_ctx,
            history=history,
            user_msg="Каковы риски архитектуры?",
            include_memory=False
        )
        self.assertIn("Mars", prompt)
        self.assertIn("<attached_task id=\"TASK-001\"", prompt)
        self.assertIn("User: Привет", prompt)
        self.assertIn("Assistant: Приветствую!", prompt)
        self.assertIn("User: Каковы риски архитектуры?", prompt)
        self.assertTrue(prompt.endswith("Assistant:"))

    @patch("shutil.which", return_value="/usr/bin/mock_cli")
    @patch("subprocess.run")
    def test_call_provider_success(self, mock_run, mock_which):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "Ответ модели"
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        reply = chat.call_provider(["mock_cli"], "test prompt", cwd=self.root)
        self.assertEqual(reply, "Ответ модели")

    @patch("shutil.which", return_value="/usr/bin/mock_cli")
    @patch("subprocess.run")
    def test_call_provider_failure(self, mock_run, mock_which):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "Fatal error from CLI"
        mock_run.return_value = mock_proc

        reply = chat.call_provider(["mock_cli"], "test prompt", cwd=self.root)
        self.assertIn("провайдер завершился с кодом 1", reply)
        self.assertIn("Fatal error from CLI", reply)

    def test_save_log(self):
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "test_chat.md"
            history = [("User", "Привет"), ("Sun", "Здравствуйте")]
            chat.save_log(log_path, history, "codex", "Sun", "TASK-002")
            self.assertTrue(log_path.exists())
            content = log_path.read_text(encoding="utf-8")
            self.assertIn("Galaxy Chat Log", content)
            self.assertIn("Sun", content)
            self.assertIn("TASK-002", content)
            self.assertIn("User:", content)


if __name__ == "__main__":
    unittest.main()
