#!/usr/bin/env python3
# Run from the repo root, with the python of your Sage: python test/test_exec.py -v

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import _mcp
import server
from server import _exec_view, _summarise, _truncate

MARKER_SLACK = 60          # room for "\n… [truncated N characters]"


class TestSummarise(unittest.TestCase):
    """The outputs to the short text view, and the truncation policy. No kernel."""

    def test_stdout_and_value(self):
        outs = [{"output_type": "stream", "name": "stdout", "text": "a\n"},
                {"output_type": "execute_result", "data": {"text/plain": "5"},
                 "metadata": {}}]
        self.assertEqual(_summarise(outs), ("a\n", "", "5", None))

    def test_error(self):
        outs = [{"output_type": "error", "ename": "ZeroDivisionError",
                 "evalue": "division by zero", "traceback": ["line"]}]
        self.assertEqual(_summarise(outs),
                         ("", "", None, "ZeroDivisionError: division by zero"))

    def test_no_output(self):
        self.assertEqual(_summarise([]), ("", "", None, None))
        self.assertEqual(_exec_view([]),
                         {"result": "", "stdout": "", "stderr": "", "truncated": False})

    def test_truncate_marks_the_cut(self):
        text, cut = _truncate("x" * 50, 10)
        self.assertTrue(cut)
        self.assertTrue(text.startswith("x" * 10))
        self.assertIn("truncated 40 characters", text)
        self.assertEqual(text[:10], "x" * 10)

    def test_truncate_leaves_short_text(self):
        self.assertEqual(_truncate("short", 10), ("short", False))
        self.assertEqual(_truncate("", 0), ("", False))

    def test_exec_view_budget_order(self):
        # result, then stdout, then stderr share one budget.
        outs = [{"output_type": "execute_result",
                 "data": {"text/plain": "r" * 100}, "metadata": {}},
                {"output_type": "stream", "name": "stdout", "text": "o" * 100},
                {"output_type": "stream", "name": "stderr", "text": "e" * 100}]
        with mock.patch.object(server, "MAX_INLINE_CHARS", 30):
            view = _exec_view(outs)
        self.assertTrue(view["truncated"])
        # `result` is cut first and uses the whole budget; the marker it gets
        # is not content, so the later fields have no budget left.
        self.assertTrue(view["result"].startswith("r" * 30))
        self.assertIn("truncated 70 characters", view["result"])
        self.assertNotIn("o", view["stdout"])
        self.assertNotIn("e" * 10, view["stderr"])

    def test_exec_view_short_payload_is_not_cut(self):
        outs = [{"output_type": "stream", "name": "stdout", "text": "hi\n"}]
        self.assertEqual(_exec_view(outs)["truncated"], False)


class TestExec(unittest.TestCase):
    """exec against the live kernel. The first call starts the kernel."""

    def test_gap_crash_loop(self):
        r = _mcp.call("exec", code="import sys\n"
                      "for i in range(3):\n"
                      "    print('Gap crashed', file=sys.stderr)")
        self.assertEqual(r["error"]["code"], "gap_crash_loop")
        self.assertIn("Gap crashed", r["stderr"])
        self.assertIn("Gap crashed", r["error"]["details"])

    def test_interrupt_busy(self):
        self.assertIsNone(_mcp.call("exec", code="survivor = 41")["error"])
        fut = _mcp.call_async("exec", code="while True: pass")
        time.sleep(1.0)                      # let the computation start
        r = _mcp.call("interrupt")
        self.assertIsNone(r["error"])
        self.assertTrue(r["interrupted"])
        busy = _mcp.unwrap(fut.result(120))
        self.assertEqual(busy["error"]["code"], "cell_execution_failed")
        self.assertIn("KeyboardInterrupt", busy["error"]["details"])
        # The interrupt keeps the namespace.
        self.assertEqual(_mcp.call("exec", code="survivor + 1")["result"], "42")

    def test_interrupt_idle(self):
        r = _mcp.call("interrupt")
        self.assertIsNone(r["error"])
        self.assertFalse(r["interrupted"])

    def test_large_output_truncated(self):
        r = _mcp.call("exec", code="print('x' * 40000)")
        self.assertIsNone(r["error"])
        self.assertTrue(r["truncated"])
        self.assertEqual(r["result"], "")
        self.assertLessEqual(len(r["stdout"]),
                             server.MAX_INLINE_CHARS + MARKER_SLACK)

    def test_no_session_param(self):
        # There is exactly one namespace: `session` is not part of the input.
        with self.assertRaises(Exception):
            _mcp.call("exec", code="1 + 1", session="default")
        self.assertIsNone(_mcp.call("exec", code="1 + 1")["error"])

    def test_persistence(self):
        self.assertIsNone(_mcp.call("exec", code="kept = 7")["error"])
        self.assertEqual(_mcp.call("exec", code="kept * 2")["result"], "14")

    def test_preparse(self):
        # `^` is a power in Sage and XOR in Python. This must give 9.
        r = _mcp.call("exec", code="3^2")
        self.assertIsNone(r["error"])
        self.assertEqual(r["result"], "9")

    def test_print_stdout(self):
        r = _mcp.call("exec", code="print('hello')")
        self.assertIsNone(r["error"])
        self.assertEqual(r["stdout"], "hello\n")
        self.assertEqual(r["stderr"], "")
        self.assertEqual(r["result"], "")

    def test_restart_while_busy(self):
        # Warm the kernel first: this test is about a running computation, not
        # about interrupting a kernel that is still starting (SIGINT sent during
        # the sage.all import is deferred, so restart falls back to the hard kill).
        self.assertIsNone(_mcp.call("exec", code="warm = 1")["error"])
        fut = _mcp.call_async("exec", code="while True: pass")
        time.sleep(1.0)                      # let the computation start
        started = time.monotonic()
        r = _mcp.call("restart")
        elapsed = time.monotonic() - started
        self.assertIsNone(r["error"])
        self.assertTrue(r["restarted"])
        self.assertLess(elapsed, server.SHUTDOWN_TIMEOUT)
        busy = _mcp.unwrap(fut.result(120))
        self.assertIn(busy["error"]["code"],
                      ("kernel_died", "cell_execution_failed"))

    def test_first_call_after_restart_is_interruptible(self):
        # The namespace is seeded when the kernel starts, so a runaway call made
        # right after `restart` still answers an interrupt. Before that change
        # the first call paid the `from sage.all import *` cost itself and the
        # SIGINT was deferred into the computation.
        self.assertTrue(_mcp.call("restart")["restarted"])
        fut = _mcp.call_async("exec", code="while True: pass")
        time.sleep(1.0)
        started = time.monotonic()
        i = _mcp.call("interrupt")
        self.assertIsNone(i["error"])
        self.assertTrue(i["interrupted"])
        busy = _mcp.unwrap(fut.result(120))
        self.assertLess(time.monotonic() - started, server.SHUTDOWN_TIMEOUT)
        self.assertEqual(busy["error"]["code"], "cell_execution_failed")
        self.assertIn("KeyboardInterrupt", busy["error"]["details"])

    def test_restart_wipes(self):
        self.assertIsNone(_mcp.call("exec", code="wiped = 5")["error"])
        r = _mcp.call("restart")
        self.assertIsNone(r["error"])
        self.assertTrue(r["restarted"])
        after = _mcp.call("exec", code="wiped")
        self.assertEqual(after["error"]["code"], "cell_execution_failed")
        self.assertIn("NameError", after["error"]["details"])

    def test_runtime_error(self):
        r = _mcp.call("exec", code="1/0")
        self.assertEqual(r["error"]["code"], "cell_execution_failed")
        self.assertIn("ZeroDivisionError", r["error"]["details"])
        self.assertEqual(r["stdout"], "")
        self.assertEqual(r["stderr"], "")

    def test_stderr_separate(self):
        r = _mcp.call("exec", code="import sys\n"
                      "print('out')\nprint('bad', file=sys.stderr)")
        self.assertIsNone(r["error"])
        self.assertEqual(r["stdout"], "out\n")
        self.assertEqual(r["stderr"], "bad\n")

    def test_syntax_error(self):
        r = _mcp.call("exec", code="1 +")
        self.assertEqual(r["error"]["code"], "invalid_source")
        self.assertTrue(r["error"]["details"])

    def test_timeout_then_interrupt(self):
        self.assertIsNone(_mcp.call("exec", code="timeout_kept = 5")["error"])
        r = _mcp.call("exec", code="while True: pass", timeout=1)
        self.assertEqual(r["error"]["code"], "timeout")
        self.assertFalse(r["truncated"])
        i = _mcp.call("interrupt")
        self.assertIsNone(i["error"])
        self.assertTrue(i["interrupted"])
        time.sleep(0.5)                      # the kernel needs to become idle
        # The namespace survived the timeout.
        self.assertEqual(_mcp.call("exec", code="timeout_kept * 2")["result"], "10")

    def test_timeout_zero_rejected(self):
        with self.assertRaises(Exception):
            _mcp.call("exec", code="1", timeout=0)

    def test_value(self):
        r = _mcp.call("exec", code="2 + 2")
        self.assertIsNone(r["error"])
        self.assertEqual(r["result"], "4")
        self.assertEqual(r["stdout"], "")
        self.assertEqual(r["stderr"], "")
        self.assertFalse(r["truncated"])


class TestExecSchema(unittest.TestCase):
    """The declared output schema and the builder agree with the return value."""

    def test_schema_is_closed_and_error_is_required(self):
        schema = server.OUTPUT_SCHEMA_EXEC
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]),
                         {"result", "stdout", "stderr", "truncated", "error"})
        self.assertEqual(set(schema["properties"]), set(schema["required"]))
        self.assertIn("error", schema["required"])
        self.assertIn("cell_execution_failed",
                      schema["properties"]["error"]["properties"]["code"]["enum"])

    def test_return_keys(self):
        r = _mcp.call("exec", code="1")
        self.assertEqual(set(r),
                         {"result", "stdout", "stderr", "truncated", "error"})


if __name__ == "__main__":
    unittest.main()
