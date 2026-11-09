#!/usr/bin/env python3
# Run from the repo root, with the python of your Sage: python test/test_tool_list.py -v
# The tool list. The default names and order come from a second server started with
# no flag. The legacy tools come from the shared client, which is started with
# --legacy-notebook-tools (see _mcp.SHARED_ARGS). No kernel is started: listing and
# an unknown-tool call never touch Sage.

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "src"))
import _mcp

DEFAULT_TOOLS = ["exec", "run_file", "job_status", "interrupt", "restart"]
LEGACY_TOOLS = ["nb_run", "nb_write", "nb_read", "nb_edit"]


class TestToolList(unittest.TestCase):

    def test_default_names_and_order(self):
        server = _mcp.Server()
        try:
            self.assertEqual(server.tool_names(), DEFAULT_TOOLS)
            # Without the flag the notebook tools do not exist at all.
            with self.assertRaises(Exception):
                server.call("nb_read", path="/nonexistent/none.ipynb")
        finally:
            server.close()

    def test_legacy_flag_appends_the_nb_tools_in_order(self):
        self.assertEqual(_mcp.tool_names(), DEFAULT_TOOLS + LEGACY_TOOLS)


if __name__ == "__main__":
    unittest.main()
