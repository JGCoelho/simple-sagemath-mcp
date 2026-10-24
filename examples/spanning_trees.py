#!/usr/bin/env python3
"""Example script for the `run_file` tool.

`run_file` executes a file that already exists -- it copies nothing -- in Sage's
own interpreter, with cwd set to the script's directory, and returns the exit
code, the duration, and stdout/stderr inline.  `lang="auto"` honours the "python"
extension and this shebang; use `lang="sage"` to shell out to `sage` instead.

    run_file(path="/…/examples/spanning_trees.py", args=["--n", "6"])
"""

import sys

from sage.all import graphs  # only available in Sage's interpreter

n = 6
if "--n" in sys.argv:
    n = int(sys.argv[sys.argv.index("--n") + 1])

total = 0
connected = 0
for g in graphs(n):
    if g.is_connected():
        connected += 1
        total += g.spanning_trees_count()

print(f"connected graphs on {n} vertices: {connected}")
print(f"spanning trees in total:          {total}")
print(f"arguments seen by the script:     {sys.argv[1:]}", file=sys.stderr)

if "--fail" in sys.argv:  # the exit code is part of the result
    print("failing on request", file=sys.stderr)
    sys.exit(3)
