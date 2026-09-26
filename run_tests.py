#!/usr/bin/env python3
"""Run Python regression tests and the real JavaScript calculator checks."""
import argparse
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
from galaxy_core.engine.storage import VERSION
ROOT=Path(__file__).resolve().parent

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--report',type=Path);args=parser.parse_args()
    start=time.monotonic()
    suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    node=subprocess.run(['node',str(ROOT/'calculator/test_calculator.js')],text=True,capture_output=True)
    print(node.stdout,end='');print(node.stderr,end='',file=sys.stderr)
    report={'version':VERSION,'tested_at':dt.datetime.now(dt.timezone.utc).isoformat(),
            'python_tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
            'skipped':len(result.skipped),'calculator_pass':node.returncode==0,
            'duration_seconds':round(time.monotonic()-start,3),'live_llm_tested':False,
            'scope':'Deterministic tests, fake agents, real subprocess fixture, real SQLite/Git/Node; no live LLM.',
            'passed':result.wasSuccessful() and node.returncode==0}
    if args.report:args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2));return 0 if report['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
