#!/usr/bin/env python3
# Run from the repo root, with the python of your Sage: python test/test_tool_list.py -v
# The tool list: the five names and their order, from the shared client and from a
# second server started with no arguments. No kernel is started: listing and an
# unknown-tool call never touch Sage.

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "src"))
import _mcp

TOOLS = ["exec", "run_file", "job_status", "interrupt", "restart"]


class TestToolList(unittest.TestCase):

    def test_shared_client_names_and_order(self):
        self.assertEqual(_mcp.tool_names(), TOOLS)

    def test_second_server_names_and_order(self):
        server = _mcp.Server()
        try:
            self.assertEqual(server.tool_names(), TOOLS)
            # A name that is not a tool is refused, and never reaches Sage.
            with self.assertRaises(Exception):
                server.call("no_such_tool")
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
