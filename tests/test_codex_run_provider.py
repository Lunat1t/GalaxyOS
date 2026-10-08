"""Offline checks for the normalized Codex run contract."""
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest

from galaxy_core.runs.codex_cli import CodexRunProvider, RunRequest


class CodexRunProviderTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
