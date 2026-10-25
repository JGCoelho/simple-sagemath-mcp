#!/usr/bin/env python3
# Run from the repo root, with the python of your Sage: python test/test_exec.py

import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import _mcp
from server import _summarise


class TestSummarise(unittest.TestCase):
    """The outputs to the short text view. No kernel."""

    def test_stdout_and_value(self):
        outs = [{"output_type": "stream", "name": "stdout", "text": "a\n"},
                {"output_type": "execute_result", "data": {"text/plain": "5"},
                 "metadata": {}}]
        self.assertEqual(_summarise(outs), ("a\n", "", "5", None))

    def test_error(self):
        outs = [{"output_type": "error", "ename": "ZeroDivisionError",
                 "evalue": "division by zero", "traceback": ["line"]}]
        self.assertEqual(_summarise(outs), ("", "", None, "ZeroDivisionError: division by zero"))

    def test_no_output(self):
        self.assertEqual(_summarise([]), ("", "", None, None))


class TestExec(unittest.TestCase):
    """exec against the live kernel. The first call starts the kernel."""

    def test_value(self):
        r = _mcp.call("exec", code="2 + 2")
        self.assertEqual(r["result"], "4")
        self.assertIsNone(r["error"])

    def test_preparse(self):
        # `^` makes a power in Sage and XOR in Python. This must give 9.
        self.assertEqual(_mcp.call("exec", code="3^2")["result"], "9")

    def test_error_is_separate(self):
        r = _mcp.call("exec", code="1/0")
        self.assertIsNone(r["result"])
        self.assertTrue(r["error"].startswith("ZeroDivisionError"))

    def test_variables_keep(self):
        _mcp.call("exec", code="a = 7", session="keep")
        self.assertEqual(_mcp.call("exec", code="a * 2", session="keep")["result"], "14")

    def test_sessions_are_isolated(self):
        _mcp.call("exec", code="b = 1", session="one")
        self.assertTrue(_mcp.call("exec", code="b", session="two")["error"]
                        .startswith("NameError"))

    def test_print_goes_to_stdout(self):
        r = _mcp.call("exec", code="print('hello')")
        self.assertEqual(r["stdout"], "hello\n")
        self.assertIsNone(r["result"])

    def test_display_output(self):
        r = _mcp.call("exec", code="display(41 + 1)")
        self.assertEqual(r["result"], "42")
        self.assertIsNone(r["error"])

    def test_stderr_is_separate(self):
        r = _mcp.call("exec", code="import sys\n"
                      "print('out')\nprint('bad', file=sys.stderr)")
        self.assertEqual(r["stdout"], "out\n")
        self.assertEqual(r["stderr"], "bad\n")

    def test_timeout_keeps_the_variables(self):
        _mcp.call("exec", code="t = 5", session="tmo")
        err = _mcp.call_error("exec", code="while True: pass",
                              session="tmo", timeout=1)
        self.assertIn("exceeded 1", err)
        _mcp.call("interrupt")
        time.sleep(0.5)          # the kernel needs time to become idle again
        self.assertEqual(_mcp.call("exec", code="t * 2", session="tmo")["result"],
                         "10")

    def test_list_and_stop(self):
        _mcp.call("exec", code="s = 1", session="dropme")
        self.assertIn("dropme", _mcp.call("list")["sessions"])
        self.assertEqual(_mcp.call("stop", session="dropme"),
                         {"dropped": "dropme"})
        self.assertNotIn("dropme", _mcp.call("list")["sessions"])
        r = _mcp.call("exec", code="s", session="dropme")
        self.assertTrue(r["error"].startswith("NameError"))
        self.assertEqual(_mcp.call("stop", session="ghost"),
                         {"error": "no such session: ghost"})

    def test_gap_crash_guard(self):
        r = _mcp.call("exec", code="import sys\n"
                      "for i in range(3): print('Gap crashed', file=sys.stderr)")
        self.assertIn("GapCrashLoop", r["error"])


if __name__ == "__main__":
    unittest.main()
