#!/usr/bin/env python3
# Run from the repo root, with the python of your Sage: python test/test_nb_run.py -v

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import _mcp
import server


def code_cell(source, cell_id, outputs=None, execution_count=None):
    return {"cell_type": "code", "id": cell_id, "metadata": {},
            "source": source, "outputs": outputs or [],
            "execution_count": execution_count}


def raw_notebook(path, cells):
    """Write a complete 4.5 notebook straight to disk (full control of outputs)."""
    data = {"nbformat": 4, "nbformat_minor": 5,
            "metadata": copy.deepcopy(server.KERNEL_META), "cells": cells}
    Path(path).write_text(json.dumps(data), encoding="utf-8")


def disk(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def norm(value):
    """nbformat stores multi-line strings as lists; compare the joined text."""
    if isinstance(value, list):
        if all(isinstance(v, str) for v in value):
            return "".join(value)
        return [norm(v) for v in value]
    if isinstance(value, dict):
        return {k: norm(v) for k, v in value.items()}
    return value


class TestNbRun(unittest.TestCase):
    """nb_run against the live kernel. Exactly five nb_run calls in this file."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.TemporaryDirectory()
        cls.dir = Path(cls._dir.name)

    @classmethod
    def tearDownClass(cls):
        cls._dir.cleanup()

    def path(self, name):
        return str(self.dir / name)

    def test_disk_and_inline_outputs(self):
        p = self.path("run.ipynb")
        w = _mcp.call("nb_write", path=p, cells=[
            {"cell_type": "markdown", "source": "# run"},
            {"cell_type": "code", "source": "a = 6"},
            {"cell_type": "code", "source": "print(a * 7)"},
            {"cell_type": "code", "source": "a^2 + 1"},
        ])
        self.assertIsNone(w["error"])
        r = _mcp.call("nb_run", path=p)
        self.assertIsNone(r["error"])
        self.assertTrue(r["saved"])
        self.assertFalse(r["truncated"])
        self.assertEqual(len(r["cells"]), 3)          # markdown is omitted
        self.assertEqual([c["cell_id"] for c in r["cells"]], w["cell_ids"][1:])

        data = disk(p)
        code = [c for c in data["cells"] if c["cell_type"] == "code"]
        self.assertEqual([c["execution_count"] for c in code], [1, 2, 3])
        self.assertEqual([norm(c["outputs"]) for c in code],
                         [norm(c["outputs"]) for c in r["cells"]])
        stream = code[1]["outputs"][0]
        self.assertEqual((stream["output_type"], stream["name"]),
                         ("stream", "stdout"))
        self.assertEqual(norm(stream["text"]), "42\n")
        value = code[2]["outputs"][0]
        self.assertEqual(value["output_type"], "execute_result")
        self.assertEqual(norm(value["data"]["text/plain"]), "37")
        self.assertIn("text/latex", value["data"])
        self.assertEqual(value["execution_count"], 3)
        # The written notebook is still nbformat-valid (nb_read validates it).
        self.assertIsNone(_mcp.call("nb_read", path=p)["error"])
        # The kernel is warm and holds the notebook's variables.
        after = _mcp.call("exec", code="a * 2")
        self.assertIsNone(after["error"])
        self.assertEqual(after["result"], "12")

    def test_restart_first_and_stop_at_first_error_saved_true(self):
        self.assertIsNone(_mcp.call("exec", code="pre = 123")["error"])
        p = self.path("restart.ipynb")
        sentinel = {"output_type": "stream", "name": "stdout",
                    "text": "sentinel\n"}
        raw_notebook(p, [
            code_cell("fail_value = pre\nprint(fail_value)", "first"),
            code_cell("untouched = 1", "second", [sentinel], 4),
        ])
        r = _mcp.call("nb_run", path=p)
        self.assertEqual(r["error"]["code"], "cell_execution_failed")
        self.assertIn("NameError", r["error"]["details"])
        self.assertIn("first", r["error"]["details"])
        self.assertTrue(r["saved"])
        self.assertEqual([c["cell_id"] for c in r["cells"]], ["first"])

        data = disk(p)
        first, second = data["cells"]
        # Execution counts continue from the notebook's max: 4 + 1.
        self.assertEqual(first["execution_count"], 5)
        self.assertEqual([o["output_type"] for o in first["outputs"]], ["error"])
        self.assertIn("NameError", first["outputs"][0]["ename"])
        # The cell after the failure keeps exactly what was on disk.
        self.assertEqual(norm(second["outputs"]), norm([sentinel]))
        self.assertEqual(second["execution_count"], 4)

    def test_truncation_inline_only(self):
        p = self.path("big.ipynb")
        w = _mcp.call("nb_write", path=p, cells=[
            {"cell_type": "code", "source": "print('x' * 40000)"}])
        self.assertIsNone(w["error"])
        r = _mcp.call("nb_run", path=p)
        self.assertIsNone(r["error"])
        self.assertTrue(r["saved"])
        self.assertTrue(r["truncated"])
        self.assertEqual(r["cells"][0]["outputs"], [])
        data = disk(p)
        self.assertEqual(len(norm(data["cells"][0]["outputs"][0]["text"])), 40001)
        disk_text = Path(p).read_text(encoding="utf-8")
        inline_text = json.dumps(r["cells"])
        self.assertGreater(len(disk_text), len(inline_text))
        # nb_read recovers the full output that the inline view cut.
        back = _mcp.call("nb_read", path=p, cell_ids=w["cell_ids"])
        self.assertIsNone(back["error"])
        full = norm(back["cells"][0]["outputs"][0]["text"])
        self.assertEqual(len(full), 40001)

    def test_validation_failure_no_restart_no_write(self):
        # The ordering proof for SPEC §7.1: load+validate happen before restart.
        self.assertIsNone(_mcp.call("exec", code="survivor = 99")["error"])
        p = self.path("magic.ipynb")
        raw_notebook(p, [code_cell("%matplotlib inline", "magic")])
        before = Path(p).read_bytes()
        r = _mcp.call("nb_run", path=p)
        self.assertEqual(r["error"]["code"], "unsupported_magic")
        self.assertFalse(r["saved"])
        self.assertFalse(r["truncated"])
        self.assertEqual(r["cells"], [])
        self.assertEqual(Path(p).read_bytes(), before)
        # If nb_run had restarted, `survivor` would be gone.
        after = _mcp.call("exec", code="survivor")
        self.assertIsNone(after["error"])
        self.assertEqual(after["result"], "99")

    def test_zero_code_cells(self):
        p = self.path("markdown-only.ipynb")
        w = _mcp.call("nb_write", path=p,
                      cells=[{"cell_type": "markdown", "source": "# nothing"}])
        self.assertIsNone(w["error"])
        before = Path(p).read_bytes()
        r = _mcp.call("nb_run", path=p)
        self.assertIsNone(r["error"])
        self.assertFalse(r["saved"])
        self.assertFalse(r["truncated"])
        self.assertEqual(r["cells"], [])
        self.assertEqual(Path(p).read_bytes(), before)

class TestNbRunSchema(unittest.TestCase):
    """The declared output schema and the builder agree with the return value."""

    def test_schema_is_closed_and_error_is_required(self):
        schema = server.OUTPUT_SCHEMA_NB_RUN
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]),
                         {"cells", "truncated", "saved", "error"})
        self.assertEqual(set(schema["properties"]), set(schema["required"]))
        self.assertIn("error", schema["required"])
        codes = schema["properties"]["error"]["properties"]["code"]["enum"]
        for code in ("cell_execution_failed", "unsupported_magic", "not_found",
                     "invalid_notebook", "write_failed", "kernel_died",
                     "sage_not_found", "invalid_source", "internal"):
            self.assertIn(code, codes)
        self.assertNotIn("timeout", codes)

    def test_builder_keys(self):
        self.assertEqual(set(server._nb_run_out()),
                         set(server.OUTPUT_SCHEMA_NB_RUN["required"]))


if __name__ == "__main__":
    unittest.main()
