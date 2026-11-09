#!/usr/bin/env python3
# Run from the repo root: python test/test_envelope.py -v
# Kernel-free: the output schemas and the builders in sagemcp.envelope. Each
# schema lists exactly the keys its builder returns, the error codes are the
# wire contract, and a field that tools share is one definition.

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
from sagemcp import envelope

EXEC = envelope.OUTPUT_SCHEMA_EXEC
RUN_FILE = envelope.OUTPUT_SCHEMA_RUN_FILE
JOB_STATUS = envelope.OUTPUT_SCHEMA_JOB_STATUS
INTERRUPT = envelope.OUTPUT_SCHEMA_INTERRUPT
RESTART = envelope.OUTPUT_SCHEMA_RESTART
SCHEMAS = {"exec": EXEC, "run_file": RUN_FILE, "job_status": JOB_STATUS,
           "interrupt": INTERRUPT, "restart": RESTART}

JOB_BASE = {"result", "stdout", "stderr", "truncated", "error"}
JOB_RUNNING = {"status", "job_id", "elapsed"}
STATUS_KEYS = {"status", "elapsed", "stdout", "stderr", "truncated", "result", "error"}


def keys(schema: dict) -> set[str]:
    return set(schema["properties"])


def codes(schema: dict) -> list[str]:
    return schema["properties"]["error"]["properties"]["code"]["enum"]


class TestSchemaShape(unittest.TestCase):

    def test_every_schema_is_a_closed_object(self):
        for name, schema in SCHEMAS.items():
            with self.subTest(tool=name):
                self.assertEqual(schema["type"], "object")
                self.assertFalse(schema["additionalProperties"])
                self.assertLessEqual(set(schema["required"]), keys(schema))

    def test_exec_and_run_file_have_the_job_keys(self):
        for name, schema in (("exec", EXEC), ("run_file", RUN_FILE)):
            with self.subTest(tool=name):
                self.assertEqual(set(schema["required"]), JOB_BASE)
                self.assertEqual(keys(schema), JOB_BASE | JOB_RUNNING)

    def test_job_status_keys(self):
        self.assertEqual(keys(JOB_STATUS), STATUS_KEYS)
        self.assertEqual(set(JOB_STATUS["required"]),
                         {"stdout", "stderr", "truncated", "error"})

    def test_interrupt_and_restart_keys(self):
        self.assertEqual(keys(INTERRUPT), {"interrupted", "error"})
        self.assertEqual(keys(RESTART), {"restarted", "error"})


class TestBuildersMatchSchemas(unittest.TestCase):

    def test_finished_exec_has_exactly_the_base_keys(self):
        self.assertEqual(set(envelope._exec_out()), JOB_BASE)

    def test_running_exec_has_the_base_and_the_job_keys(self):
        running = envelope._exec_running_out(job_id="abc", elapsed=0.5)
        self.assertEqual(set(running), JOB_BASE | JOB_RUNNING)
        self.assertEqual(running["status"], "running")

    def test_job_status_builder_keys_are_declared(self):
        self.assertEqual(set(envelope._job_status_out(error=None)),
                         {"stdout", "stderr", "truncated", "error"})
        finished = envelope._job_status_out(status="done", elapsed=1.0, result="",
                                            error=None)
        self.assertEqual(set(finished), STATUS_KEYS)

    def test_interrupt_and_restart_builders(self):
        self.assertEqual(set(envelope._interrupt_out()), keys(INTERRUPT))
        self.assertEqual(set(envelope._restart_out()), keys(RESTART))

    def test_running_exec_wire_order_is_unchanged(self):
        # The order of the keys is part of the wire contract (see the module docstring).
        running = envelope._exec_running_out(job_id="abc", elapsed=0.5)
        self.assertEqual(list(running),
                         ["result", "stdout", "stderr", "truncated", "error",
                          "status", "job_id", "elapsed"])


class TestErrorCodes(unittest.TestCase):

    def test_code_enums(self):
        self.assertEqual(codes(EXEC), ["invalid_source", "cell_execution_failed",
                                       "gap_crash_loop", "kernel_died",
                                       "sage_not_found", "internal", "kernel_busy"])
        self.assertEqual(codes(RUN_FILE), ["invalid_source", "file_not_found",
                                           "cell_execution_failed", "gap_crash_loop",
                                           "kernel_died", "sage_not_found", "internal",
                                           "kernel_busy"])
        self.assertEqual(codes(JOB_STATUS), ["invalid_source", "cell_execution_failed",
                                             "gap_crash_loop", "kernel_died",
                                             "sage_not_found", "internal", "unknown_job"])
        self.assertEqual(codes(INTERRUPT), ["internal"])
        self.assertEqual(codes(RESTART), ["sage_not_found", "kernel_died", "internal"])


class TestSharedFields(unittest.TestCase):
    """A field that two tools return is the same definition in both schemas."""

    SHARED = ("result", "stdout", "stderr", "truncated", "elapsed")

    def test_shared_fields_are_equal(self):
        for field in self.SHARED:
            for name, schema in (("run_file", RUN_FILE), ("job_status", JOB_STATUS)):
                if field not in schema["properties"]:
                    continue
                with self.subTest(field=field, tool=name):
                    self.assertEqual(schema["properties"][field],
                                     EXEC["properties"][field])

    def test_error_shape_is_shared_and_only_the_codes_differ(self):
        base = EXEC["properties"]["error"]
        for name, schema in SCHEMAS.items():
            error = schema["properties"]["error"]
            with self.subTest(tool=name):
                self.assertEqual(error["description"], base["description"])
                self.assertEqual(error["properties"]["message"],
                                 base["properties"]["message"])
                self.assertEqual(error["properties"]["details"],
                                 base["properties"]["details"])
                self.assertEqual(error["required"], base["required"])

    def test_no_schema_shares_a_dict_with_another(self):
        seen: set[int] = set()
        for name, schema in SCHEMAS.items():
            for field, value in schema["properties"].items():
                with self.subTest(tool=name, field=field):
                    self.assertNotIn(id(value), seen)
                    seen.add(id(value))


if __name__ == "__main__":
    unittest.main()
