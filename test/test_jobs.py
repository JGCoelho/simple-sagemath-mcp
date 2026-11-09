#!/usr/bin/env python3
# Run from the repo root, with the python of your Sage: python test/test_jobs.py -v
# A job is an exec (or run_file) call that outlives its wait. These tests cover
# the running envelope, job_status, kernel_busy, unknown_job, and an interrupt or
# a restart that reaches a running job. TestJobRetention needs no kernel.

import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "src"))
import _mcp
from sagemcp import constants, jobs


class TestJobRetention(unittest.TestCase):
    """The bound on finished job records. No kernel, no server."""

    def setUp(self):
        self.state = {"active": None, "jobs": {}}

    def finish_one(self) -> str:
        """Register a job the way `_start_job` does, then finish it the way `_run_job` does."""
        job = jobs.new_job()
        jobs.remember(self.state, job)
        job.status = "done"
        job.result = {"result": "", "stdout": "", "stderr": "",
                      "truncated": False, "error": None}
        job.finished = time.monotonic()
        jobs.trim(self.state)
        return job.job_id

    def test_bound_drops_the_oldest_finished_records(self):
        ids = [self.finish_one() for _ in range(constants.JOB_RETENTION + 5)]
        self.assertEqual(len(self.state["jobs"]), constants.JOB_RETENTION)
        for dropped in ids[:5]:
            self.assertIsNone(jobs.find(self.state, dropped))
        for kept in ids[5:]:
            self.assertIsNotNone(jobs.find(self.state, kept))

    def test_running_job_is_never_dropped(self):
        for _ in range(constants.JOB_RETENTION):
            self.finish_one()
        running = jobs.new_job()
        jobs.remember(self.state, running)        # over the bound by one, for now
        self.assertIs(jobs.find(self.state, running.job_id), running)
        self.assertLessEqual(len(self.state["jobs"]), constants.JOB_RETENTION)
        self.assertEqual(running.status, "running")

    def test_unknown_id_is_none(self):
        self.assertIsNone(jobs.find(self.state, "never-issued"))


class TestJobs(unittest.TestCase):
    """Jobs against the live kernel. The first call warms the kernel up.

    Every test that starts a job settles it in a `finally`, so a failed assertion
    cannot leave a job holding the kernel for the tests after it.
    """

    @classmethod
    def setUpClass(cls):
        _mcp.call("exec", code="warm = 1")

    def test_running_envelope_and_partial_output(self):
        r = _mcp.call("exec", code="print('first', flush=True)\n"
                      "import time\ntime.sleep(3)\nprint('second')", timeout=1)
        job_id = r.get("job_id")
        try:
            self.assertIsNone(r["error"])
            self.assertEqual(r["status"], "running")
            self.assertTrue(job_id)
            self.assertGreater(r["elapsed"], 0)
            # The job is still in its sleep, and its first print is already readable.
            polled = _mcp.call_raw("job_status", job_id=job_id)
            self.assertEqual(polled["status"], "running")
            self.assertIsNone(polled["error"])
            self.assertEqual(polled["stdout"], "first\n")
            self.assertNotIn("result", polled)     # wire dict: the key is absent
            final = _mcp.wait_job(job_id)
            self.assertEqual(final["status"], "done")
            self.assertIsNone(final["error"])
            self.assertEqual(final["stdout"], "first\nsecond\n")
            self.assertEqual(final["result"], "")
            self.assertFalse(final["truncated"])
            self.assertGreater(final["elapsed"], 2.5)
        finally:
            if job_id:
                _mcp.settle(job_id)

    def test_kernel_busy_refuses_other_calls_without_queueing(self):
        r = _mcp.call("exec", code="import time\ntime.sleep(2)\nbusy_done = 7",
                      timeout=0.2)
        job_id = r.get("job_id")
        try:
            self.assertEqual(r["status"], "running")
            refused = _mcp.call_raw("exec", code="1 + 1")
            self.assertEqual(refused["error"]["code"], "kernel_busy")
            self.assertEqual(refused["error"]["details"], job_id)
            self.assertNotIn("status", refused)     # a refusal is not a running job
            nb = _mcp.call("nb_read", path="/nonexistent/none.ipynb")
            self.assertEqual(nb["error"]["code"], "kernel_busy")
            self.assertEqual(nb["error"]["details"], job_id)
            self.assertEqual(_mcp.wait_job(job_id)["status"], "done")
            self.assertIsNone(_mcp.call("exec", code="busy_done")["error"])
        finally:
            if job_id:
                _mcp.settle(job_id)

    def test_unknown_job(self):
        r = _mcp.call("job_status", job_id="does-not-exist")
        self.assertEqual(r["error"]["code"], "unknown_job")
        self.assertEqual(r["error"]["details"], "does-not-exist")
        self.assertEqual(r["stdout"], "")
        self.assertIsNone(r["status"])          # absent, not a status value

    def test_interrupt_reaches_the_job(self):
        r = _mcp.call("exec", code="while True: pass", timeout=0.5)
        job_id = r.get("job_id")
        try:
            self.assertEqual(r["status"], "running")
            i = _mcp.call("interrupt")
            self.assertIsNone(i["error"])
            self.assertTrue(i["interrupted"])
            done = _mcp.wait_job(job_id)
            self.assertEqual(done["status"], "error")
            self.assertEqual(done["error"]["code"], "cell_execution_failed")
            self.assertIn("KeyboardInterrupt", done["error"]["details"])
            self.assertEqual(_mcp.call("exec", code="2 + 2")["result"], "4")
        finally:
            if job_id:
                _mcp.settle(job_id)

    def test_restart_kills_the_job(self):
        r = _mcp.call("exec", code="while True: pass", timeout=0.5)
        job_id = r.get("job_id")
        try:
            self.assertEqual(r["status"], "running")
            restarted = _mcp.call("restart")
            self.assertIsNone(restarted["error"])
            self.assertTrue(restarted["restarted"])
            # restart waits for the job to let go of the kernel, so the record is final.
            done = _mcp.call("job_status", job_id=job_id)
            self.assertEqual(done["status"], "interrupted")
            self.assertEqual(done["error"]["code"], "kernel_died")
            self.assertEqual(_mcp.call("exec", code="3 * 3")["result"], "9")
        finally:
            if job_id:
                _mcp.settle(job_id)


if __name__ == "__main__":
    unittest.main()
