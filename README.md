![simple-sagemath-mcp](images/simple-sagemath-banner.png)

# simple-sagemath-mcp

An MCP server that gives an agent a **persistent SageMath kernel**: one warm
Sage process, many named namespaces, and Jupyter notebooks you can run cell by
cell.

This is an experimental server. It is small on purpose: one file, six tools,
no daemon.

## Install

```bash
git clone https://github.com/JGCoelho/simple-sagemath-mcp.git
cd simple-sagemath-mcp
./install.sh
```

You need SageMath 10.x. `install.sh` finds the python of your Sage, installs
`fastmcp` and `nbformat` there, and prints the config block to paste.

Add the block that it prints to the MCP config of your host, then restart the
host. For pi the file is `~/.pi/agent/mcp.json`. Claude uses `.mcp.json` or
`~/.claude.json`:

```json
{
  "mcpServers": {
    "sage": {
      "command": "/path/to/sage/env/bin/python",
      "args": ["/path/to/simple-sagemath-mcp/server.py"]
    }
  }
}
```

If the script cannot find Sage, give the interpreter yourself:

```bash
SAGE_PYTHON=/opt/sage/local/bin/python3 ./install.sh
```

A manual install is three steps:

```bash
PY=/path/to/sage/env/bin/python          # the python of your Sage
"$PY" -m pip install fastmcp nbformat   # one time
"$PY" server.py                         # starts on stdio and waits for input
```

`SAGE_PYTHON` also lets the server itself run on a different python. The server
then starts the kernel with the python in `SAGE_PYTHON`. `server.py` has a
PEP 723 header, so `uv` can run it with no install step:

```bash
SAGE_PYTHON=/path/to/sage/python uv run server.py
```

The tool list is read at connect time. Start the host again after a change.

## Why a persistent kernel

A new Sage process takes about 1.7 s and 200 MB before it can answer `2+2`.
Without a persistent kernel, an agent that loads a graph file, inspects it, and
then asks for its automorphism group must load the file three times.

This server starts one kernel on the first call, keeps it, and gives each
session name its own namespace. Variables, imports, and loaded data survive
between calls.

## Tools

| Tool | Purpose |
|------|---------|
| `sage_exec` | Run Sage code in a session namespace |
| `sage_exec_notebook` | Run the code cells of a notebook and write the outputs into it |
| `sage_write_notebook` | Write a Sage notebook, validate it, and optionally run it |
| `sage_interrupt` | Stop the running computation. The kernel keeps its state |
| `sage_list` | Show the session names that the server has used |
| `sage_stop` | Remove a session and free its variables |

### `sage_exec(code, session="default", timeout=None)`

Runs code and returns four fields:

```json
{"stdout": "out\n", "stderr": "", "result": "9", "error": null}
```

- `stdout` / `stderr` — what the code printed on each stream. They stay apart.
- `result` — the value of the last expression, as text. `null` when the last
  statement is not an expression.
- `error` — `"ZeroDivisionError: rational division by zero"`, or `null`.
- `timeout` — seconds for this call. The default is `kernel_read_timeout` from
  `server.json`. On a timeout the call fails and the variables stay.

Use the same `session` name in each call to keep the same variables. Different
names cannot see each other.

### `sage_exec_notebook(path, session="default", cells=None)`

Runs the code cell in order, in one namespace, and **writes the outputs into the
notebook file**. The result looks as if a user ran the notebook: stream output,
`Out[n]` values with `execution_count`, and error outputs with the traceback.

```json
{"path": "find.ipynb",
 "cells": [{"cell": 0, "stdout": "", "stderr": "", "result": null, "error": null},
           {"cell": 1, "stdout": "42\n", "stderr": "", "result": null, "error": null}]}
```

- `cells` — run only these cell indexes, for example `[0, 2]`. Empty means all
  code cells. Markdown cells are never run and never changed.
- The run stops at the first error. The report names the cell and the error.
- The file is written through a temp file, so a failed write cannot break it.

### `sage_write_notebook(path, cells, run=false, session="default", overwrite=false)`

Writes a notebook from a list of code strings. One string is one cell. The
notebook gets the `sagemath` kernelspec metadata, is checked with
`nbformat.validate`, and is read back from disk after the write. With `run=true`
it is also executed.

```json
{"path": "find.ipynb", "valid": true, "cells": [ ... ]}
```

The tool does not replace an existing file unless you set `overwrite=true`.

### `sage_interrupt()`

Sends Ctrl+C to the kernel. The running call fails with `KeyboardInterrupt` and
the session keeps its variables — the first thing to try for a runaway loop.

The kernel has one process, so the interrupt is kernel-wide: it also stops work
in other sessions. That follows from the one-kernel design.

### `sage_list()`

Returns the session names that this server has used:

```json
{"sessions": ["default", "find"]}
```

### `sage_stop(session="default")`

Removes a session and its variables from the kernel, and returns
`{"dropped": "find"}`. Use it to free memory after a task. An unknown name
returns `{"error": "no such session: find"}`.

## Notebook support

```python
# cell 0
var('x')

# cell 1
f = x^3 - 2*x + 1

# cell 2
f
```

After `sage_exec_notebook("nb.ipynb")` the file holds

```
cell 2   execute_result   execution_count=3
         text/plain: "x^3 - 2*x + 1"
         text/latex: "$x^{3} - 2 \, x + 1$"
```

Each trailing value is published as a mime bundle. Sage objects give
`text/plain`, `text/latex` from `_latex_()`, and `text/html` from `_html_()`
when the object has these methods. Matrices, polynomials, and graphs therefore
show as real LaTeX in any notebook viewer.

Two rules:

- IPython magics (`%matplotlib inline`, `%%time`, `!ls`) are **rejected** with
  an error that names the cell. This kernel runs plain Sage code.
- A cell that raises stores an `error` output and stops the run.

## Architecture

The server drives the Jupyter kernel protocol with `jupyter_client`, not a
subprocess with a hand-made pipe. That gives clean streams, a real interrupt,
multi-line input, and the Jupyter notebook output format for free.

- **Kernel start.** One `KernelManager` with an injected `KernelSpec` runs
  `SAGE_PYTHON` (or `sys.executable`) with `-m ipykernel_launcher`. The `bin`
  directory of that interpreter goes first in `PATH`, so Sage finds `Singular`
  and other helper programs.
- **Lazy start.** Nothing starts at connect time. The kernel starts on the first
  call, under one `asyncio.Lock`. Connect takes about 2 s; the first call pays
  the kernel start.
- **Sessions.** Each session name is one dict in the kernel: `globals()[
  "__mcp_sage_ns__" + name]`. The dict gets `from sage.all import *` on first
  use. Do not use the MCP session id for this: with the default protocol it
  changes on each call.
- **Preparse.** Code goes through `sage.repl.preparse`, so `^` is a power
  (`3^2 == 9`), `R.<x> = PolynomialRing(QQ)` works, and `1/2` is a rational.
  This is the main correctness difference from servers that run Sage code as
  plain Python.
- **Output capture.** The kernel messages are collected in order. A trailing
  expression is shown through `display(..., raw=True)`; the collector turns that
  message into an `execute_result`, so a notebook cell gets a proper `Out[n]`.
- **One kernel.** All calls are serialized by one lock. A 3 s job in one session
  delays a call in another session. This is deliberate: one kernel uses 200 MB,
  and the target machine has about 3 GB free.
- **Failure policy.** A timeout (`timeout` on the call, or 2400 s by default)
  raises `TimeoutError` and **keeps** the variables. A dead kernel is detected
  during the run (the read waits in 5 s slices and checks `is_alive()`), the
  kernel restarts, and the error says that all sessions were lost. Three lines
  that match `Gap crashed` on stderr stop the run with a `GapCrashLoop` error,
  because GAP can start processes without limit.

## Configuration

Defaults live in `server.json` next to `server.py`:

| key | default | meaning |
|-----|---------|---------|
| `kernel_read_timeout` | 2400 | seconds to wait for one call |
| `heartbeat_interval` | 15 | seconds between progress messages |
| `shell_drain_timeout` | 30 | seconds to drain the shell channel |
| `kernel_ready_timeout` | 60 | seconds to wait for a new kernel |

`SAGE_MCP_<KEY>` environment variables override the file, for example
`SAGE_MCP_KERNEL_READ_TIMEOUT=60`. Note that most MCP clients start the server
with a **sanitized environment**, so these variables often do not arrive. Use
`server.json` when you need a different value.

## Requirements

- SageMath 10.x. Run `./install.sh`, or set `SAGE_PYTHON` to the python of your
  Sage install.
- `fastmcp` and `nbformat`, installed into that same python (`install.sh` does
  this).
- `jupyter_client` and `ipykernel`, which are part of the Sage environment.
- Python 3.10 or newer.

## Tests

Run from this directory, with the python that has `fastmcp` (`install.sh`
prints it). The tests share one client and one kernel per file, so the whole
folder takes about 25 s.

```bash
PY=/path/to/sage/env/bin/python     # the python that install.sh found
"$PY" test/test_config.py          # config only, no kernel, 6 tests
"$PY" test/test_exec.py            # sage_exec and the session tools, 14 tests
"$PY" test/test_notebook.py        # notebooks, 8 tests
```

The files use `unittest`, so `pytest test/` also works.

## Limits

- No magics, no `!` shell commands.
- A session stays in the kernel until `sage_stop` or the end of the server. Many
  different session names grow the kernel memory.
- An interrupt that arrives just after a timeout can hit the next call.
- `sage_exec` gives the plain text value only. LaTeX and HTML are for notebooks.
- Stored error outputs keep the ANSI colours of the kernel traceback.

## Inspiration

The tool surface follows [szeider/mcp-sage](https://github.com/szeider/mcp-sage)
(`sage_exec`, `sage_interrupt`, Jupyter kernel protocol, injected `KernelSpec`,
and the GAP crash-loop guard). The differences are: Sage preparse on every call,
one kernel with named namespaces instead of one kernel per session, and native
notebook support.
