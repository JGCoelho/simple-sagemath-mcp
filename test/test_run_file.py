#!/usr/bin/env python3
# Run from the repo root, with the python of your Sage: python test/test_run_file.py -v
# run_file runs a .py or .sage file as a job in the shared namespace. These tests
# cover the envelope for each suffix, the file errors, the BOM, the traceback, a
# relative path against --directory (a second server), kernel_busy, and a long
# run polled to done.

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "src"))
import _mcp


class TestRunFile(unittest.TestCase):
    """run_file against the shared server. Each job-starting test settles its job."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        _mcp.call("exec", code="warm = 1")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def write(self, name: str, data) -> str:
        path = self.dir / name
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return str(path)

    def test_py_value_and_persistence(self):
        p = self.write("value.py", "ran_py = 5\nran_py * 2\n")
        r = _mcp.call("run_file", path=p)
        self.assertIsNone(r["error"])
        self.assertEqual(r["result"], "10")
        self.assertFalse(r["truncated"])
        # The file ran in the shared namespace, so its names outlive the call.
        self.assertEqual(_mcp.call("exec", code="ran_py + 1")["result"], "6")

    def test_sage_file_is_preparsed(self):
        p = self.write("preparse.sage", "print(2^3)\n2^3\n")
        r = _mcp.call("run_file", path=p)
        self.assertIsNone(r["error"])
        self.assertEqual(r["stdout"], "8\n")
        self.assertEqual(r["result"], "8")

    def test_missing_file_is_file_not_found(self):
        p = str(self.dir / "nope.py")
        r = _mcp.call("run_file", path=p)
        self.assertEqual(r["error"]["code"], "file_not_found")
        self.assertEqual(r["error"]["details"], p)
        self.assertEqual(r["result"], "")

    def test_suffix_must_be_py_or_sage(self):
        for name in ("notes.txt", "upper.PY", "no_suffix"):
            with self.subTest(name=name):
                p = self.write(name, "1 + 1\n")
                r = _mcp.call("run_file", path=p)
                self.assertEqual(r["error"]["code"], "invalid_source")
                self.assertEqual(r["error"]["details"], p)
        # The suffix is checked before the file is looked for.
        r = _mcp.call("run_file", path=str(self.dir / "missing.txt"))
        self.assertEqual(r["error"]["code"], "invalid_source")

    def test_bad_utf8_is_invalid_source(self):
        p = self.write("latin.py", b"x = '\xff'\n")
        r = _mcp.call("run_file", path=p)
        self.assertEqual(r["error"]["code"], "invalid_source")
        self.assertIn("utf-8", r["error"]["details"].lower())

    def test_bom_is_dropped(self):
        p = self.write("bom.py", b"\xef\xbb\xbfbom_value = 11\nbom_value + 1\n")
        r = _mcp.call("run_file", path=p)
        self.assertIsNone(r["error"])
        self.assertEqual(r["result"], "12")

    def test_syntax_error_is_invalid_source(self):
        p = self.write("bad.py", "x = (\n")
        r = _mcp.call("run_file", path=p)
        self.assertEqual(r["error"]["code"], "invalid_source")

    def test_traceback_names_the_file(self):
        p = self.write("boom.py", "a = 1\n\n1/0\n")
        r = _mcp.call("run_file", path=p)
        self.assertEqual(r["error"]["code"], "execution_failed")
        details = r["error"]["details"]
        self.assertIn(p, details)
        self.assertIn(f"{p}:3", details)     # IPython writes "File <path>:<line>"
        self.assertIn("ZeroDivisionError", details)
        self.assertNotIn("_mcp_", details)     # the wrapper's frames are cleaned out

    def test_relative_path_resolves_against_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "rel_ok.py").write_text("rel_value = 'here'\nrel_value\n",
                                              encoding="utf-8")
            server = _mcp.Server("--directory", tmp)
            try:
                r = server.call("run_file", path="rel_ok.py")
                self.assertIsNone(r["error"])
                self.assertEqual(r["result"], "'here'")
            finally:
                server.close()
        # The shared server runs in the test's own directory, where this name is absent.
        missing = _mcp.call("run_file", path="rel_ok.py")
        self.assertEqual(missing["error"]["code"], "file_not_found")

    def test_run_file_while_a_job_runs_is_kernel_busy(self):
        r = _mcp.call("exec", code="import time\ntime.sleep(2)\nbusy_done = 1",
                      timeout=0.2)
        job_id = r.get("job_id")
        try:
            self.assertEqual(r["status"], "running")
            p = self.write("busy.py", "busy_never = 3\n")
            refused = _mcp.call_raw("run_file", path=p)
            self.assertEqual(refused["error"]["code"], "kernel_busy")
            self.assertEqual(refused["error"]["details"], job_id)
            self.assertNotIn("status", refused)     # a refusal is not a running job
            self.assertEqual(_mcp.wait_job(job_id)["status"], "done")
            # The refused file never ran.
            after = _mcp.call("exec", code="busy_never")
            self.assertEqual(after["error"]["code"], "execution_failed")
        finally:
            if job_id:
                _mcp.settle(job_id)

    def test_long_run_file_returns_running_then_done(self):
        p = self.write("slow.py", "import time\ntime.sleep(2)\n"
                                  "slow_value = 'yes'\nprint('after sleep')\n")
        r = _mcp.call("run_file", path=p, timeout=0.2)
        job_id = r.get("job_id")
        try:
            self.assertIsNone(r["error"])
            self.assertEqual(r["status"], "running")
            self.assertTrue(job_id)
            self.assertEqual(_mcp.call("job_status", job_id=job_id)["status"], "running")
            final = _mcp.wait_job(job_id)
            self.assertEqual(final["status"], "done")
            self.assertIsNone(final["error"])
            self.assertEqual(final["stdout"], "after sleep\n")
            self.assertEqual(final["result"], "")
            self.assertEqual(_mcp.call("exec", code="slow_value")["result"], "'yes'")
        finally:
            if job_id:
                _mcp.settle(job_id)

    def test_interrupt_reaches_a_run_file_job(self):
        p = self.write("forever.py", "while True: pass\n")
        r = _mcp.call("run_file", path=p, timeout=0.3)
        job_id = r.get("job_id")
        try:
            self.assertEqual(r["status"], "running")
            self.assertTrue(_mcp.call("interrupt")["interrupted"])
            done = _mcp.wait_job(job_id)
            self.assertEqual(done["status"], "error")
            self.assertIn("KeyboardInterrupt", done["error"]["details"])
            self.assertEqual(_mcp.call("exec", code="2 + 2")["result"], "4")
        finally:
            if job_id:
                _mcp.settle(job_id)

    def test_timeout_zero_rejected(self):
        p = self.write("zero.py", "1\n")
        with self.assertRaises(Exception):
            _mcp.call("run_file", path=p, timeout=0)


if __name__ == "__main__":
    unittest.main()
