import tempfile
import unittest
from pathlib import Path

from galaxy_core.mcp_server import get_context


class MCPServerTests(unittest.TestCase):
    def test_get_context_compiles_fresh_ranked_repository_evidence(self):
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as home_dir:
            repo = Path(repo_dir)
            (repo / "payments.py").write_text(
                "def refund_payment(order_id):\n    return {'order_id': order_id, 'status': 'refunded'}\n",
                encoding="utf-8",
            )
            result = get_context("fix refund payment status", str(repo), galaxy_home=home_dir,
                                 budget_tokens=2000, max_files=4)
            self.assertEqual(result["files"][0]["path"], "payments.py")
            self.assertIn("refund_payment", result["context"])
            self.assertTrue(result["world_snapshot_id"])
            self.assertLessEqual(result["estimated_tokens"], result["budget_tokens"])

    def test_get_context_rejects_invalid_inputs(self):
        with self.assertRaisesRegex(ValueError, "task_description"):
            get_context("  ", ".")
        with self.assertRaisesRegex(ValueError, "repo_path"):
            get_context("task", "/definitely/missing/galaxy-repository")


if __name__ == "__main__":
    unittest.main()

