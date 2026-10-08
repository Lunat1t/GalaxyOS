"""Local API integration checks using a fake provider (no model calls)."""
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen

from galaxy_core.projects import ProjectRegistry
from galaxy_core.runs.codex_cli import ProviderEvent
from galaxy_core.local_api import GalaxyLocalApi


class LocalApiRunTests(unittest.TestCase):
    def test_create_poll_and_read_run_events(self):
        class FakeRun:
            def events(self):
                yield ProviderEvent("message", message="Сделано")
                yield ProviderEvent("result", result={"text": "Сделано"}, input_tokens=5, output_tokens=2)
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
            project_path = root / "project"
            project_path.mkdir()
            subprocess.run(["git", "init", str(project_path)], check=True, capture_output=True)
            registry = ProjectRegistry(root / "data")
            project = registry.add(project_path)
            task = registry.create_task(project.id, "Проверочная задача")
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            api = GalaxyLocalApi(registry, port=port)
            api.runs.provider = FakeProvider()
            server_thread = threading.Thread(target=api.serve_forever, daemon=True)
            server_thread.start()
            base = f"http://127.0.0.1:{port}/api/v1"

            def call(path, method="GET", payload=None):
                body = json.dumps(payload or {}).encode("utf-8") if method != "GET" else None
                request = Request(base + path, data=body, method=method, headers={
                    "Authorization": f"Bearer {api.token}",
                    "Content-Type": "application/json",
                })
                with urlopen(request, timeout=3) as response:
                    return json.loads(response.read().decode("utf-8"))

            try:
                started = call(f"/projects/{project.id}/tasks/{task.id}/run", "POST")
                run_id = started["run"]["id"]
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    status = call(f"/runs/{run_id}")["run"]["status"]
                    if status != "running":
                        break
                    time.sleep(0.01)
                self.assertEqual(status, "completed")
                events = call(f"/runs/{run_id}/events")["events"]
                self.assertEqual(events[0]["event_type"], "run.started")
                self.assertEqual(events[-1]["event_type"], "run.completed")
                self.assertEqual(call(f"/tasks/{task.id}")["task"]["latest_run_id"], run_id)
            finally:
                api.shutdown()
                server_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
