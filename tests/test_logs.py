import json
import unittest
from pathlib import Path
from unittest.mock import patch
import io

import galaxy

class GalaxyLogsTests(unittest.TestCase):
    def test_logs_list(self):
        parser = galaxy.parser()
        args = parser.parse_args(["logs", "list"])
        self.assertEqual(args.command, "logs")
        self.assertEqual(args.target, "list")

        f = io.StringIO()
        with patch("sys.stdout", f):
            code = galaxy.cmd_logs(args)
        self.assertEqual(code, 0)
        output = f.getvalue()
        self.assertIn("Доступные логи Galaxy", output)

    def test_logs_json(self):
        parser = galaxy.parser()
        args = parser.parse_args(["logs", "list", "--json"])
        f = io.StringIO()
        with patch("sys.stdout", f):
            code = galaxy.cmd_logs(args)
        self.assertEqual(code, 0)
        data = json.loads(f.getvalue())
        self.assertIn("runs", data)
        self.assertIn("tasks", data)

    def test_logs_target_filtering(self):
        runs_dir = galaxy.ROOT / "data" / "runs"
        if not runs_dir.is_dir():
            self.skipTest("No generated data/runs directory in clean core build")
        runs = [r for r in runs_dir.iterdir() if r.is_dir()]
        if not runs:
            self.skipTest("No runs in data/runs")
        run_id = runs[0].name
        parser = galaxy.parser()
        args = parser.parse_args(["logs", run_id, "--json", "-n", "10"])
        f = io.StringIO()
        with patch("sys.stdout", f):
            code = galaxy.cmd_logs(args)
        self.assertEqual(code, 0)
        logs = json.loads(f.getvalue())
        self.assertIsInstance(logs, list)
        if logs:
            self.assertIn("node_id", logs[0])
            self.assertIn("lines", logs[0])

if __name__ == "__main__":
    unittest.main()
