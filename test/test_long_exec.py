#!/usr/bin/env python3
"""The only test allowed to take more than 30 s: one long `exec`.

It runs ~45 s of real Sage work (a rational harmonic sum, whose cost grows
quadratically with N) and asserts that the call returns normally with
`error is None` and the expected result: the server neither disconnects nor
loses the reply while the kernel computes.

Opt in from the repo root, with the python of your Sage:

    SAGE_MCP_LONG=1 python test/test_long_exec.py -v

Without `SAGE_MCP_LONG=1`, unittest discovery skips it in ~0 s.
"""

import os
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import _mcp

# H_600000 is a rational whose numerator has 865772 bits; accumulating it takes
# ~43 s here. The bit count is exact and deterministic, so it also proves the
# computation really finished instead of being cut short.
CODE = """
h = sum(1/k for k in range(1, 600001))
h.numerator().nbits()
"""
EXPECTED_NBITS = "865772"


class TestLongExec(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("SAGE_MCP_LONG"),
                         "set SAGE_MCP_LONG=1 to run the long test")
    def test_long_exec_survives(self):
        started = time.monotonic()
        r = _mcp.call("exec", code=CODE)
        elapsed = time.monotonic() - started
        self.assertIsNone(r["error"])
        self.assertFalse(r["truncated"])
        self.assertEqual(r["result"], EXPECTED_NBITS)
        print(f"\nlong exec returned in {elapsed:.1f} s")


if __name__ == "__main__":
    unittest.main()
