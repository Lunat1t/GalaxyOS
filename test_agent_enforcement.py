#!/usr/bin/env python3
"""Compatibility entry point for the Galaxy 1.7 regression suite."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent/'tests'))
import test_v17
if __name__ == '__main__':
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(getattr(test_v17,name)) for name in ['OrchestratorTests'])
    raise SystemExit(0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1)
