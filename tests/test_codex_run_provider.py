"""Offline checks for the normalized Codex run contract."""
import json
from pathlib import Path
import sys
import subprocess
import sqlite3
import time
import tempfile
import unittest

from galaxy_core.runs.codex_cli import CodexRunProvider, RunRequest
from galaxy_core.runs.codex_cli import ProviderEvent
from galaxy_core.runs.manager import RunManager
from galaxy_core.projects import ProjectRegistry


class CodexRunProviderTests(unittest.TestCase):
    def test_existing_task_data_survives_run_schema_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            db = sqlite3.connect(data / "projects.sqlite3")
            db.executescript("""
                CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL, last_opened_at TEXT, settings_json TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE registry_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE project_tasks (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    request TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL);
            """)
            db.execute("INSERT INTO projects VALUES('p','project',?, 'now', NULL, '{}')", (directory,))
            db.execute("INSERT INTO project_tasks VALUES('t','p','old request','queued','then','then')")
            db.commit()
            db.close()

            registry = ProjectRegistry(data)
            task = registry.get_task("t")
            self.assertEqual(task.request, "old request")
            self.assertIsNone(task.latest_run_id)

    def test_streams_messages_result_usage_and_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
            executable = root / "fake-codex"
            events = [
                {"type": "item.completed", "item": {"type": "agent_message", "text": "Done"}},
                {"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 4}, "last_message": "Done"},
            ]
            payload = "\n".join(json.dumps(event) for event in events)
            executable.write_text(
                f"#!{sys.executable}\nimport sys\n"
                "assert '--worktree' in sys.argv and '--sandbox' in sys.argv\n"
                f"print({payload!r})\n", encoding="utf-8"
            )
            executable.chmod(0o700)
            run = CodexRunProvider(str(executable)).start(RunRequest("task", root))
            actual = list(run.events())

        self.assertEqual([event.kind for event in actual], ["message", "result", "completed"])
        self.assertEqual(actual[0].message, "Done")
        self.assertEqual(actual[1].input_tokens, 12)
        self.assertEqual(actual[1].output_tokens, 4)
        self.assertEqual(actual[1].result, {"text": "Done"})

    def test_preserves_codex_failure_reason_for_the_run_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
            executable = root / "fake-codex"
            failure = {"type": "turn.failed", "error": {"message": "Authentication expired"}}
            executable.write_text(
                f"#!{sys.executable}\nimport json,sys\nprint(json.dumps({failure!r}))\nsys.exit(1)\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            run = CodexRunProvider(str(executable)).start(RunRequest("task", root))
            errors = [event for event in run.events() if event.kind == "error"]

        self.assertEqual(errors[0].message, "Authentication expired")
        self.assertEqual(errors[0].error_code, "provider_failed")

    def test_rejects_empty_prompt_and_missing_workspace(self):
        provider = CodexRunProvider(sys.executable)
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["git", "init", directory], check=True, capture_output=True)
            with self.assertRaises(ValueError):
                provider.start(RunRequest("  ", Path(directory)))
            with self.assertRaises(ValueError):
                provider.start(RunRequest("task", Path(directory) / "missing"))

    def test_cancel_stops_the_cli_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
            executable = root / "fake-codex"
            executable.write_text(
                f"#!{sys.executable}\nimport time\ntime.sleep(30)\n", encoding="utf-8"
            )
            executable.chmod(0o700)
            run = CodexRunProvider(str(executable)).start(RunRequest("task", root))
            run.cancel()
            self.assertEqual([event.kind for event in run.events()], ["cancelled"])

    def test_run_manager_persists_status_and_ordered_events(self):
        class FakeRun:
            def events(self):
                yield ProviderEvent("message", message="Изменение готово api_key=sk-123456789012345678901234")
                yield ProviderEvent("result", result={"text": "Готово api_key=sk-123456789012345678901234"}, input_tokens=8, output_tokens=3)
                yield ProviderEvent("completed")

            def cancel(self):
                return None

        class FakeProvider:
            def available(self):
                return True

            def start(self, request):
                return FakeRun()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_root = root / "project"
            project_root.mkdir()
            subprocess.run(["git", "init", str(project_root)], check=True, capture_output=True)
            registry = ProjectRegistry(root / "data")
            project = registry.add(project_root)
            task = registry.create_task(project.id, "Измени файл")
            manager = RunManager(registry, FakeProvider())
            run = manager.start(project.id, task.id)
            deadline = time.monotonic() + 3
            while manager.get(run["id"])["status"] == "running" and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(manager.get(run["id"])["status"], "completed")
            events = manager.events(run["id"])
            self.assertEqual([item["sequence"] for item in events], list(range(1, len(events) + 1)))
            self.assertIn("provider.message", [item["event_type"] for item in events])
            message = next(item for item in events if item["event_type"] == "provider.message")
            self.assertNotIn("sk-123456789012345678901234", message["payload"]["text"])
            result = next(item for item in events if item["event_type"] == "provider.result")
            self.assertNotIn("sk-123456789012345678901234", result["payload"]["result"]["text"])
            self.assertEqual(registry.get_task(task.id).latest_run_id, run["id"])
            reopened = RunManager(ProjectRegistry(root / "data"), FakeProvider())
            self.assertEqual(reopened.get(run["id"])["status"], "completed")

    def test_run_manager_keeps_safe_provider_failure_message(self):
        class FailedRun:
            def events(self):
                yield ProviderEvent("error", message="Authentication expired api_key=sk-123456789012345678901234")

            def cancel(self):
                return None

        class FailedProvider:
            def available(self):
                return True

            def start(self, request):
                return FailedRun()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path = root / "project"
            project_path.mkdir()
            subprocess.run(["git", "init", str(project_path)], check=True, capture_output=True)
            registry = ProjectRegistry(root / "data")
            project = registry.add(project_path)
            task = registry.create_task(project.id, "Проверочная задача")
            manager = RunManager(registry, FailedProvider())
            run = manager.start(project.id, task.id)
            deadline = time.monotonic() + 3
            while manager.get(run["id"])["status"] == "running" and time.monotonic() < deadline:
                time.sleep(0.01)

            failure = manager.events(run["id"])[-1]
            self.assertEqual(failure["event_type"], "run.failed")
            self.assertIn("Authentication expired", failure["payload"]["message"])
            self.assertNotIn("sk-123456789012345678901234", failure["payload"]["message"])


if __name__ == "__main__":
    unittest.main()
