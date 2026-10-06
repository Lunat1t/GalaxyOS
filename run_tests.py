#!/usr/bin/env python3
"""Run Galaxy Core regression tests, calculator fixture, and optional live LLM tests."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest

from galaxy_core.engine.storage import VERSION

ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Galaxy Test Runner")
    parser.add_argument("--report", type=Path, help="Path to write JSON test report")
    parser.add_argument("--live-llm", action="store_true", help="Enforce live LLM endpoint testing (Ollama/DeepSeek/OpenRouter)")
    args = parser.parse_args()

    start = time.monotonic()

    # Discover and run standard test suite
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    # Run JS calculator fixture
    node = subprocess.run(
        ["node", str(ROOT / "examples/calculator/test_calculator.js")],
        text=True,
        capture_output=True,
    )
    print(node.stdout, end="")
    print(node.stderr, end="", file=sys.stderr)

    # Check live LLM status
    live_tested = False
    live_desc = "none"
    try:
        from tests.test_live_llm import detect_live_provider
        prov, live_desc = detect_live_provider()
        if prov is not None:
            live_tested = True
    except Exception as exc:
        live_desc = f"detection failed: {exc}"

    if args.live_llm and not live_tested:
        print(f"\n[ERROR] --live-llm requested, but no live provider was found ({live_desc}).", file=sys.stderr)
        print("Set DEEPSEEK_API_KEY, OPENROUTER_API_KEY, OPENAI_API_KEY, or start Ollama at 127.0.0.1:11434.", file=sys.stderr)
        return 1

    report = {
        "version": VERSION,
        "tested_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "python_tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "calculator_pass": node.returncode == 0,
        "duration_seconds": round(time.monotonic() - start, 3),
        "live_llm_tested": live_tested,
        "live_provider": live_desc,
        "scope": "Galaxy v1 second brain: memory, provenance, reconciliation, experience, verification, vault, goals, planning, agents and providers.",
        "passed": result.wasSuccessful() and node.returncode == 0,
    }

    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
