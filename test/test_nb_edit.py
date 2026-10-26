#!/usr/bin/env python3
# Run from the repo root, with the python of your Sage: python test/test_nb_edit.py -v

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import _mcp
import server
from server import NotebookError, _apply_ops, _load_nb, _save_nb, _validate_cells


def digest(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def code_cell(source, cell_id="c0", outputs=None, execution_count=None):
    return {"cell_type": "code", "id": cell_id, "metadata": {},
            "source": source, "outputs": outputs or [],
            "execution_count": execution_count}


def markdown_cell(source, cell_id="m0"):
    return {"cell_type": "markdown", "id": cell_id, "metadata": {},
            "source": source}


def raw_notebook(path, cells, minor=5):
    """Write a minimal notebook as JSON, bypassing nbformat's repair pass."""
    data = {"nbformat": 4, "nbformat_minor": minor,
            "metadata": copy.deepcopy(server.KERNEL_META), "cells": cells}
    Path(path).write_text(json.dumps(data), encoding="utf-8")


class NotebookTestCase(unittest.TestCase):
    """A fresh temporary directory per test class; no kernel is ever started."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.TemporaryDirectory()
        cls.dir = Path(cls._dir.name)

    @classmethod
    def tearDownClass(cls):
        cls._dir.cleanup()

    def path(self, name="nb.ipynb"):
        return str(self.dir / name)


class TestNotebookIO(NotebookTestCase):
    """_load_nb / _validate_cells / _save_nb directly. No kernel, no MCP."""

    def test_load_returns_disk_version_and_mints_ids(self):
        p = self.path("load44.ipynb")
        raw_notebook(p, [code_cell("1 + 1")], minor=4)
        data = json.loads(Path(p).read_text())
        del data["cells"][0]["id"]
        Path(p).write_text(json.dumps(data))
        before = digest(p)
        nb, disk_format, disk_minor = _load_nb(p)
        self.assertEqual((disk_format, disk_minor), (4, 4))
        self.assertTrue(nb.cells[0]["id"])
        self.assertEqual(digest(p), before)     # upgrade is in memory only

    def test_not_json(self):
        p = self.path("broken.ipynb")
        Path(p).write_text("{not json")
        with self.assertRaises(NotebookError) as cm:
            _load_nb(p)
        self.assertEqual(cm.exception.code, "invalid_notebook")

    def test_missing_file(self):
        with self.assertRaises(NotebookError) as cm:
            _load_nb(self.path("absent.ipynb"))
        self.assertEqual(cm.exception.code, "not_found")

    def test_duplicate_ids_rejected(self):
        p = self.path("dupe.ipynb")
        raw_notebook(p, [code_cell("1", "same"), code_cell("2", "same")])
        before = digest(p)
        with self.assertRaises(NotebookError) as cm:
            _load_nb(p)
        self.assertEqual(cm.exception.code, "invalid_notebook")
        self.assertIn("same", cm.exception.details)
        self.assertEqual(digest(p), before)

    def test_missing_id_in_45_rejected(self):
        p = self.path("noid.ipynb")
        cell = code_cell("1")
        del cell["id"]
        raw_notebook(p, [cell])
        with self.assertRaises(NotebookError) as cm:
            _load_nb(p)
        self.assertEqual(cm.exception.code, "invalid_notebook")

    def test_magic_details_name_cell_and_line(self):
        nb = server.nbformat.v4.new_notebook(cells=[
            server.nbformat.v4.new_code_cell("%matplotlib inline")])
        with self.assertRaises(NotebookError) as cm:
            _validate_cells(nb)
        self.assertEqual(cm.exception.code, "unsupported_magic")
        self.assertTrue(cm.exception.details.startswith(nb.cells[0].id + ": "))
        self.assertIn("%matplotlib", cm.exception.details)

    def test_shell_escape_rejected(self):
        nb = server.nbformat.v4.new_notebook(cells=[
            server.nbformat.v4.new_code_cell("x = 1\n  !ls")])
        with self.assertRaises(NotebookError) as cm:
            _validate_cells(nb)
        self.assertEqual(cm.exception.code, "unsupported_magic")

    def test_syntax_error_details_name_cell(self):
        nb = server.nbformat.v4.new_notebook(cells=[
            server.nbformat.v4.new_code_cell("1 +")])
        with self.assertRaises(NotebookError) as cm:
            _validate_cells(nb)
        self.assertEqual(cm.exception.code, "invalid_source")
        self.assertTrue(cm.exception.details.startswith(nb.cells[0].id + ": "))

    def test_markdown_is_not_validated_as_code(self):
        nb = server.nbformat.v4.new_notebook(cells=[
            server.nbformat.v4.new_markdown_cell("%not a magic\n!nor this")])
        _validate_cells(nb)                      # does not raise

    def test_missing_directory_is_write_failed_and_leaves_no_tmp(self):
        nb = server.nbformat.v4.new_notebook(cells=[])
        target = self.dir / "no-such-dir" / "x.ipynb"
        with self.assertRaises(NotebookError) as cm:
            _save_nb(nb, str(target))
        self.assertEqual(cm.exception.code, "write_failed")
        self.assertEqual(list(self.dir.glob("**/*.tmp")), [])

    def test_temp_file_lives_next_to_the_target(self):
        nb = server.nbformat.v4.new_notebook(cells=[])
        p = self.path("same-dir.ipynb")
        seen = []
        real = tempfile.NamedTemporaryFile

        def spy(*args, **kwargs):
            seen.append(kwargs.get("dir"))
            return real(*args, **kwargs)

        with mock.patch("tempfile.NamedTemporaryFile", side_effect=spy):
            _save_nb(nb, p)
        self.assertEqual(Path(seen[0]).resolve(), Path(p).parent.resolve())
        self.assertTrue(Path(p).is_file())

    def test_failed_replace_unlinks_temp_and_keeps_target(self):
        p = self.path("replace.ipynb")
        Path(p).write_text("original")
        before = digest(p)
        nb = server.nbformat.v4.new_notebook(cells=[])
        with mock.patch("server.os.replace", side_effect=OSError("nope")):
            with self.assertRaises(NotebookError) as cm:
                _save_nb(nb, p)
        self.assertEqual(cm.exception.code, "write_failed")
        self.assertEqual(digest(p), before)
        self.assertEqual(list(self.dir.glob("*.tmp")), [])

    def test_add_defaults_to_code(self):
        nb = server.nbformat.v4.new_notebook(cells=[
            server.nbformat.v4.new_markdown_cell("m")])
        applied = _apply_ops(nb, [{"op": "add", "source": "1 + 1"}])
        self.assertEqual(applied[0]["op"], "add")
        self.assertEqual(nb.cells[-1]["cell_type"], "code")
        self.assertEqual(applied[0]["cell_id"], nb.cells[-1]["id"])


class TestNbWrite(NotebookTestCase):
    """nb_write over the real MCP client. Still no kernel."""

    def test_ids_and_order(self):
        p = self.path("write.ipynb")
        r = _mcp.call("nb_write", path=p, cells=[
            {"cell_type": "code", "source": "a = 1"},
            {"cell_type": "markdown", "source": "# title"},
            {"cell_type": "raw", "source": "raw text"},
        ])
        self.assertIsNone(r["error"])
        self.assertTrue(r["saved"])
        self.assertEqual(len(r["cell_ids"]), 3)
        self.assertEqual(len(set(r["cell_ids"])), 3)
        data = json.loads(Path(p).read_text())
        self.assertEqual([c["cell_type"] for c in data["cells"]],
                         ["code", "markdown", "raw"])
        self.assertEqual([c["id"] for c in data["cells"]], r["cell_ids"])

    def test_disk_is_45_with_kernelspec(self):
        p = self.path("meta.ipynb")
        r = _mcp.call("nb_write", path=p,
                      cells=[{"cell_type": "code", "source": "1"}])
        self.assertIsNone(r["error"])
        data = json.loads(Path(p).read_text())
        self.assertEqual(data["nbformat"], 4)
        self.assertEqual(data["nbformat_minor"], 5)
        self.assertEqual(data["metadata"]["kernelspec"]["name"], "sagemath")
        self.assertEqual(data["metadata"]["language_info"]["name"], "sage")

    def test_exists_guard(self):
        p = self.path("guard.ipynb")
        first = _mcp.call("nb_write", path=p,
                          cells=[{"cell_type": "code", "source": "1"}])
        self.assertTrue(first["saved"])
        before = digest(p)
        second = _mcp.call("nb_write", path=p,
                           cells=[{"cell_type": "code", "source": "2"}])
        self.assertEqual(second["error"]["code"], "exists")
        self.assertFalse(second["saved"])
        self.assertEqual(second["cell_ids"], [])
        self.assertEqual(digest(p), before)

    def test_overwrite(self):
        p = self.path("overwrite.ipynb")
        _mcp.call("nb_write", path=p, cells=[{"cell_type": "code", "source": "1"}])
        r = _mcp.call("nb_write", path=p, overwrite=True,
                      cells=[{"cell_type": "code", "source": "2"}])
        self.assertIsNone(r["error"])
        self.assertTrue(r["saved"])
        nb, _, _ = _load_nb(p)
        self.assertEqual(nb.cells[0]["source"], "2")

    def test_magic_rejected_without_writing(self):
        p = self.path("magic.ipynb")
        r = _mcp.call("nb_write", path=p,
                      cells=[{"cell_type": "code", "source": "%matplotlib inline"}])
        self.assertEqual(r["error"]["code"], "unsupported_magic")
        self.assertFalse(r["saved"])
        self.assertEqual(r["cell_ids"], [])
        self.assertFalse(Path(p).exists())

    def test_syntax_rejected_without_writing(self):
        p = self.path("syntax.ipynb")
        r = _mcp.call("nb_write", path=p,
                      cells=[{"cell_type": "code", "source": "1 +"}])
        self.assertEqual(r["error"]["code"], "invalid_source")
        self.assertFalse(r["saved"])
        self.assertFalse(Path(p).exists())

    def test_missing_directory_write_failed(self):
        target = self.dir / "absent" / "x.ipynb"
        r = _mcp.call("nb_write", path=str(target),
                      cells=[{"cell_type": "code", "source": "1"}])
        self.assertEqual(r["error"]["code"], "write_failed")
        self.assertFalse(r["saved"])
        self.assertFalse(target.exists())

    def test_malformed_cell_is_reported_not_raised(self):
        p = self.path("malformed.ipynb")
        r = _mcp.call("nb_write", path=p,
                      cells=[{"cell_type": "code"}])
        self.assertEqual(r["error"]["code"], "invalid_notebook")
        self.assertFalse(r["saved"])


class TestNbRead(NotebookTestCase):
    """nb_read: order, unknown ids, and the on-disk version (G2)."""

    def _write_three(self, name="read.ipynb"):
        p = self.path(name)
        r = _mcp.call("nb_write", path=p, cells=[
            {"cell_type": "code", "source": "a = 1"},
            {"cell_type": "markdown", "source": "md"},
            {"cell_type": "code", "source": "b = 2"},
        ])
        self.assertIsNone(r["error"])
        return p, r["cell_ids"]

    def test_all_in_order(self):
        p, ids = self._write_three()
        r = _mcp.call("nb_read", path=p)
        self.assertIsNone(r["error"])
        self.assertEqual(r["nbformat"], 4)
        self.assertEqual(r["nbformat_minor"], 5)
        self.assertEqual([c["cell_id"] for c in r["cells"]], ids)
        self.assertEqual([c["source"] for c in r["cells"]],
                         ["a = 1", "md", "b = 2"])

    def test_selected_order(self):
        p, ids = self._write_three("selected.ipynb")
        r = _mcp.call("nb_read", path=p, cell_ids=[ids[2], ids[0]])
        self.assertIsNone(r["error"])
        self.assertEqual([c["cell_id"] for c in r["cells"]], [ids[2], ids[0]])

    def test_unknown_id(self):
        p, _ = self._write_three("unknown.ipynb")
        r = _mcp.call("nb_read", path=p, cell_ids=["no-such-cell"])
        self.assertEqual(r["error"]["code"], "unknown_cell_id")
        self.assertEqual(r["error"]["details"], "no-such-cell")
        self.assertEqual(r["cells"], [])
        self.assertIsNone(r["nbformat"])
        self.assertIsNone(r["nbformat_minor"])

    def test_read_does_not_write(self):
        p, _ = self._write_three("readonly.ipynb")
        before = digest(p)
        self.assertIsNone(_mcp.call("nb_read", path=p)["error"])
        self.assertEqual(digest(p), before)

    def test_44_reports_disk_version(self):
        p = self.path("legacy44.ipynb")
        raw_notebook(p, [code_cell("1 + 1")], minor=4)
        data = json.loads(Path(p).read_text())
        del data["cells"][0]["id"]
        Path(p).write_text(json.dumps(data))
        before = digest(p)
        r = _mcp.call("nb_read", path=p)
        self.assertIsNone(r["error"])
        self.assertEqual((r["nbformat"], r["nbformat_minor"]), (4, 4))
        self.assertTrue(r["cells"][0]["cell_id"])
        self.assertEqual(digest(p), before)

    def test_outputs_only_on_code_cells(self):
        p = self.path("outputs.ipynb")
        result = {"output_type": "execute_result", "execution_count": 7,
                  "data": {"text/plain": "42"}, "metadata": {}}
        raw_notebook(p, [code_cell("6*7", "code1", [result], 7),
                         markdown_cell("just text", "md1")])
        r = _mcp.call("nb_read", path=p)
        self.assertIsNone(r["error"])
        by_id = {c["cell_id"]: c for c in r["cells"]}
        self.assertEqual(by_id["code1"]["outputs"][0]["data"]["text/plain"], "42")
        self.assertIsNone(by_id["md1"].get("outputs"))

    def test_not_found(self):
        r = _mcp.call("nb_read", path=self.path("nope.ipynb"))
        self.assertEqual(r["error"]["code"], "not_found")
        self.assertEqual(r["cells"], [])
        self.assertIsNone(r["nbformat"])
        self.assertIsNone(r["nbformat_minor"])


class TestNbEdit(NotebookTestCase):
    """nb_edit: all-or-nothing op engine, addressed by cell id."""

    def _write(self, name="edit.ipynb", cells=None):
        p = self.path(name)
        cells = cells or [
            {"cell_type": "code", "source": "a = 1"},
            {"cell_type": "markdown", "source": "md"},
        ]
        r = _mcp.call("nb_write", path=p, cells=cells)
        self.assertIsNone(r["error"])
        return p, r["cell_ids"]

    def _read(self, p):
        r = _mcp.call("nb_read", path=p)
        self.assertIsNone(r["error"])
        return r["cells"]

    def test_edit_keeps_type(self):
        p, ids = self._write("keep.ipynb")
        r = _mcp.call("nb_edit", path=p,
                      ops=[{"op": "edit", "cell_id": ids[1], "source": "new md"}])
        self.assertIsNone(r["error"])
        self.assertTrue(r["saved"])
        cells = self._read(p)
        self.assertEqual(cells[1]["cell_type"], "markdown")
        self.assertEqual(cells[1]["source"], "new md")

    def test_add_after(self):
        p, ids = self._write("after.ipynb")
        r = _mcp.call("nb_edit", path=p, ops=[
            {"op": "add", "after": ids[0], "source": "print(1)",
             "cell_type": "code"}])
        self.assertIsNone(r["error"])
        cells = self._read(p)
        self.assertEqual(len(cells), 3)
        self.assertEqual(cells[1]["cell_id"], r["applied"][0]["cell_id"])
        self.assertEqual(cells[1]["source"], "print(1)")

    def test_add_append_by_default(self):
        p, ids = self._write("append.ipynb")
        r = _mcp.call("nb_edit", path=p,
                      ops=[{"op": "add", "source": "appended"}])
        self.assertIsNone(r["error"])
        cells = self._read(p)
        self.assertEqual(cells[-1]["source"], "appended")
        self.assertEqual(cells[-1]["cell_type"], "code")

    def test_remove(self):
        p, ids = self._write("remove.ipynb")
        r = _mcp.call("nb_edit", path=p,
                      ops=[{"op": "remove", "cell_id": ids[0]}])
        self.assertIsNone(r["error"])
        cells = self._read(p)
        self.assertEqual([c["cell_id"] for c in cells], [ids[1]])

    def test_remove_only_cell_allowed(self):
        p, ids = self._write("only.ipynb",
                             cells=[{"cell_type": "code", "source": "1"}])
        r = _mcp.call("nb_edit", path=p,
                      ops=[{"op": "remove", "cell_id": ids[0]}])
        self.assertIsNone(r["error"])
        self.assertEqual(self._read(p), [])

    def test_applied_order(self):
        p, ids = self._write("order.ipynb")
        r = _mcp.call("nb_edit", path=p, ops=[
            {"op": "edit", "cell_id": ids[0], "source": "a = 2"},
            {"op": "add", "source": "b = 3"},
            {"op": "remove", "cell_id": ids[1]},
        ])
        self.assertIsNone(r["error"])
        self.assertEqual([a["op"] for a in r["applied"]],
                         ["edit", "add", "remove"])
        self.assertEqual(r["applied"][0]["cell_id"], ids[0])
        self.assertEqual(r["applied"][2]["cell_id"], ids[1])

    def test_unknown_after_id(self):
        p, _ = self._write("unknown-after.ipynb")
        r = _mcp.call("nb_edit", path=p, ops=[
            {"op": "add", "after": "missing", "source": "1"}])
        self.assertEqual(r["error"]["code"], "unknown_cell_id")
        self.assertTrue(r["error"]["details"].startswith("op 0 (add): "))
        self.assertIn("missing", r["error"]["details"])
        self.assertEqual(r["applied"], [])
        self.assertFalse(r["saved"])

    def test_op_index_in_details(self):
        p, ids = self._write("index.ipynb")
        r = _mcp.call("nb_edit", path=p, ops=[
            {"op": "edit", "cell_id": ids[0], "source": "a = 2"},
            {"op": "edit", "cell_id": "gone", "source": "x"}])
        self.assertEqual(r["error"]["code"], "unknown_cell_id")
        self.assertTrue(r["error"]["details"].startswith("op 1 (edit): "))
        self.assertEqual(r["applied"], [])

    def test_failure_leaves_file(self):
        p, ids = self._write("atomic.ipynb")
        before = digest(p)
        r = _mcp.call("nb_edit", path=p, ops=[
            {"op": "edit", "cell_id": ids[0], "source": "a = 2"},
            {"op": "remove", "cell_id": "gone"}])
        self.assertEqual(r["error"]["code"], "unknown_cell_id")
        self.assertEqual(r["applied"], [])
        self.assertFalse(r["saved"])
        self.assertEqual(digest(p), before)

    def test_never_creates_file(self):
        p = self.path("never.ipynb")
        r = _mcp.call("nb_edit", path=p,
                      ops=[{"op": "add", "source": "1"}])
        self.assertEqual(r["error"]["code"], "not_found")
        self.assertEqual(r["applied"], [])
        self.assertFalse(Path(p).exists())

    def test_duplicate_ids_rejected(self):
        p = self.path("dupe-edit.ipynb")
        raw_notebook(p, [code_cell("1", "same"), code_cell("2", "same")])
        before = digest(p)
        r = _mcp.call("nb_edit", path=p,
                      ops=[{"op": "add", "source": "3"}])
        self.assertEqual(r["error"]["code"], "invalid_notebook")
        self.assertEqual(r["applied"], [])
        self.assertEqual(digest(p), before)

    def test_preexisting_invalid_cell_blocks_unrelated_edit(self):
        p = self.path("preexisting.ipynb")
        raw_notebook(p, [code_cell("1 +", "bad"),
                         markdown_cell("ok", "md")])
        before = digest(p)
        r = _mcp.call("nb_edit", path=p, ops=[
            {"op": "edit", "cell_id": "md", "source": "changed"}])
        self.assertEqual(r["error"]["code"], "invalid_source")
        self.assertTrue(r["error"]["details"].startswith("op 0 (edit): "))
        self.assertIn("bad", r["error"]["details"])
        self.assertEqual(r["applied"], [])
        self.assertEqual(digest(p), before)

    def test_edit_that_introduces_syntax_error(self):
        p, ids = self._write("introduced.ipynb")
        before = digest(p)
        r = _mcp.call("nb_edit", path=p, ops=[
            {"op": "edit", "cell_id": ids[0], "source": "1 +"}])
        self.assertEqual(r["error"]["code"], "invalid_source")
        self.assertTrue(r["error"]["details"].startswith("op 0 (edit): "))
        self.assertEqual(r["applied"], [])
        self.assertEqual(digest(p), before)

    def test_malformed_ops_are_invalid_op(self):
        p, ids = self._write("malformed.ipynb")
        for ops in ([{"op": "bogus"}],
                    [{"op": "add"}],
                    [{"op": "remove"}],
                    [{"op": "edit", "cell_id": ids[0], "source": "1",
                      "extra": True}]):
            r = _mcp.call("nb_edit", path=p, ops=ops)
            self.assertEqual(r["error"]["code"], "invalid_op", ops)
            self.assertEqual(r["applied"], [])

    def test_edit_does_not_execute_or_start_a_kernel(self):
        # nb_edit validates statically; a code cell that would raise at runtime
        # is still written, and no kernel is started by the call.
        p, ids = self._write("noexec.ipynb")
        r = _mcp.call("nb_edit", path=p, ops=[
            {"op": "edit", "cell_id": ids[0], "source": "1/0"}])
        self.assertIsNone(r["error"])
        self.assertTrue(r["saved"])


class TestNotebookSchemas(unittest.TestCase):
    """Every notebook schema is closed, requires error, and matches its builder."""

    def test_closed_and_required(self):
        cases = [
            (server.OUTPUT_SCHEMA_NB_RUN,
             {"cells", "truncated", "saved", "error"}),
            (server.OUTPUT_SCHEMA_NB_WRITE, {"cell_ids", "saved", "error"}),
            (server.OUTPUT_SCHEMA_NB_READ,
             {"nbformat", "nbformat_minor", "cells", "error"}),
            (server.OUTPUT_SCHEMA_NB_EDIT, {"applied", "saved", "error"}),
        ]
        for schema, required in cases:
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(set(schema["required"]), required)
            self.assertEqual(set(schema["properties"]), required)
            self.assertIn("error", schema["required"])
            codes = schema["properties"]["error"]["properties"]["code"]["enum"]
            self.assertNotIn("timeout", codes)

    def test_builder_keys(self):
        self.assertEqual(set(server._nb_run_out()),
                         {"cells", "truncated", "saved", "error"})
        self.assertEqual(set(server._nb_write_out()),
                         {"cell_ids", "saved", "error"})
        self.assertEqual(set(server._nb_read_out()),
                         {"nbformat", "nbformat_minor", "cells", "error"})
        self.assertEqual(set(server._nb_edit_out()),
                         {"applied", "saved", "error"})


if __name__ == "__main__":
    unittest.main()
