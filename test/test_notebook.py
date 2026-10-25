#!/usr/bin/env python3
# Run from the repo root, with the python of your Sage: python test/test_notebook.py

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import nbformat

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import _mcp


class TestNotebook(unittest.TestCase):
    """write_notebook and exec_notebook against the live kernel."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def write(self, name, cells, **kw):
        return _mcp.call("write_notebook", path=str(self.dir / name),
                         cells=cells, **kw)

    def read(self, name):
        nb = nbformat.read(str(self.dir / name), as_version=4)
        nbformat.validate(nb)
        return nb

    def test_write_and_run(self):
        r = self.write("simple.ipynb", ["a = 6", "print(a * 7)", "a^2 + 1"],
                       run=True, overwrite=True)
        self.assertTrue(r["valid"])
        nb = self.read("simple.ipynb")
        self.assertEqual([c.execution_count for c in nb.cells], [1, 2, 3])
        self.assertEqual(nb.cells[1].outputs[0].output_type, "stream")
        self.assertEqual(nb.cells[1].outputs[0].text, "42\n")
        self.assertEqual(nb.cells[2].outputs[0].output_type, "execute_result")
        self.assertEqual(nb.cells[2].outputs[0].execution_count, 3)
        self.assertEqual(nb.cells[2].outputs[0].data["text/plain"], "37")

    def test_latex_is_in_the_output(self):
        self.write("latex.ipynb", ["var('x')", "x^2 + 1"], run=True, overwrite=True)
        data = self.read("latex.ipynb").cells[1].outputs[0].data
        self.assertIn("text/latex", data)
        self.assertTrue(data["text/latex"].startswith("$"))
        self.assertIn("x", data["text/latex"])

    def test_stop_at_first_error(self):
        r = self.write("bad.ipynb", ["x = 1", "1/0", "y = 2"], run=True, overwrite=True)
        self.assertEqual(len(r["cells"]), 2)
        self.assertIn("ZeroDivisionError", r["cells"][1]["error"])
        nb = self.read("bad.ipynb")
        self.assertEqual(nb.cells[1].outputs[0].output_type, "error")
        self.assertEqual(nb.cells[2].outputs, [])          # cell 3 did not run

    def test_run_selected_cells(self):
        self.write("part.ipynb", ["c = 1", "c = 2", "c + 10"], run=False, overwrite=True)
        r = _mcp.call("exec_notebook", path=str(self.dir / "part.ipynb"),
                      session="part", cells=[0, 2])
        self.assertEqual([c["cell"] for c in r["cells"]], [0, 2])
        nb = self.read("part.ipynb")
        self.assertIsNone(nb.cells[1].execution_count)     # cell 2 did not run
        self.assertEqual(nb.cells[2].outputs[0].data["text/plain"], "11")

    def test_magics_are_rejected(self):
        err = _mcp.call_error("write_notebook", path=str(self.dir / "magic.ipynb"),
                              cells=["%matplotlib inline"])
        self.assertIn("IPython magics are not supported", err)

    def test_do_not_overwrite(self):
        self.write("same.ipynb", ["1"], run=False, overwrite=True)
        r = self.write("same.ipynb", ["2"], run=False)
        self.assertFalse(r["valid"])
        self.assertIn("exists", r["error"])

    def test_markdown_cells_are_kept(self):
        nb = nbformat.v4.new_notebook(cells=[
            nbformat.v4.new_markdown_cell("# Title"),
            nbformat.v4.new_code_cell("m = 4"),
            nbformat.v4.new_code_cell("m + 1")])
        nbformat.write(nb, str(self.dir / "md.ipynb"))
        r = _mcp.call("exec_notebook", path=str(self.dir / "md.ipynb"))
        self.assertEqual([c["cell"] for c in r["cells"]], [1, 2])
        nb = self.read("md.ipynb")
        self.assertEqual(nb.cells[0].cell_type, "markdown")

    def test_not_a_notebook_is_an_error(self):
        bad = self.dir / "bad.json"
        bad.write_text(json.dumps({"hello": "world"}))
        err = _mcp.call_error("exec_notebook", path=str(bad))
        self.assertIsNotNone(err)


if __name__ == "__main__":
    unittest.main()
