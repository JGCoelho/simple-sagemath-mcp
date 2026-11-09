"""The FastMCP application: one lifespan, one `mcp`, one entry point.

`main` is the `simple-sagemath-mcp` console script and what the root `server.py`
shim calls. The tools register into `mcp` when the package `__init__` imports
`tools_exec`, `tools_notebook` and `tools_kernel`, in that order.

`mcp` and `sys` stay module globals on purpose: the tests patch `app.mcp` and
`sys.executable` through the module attribute.
"""

import argparse
import asyncio
import os
import sys

from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan

from .kernel import _stop_kernel


# --- MCP ---------------------------------------------------------------------

@lifespan
async def sage_lifespan(server):
    """Hold the one kernel. Nothing starts at connect; shutdown stops it."""
    # active: the job holding the kernel, or None. jobs: id -> Job, oldest first.
    state = {"km": None, "kc": None, "busy": False, "active": None, "jobs": {}}
    try:
        yield state
    finally:
        await asyncio.to_thread(_stop_kernel, state)


mcp = FastMCP("SageMath", lifespan=sage_lifespan)


def main(argv: list[str] | None = None) -> None:
    """Run the server on stdio. Parse `argv` (default: the command line).

    `--directory` changes the working directory before the kernel starts, so
    relative paths in the agent's code point at the agent's work. This is the
    `simple-sagemath-mcp` console-script entry point.
    """
    parser = argparse.ArgumentParser(description="SageMath MCP server.")
    parser.add_argument("--directory", metavar="DIR",
                        help="start the kernel in this directory")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.directory:
        os.chdir(args.directory)
    mcp.run()

