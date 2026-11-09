#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["fastmcp", "jupyter_client", "ipykernel"]
# ///
# Run:      python server.py     (the python of your Sage install)
# Install:  sage -pip install .  (or, from git: sage -pip install
#           git+https://github.com/JGCoelho/simple-sagemath-mcp)
# Or:       ./install.sh         (no pip: checks the python, installs what is
#                                missing, and prints the config block)

"""Entry point for `python server.py`: run the server from `sagemcp`.

This file stays at the repo root because MCP host configs, `install.sh` and
`test/_mcp.py` name this path. The code is the `sagemcp` package under `src/`;
the server's own docstring is `sagemcp/__init__.py`.
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))

from sagemcp.app import main          # noqa: E402  (import must follow the path insert)

if __name__ == "__main__":
    main()
