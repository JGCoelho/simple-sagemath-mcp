![simple-sagemath-mcp](images/simple-sagemath-banner.png)

# simple-sagemath-mcp

An MCP server that gives an agent a **persistent SageMath kernel**: one warm
Sage process, one namespace, and Jupyter notebooks you can run cell by cell.

This is an experimental server. It is small on purpose: one file, seven tools,
no daemon, and no named namespaces.

The special point of this server is **native Jupyter notebook support**: it runs
notebook files cell by cell and writes the results back, with LaTeX and HTML
output in the cells.

## Install

```bash
git clone https://github.com/JGCoelho/simple-sagemath-mcp.git
cd simple-sagemath-mcp
./install.sh
```

You need SageMath 10.x (tested with 10.8). `install.sh` finds the `sage`
executable, derives the python that runs the server from it, installs `fastmcp`
and `nbformat` into that python if they are missing, and prints the config block
to paste. It changes no config file.

Add the block that it prints to the MCP config of your host, then restart the
host. For pi the file is `~/.pi/agent/mcp.json`. Claude uses `.mcp.json` or
`~/.claude.json`:

```json
{
  "mcpServers": {
    "sage": {
      "command": "/path/to/sage/env/bin/python3",
      "args": ["/path/to/simple-sagemath-mcp/server.py",
               "--directory", "/path/to/your/work"]
    }
  }
}
```

`--directory` changes the working directory of the server before it starts, so
a relative path in the code of the agent points at your work and not at the home
directory.

If `sage` is not on `PATH`, `install.sh` adds the resolved path to the block,
because the server resolves Sage from `SAGE_BIN` or from `PATH`:

```json
  "env": { "SAGE_BIN": "/opt/sage/sage" }
```

If the script cannot find Sage, give the executable yourself:

```bash
SAGE_BIN=/opt/sage/sage ./install.sh
```

A manual install is three steps:

```bash
PY=/path/to/sage/env/bin/python3       # the python of your Sage
"$PY" -m pip install fastmcp nbformat  # one time
"$PY" server.py                        # starts on stdio and waits for input
```

The tool list is read at connect time. Start the host again after a change.

## Why a persistent kernel

A new Sage process takes about 1.7 s and 200 MB before it can answer `2+2`.
Without a persistent kernel, an agent that loads a graph file, inspects it, and
then asks for its automorphism group must load the file three times.

This server starts one kernel on the first call, keeps it for the life of the
MCP connection, and keeps everything the agent defines in one namespace.
Variables, imports, and loaded data survive between calls until `restart` or
kernel death.

## Tools

| Tool | Purpose |
|------|---------|
| `exec` | Run Sage code in the kernel namespace and return its output |
| `nb_run` | Restart the kernel, run the code cells of a notebook, write the outputs into it, and return them |
| `nb_write` | Write a new notebook from cells and validate it |
| `nb_read` | Read cells and stored outputs from a notebook |
| `nb_edit` | Edit, add, or remove cells of an existing notebook atomically |
| `interrupt` | Stop the running computation. The kernel keeps its state |
| `restart` | Restart the kernel and wipe the namespace |

### `exec(code, timeout=None)`

Runs code in the one namespace and returns five fields:

```json
{"result": "9", "stdout": "out\n", "stderr": "", "truncated": false,
 "error": null}
```

- `result` — `repr()` of the value of the final expression, as text. Empty
  string when the source is only statements or the final value is `None`.
- `stdout` / `stderr` — what the code wrote on each stream. They stay apart;
  stderr is never folded into stdout.
- `truncated` — true if the inline text was cut. The untruncated text is **not
  recoverable**, so keep individual calls small.
- `error` — `null` on success, else the error object (see **Errors**).
- `timeout` — seconds for this call, default 2400. On expiry the call returns
  code `timeout`, the kernel **keeps computing**, and the namespace is kept.
  Call `interrupt` to stop it; `restart` is the hard way out. A second `exec`
  waits behind the kernel lock until that computation stops.

The inline budget is 20000 characters per call. `result` is charged first, then
`stdout`, then `stderr`; a field is cut only when the earlier ones left budget.
A cut field gets a `… [truncated N characters]` marker.

### `nb_run(path)`

Runs a notebook as a user would, then returns what it wrote.

1. Load and validate the file: JSON, nbformat schema, unique cell ids, no
   magics, every code cell preparses. A failure returns `invalid_notebook`,
   `unsupported_magic`, `invalid_source`, or `not_found` with `saved:false`;
   the file stays byte-identical and the kernel is **not** restarted.
2. Restart the kernel. Every variable from earlier `exec` or `nb_run` calls is
   gone.
3. Run the code cells in order. `execution_count` continues from the largest
   existing count + 1.
4. Stop at the first cell that raises. Write the file with outputs through that
   cell; later cells keep whatever was on disk. The call returns
   `cell_execution_failed`, `saved:true`, and the cells that ran.
5. Otherwise write the file once, return the cells with their outputs, and
   **leave the kernel running and warm** with the notebook's variables.

A notebook with no code cell is not written: `saved:false`, `truncated:false`,
`cells:[]`. `nb_run` never creates a file. If the kernel dies mid-run, the
outputs of the cells that already ran are still written (`saved:true`,
`kernel_died`); if a cell exceeds the kernel read budget, the call stops with
`cell_execution_failed` and keeps what that cell had already printed.

The inline outputs are cut at the 20000-character budget by whole outputs: the
first output that would exceed it clears that cell's and every later cell's
`outputs` and sets `truncated:true`. **On disk there is no truncation, ever.**
Use `nb_read` to recover the full outputs.

Each trailing value is published as a mime bundle. Sage objects give
`text/plain`, `text/latex` from `_latex_()`, and `text/html` from `_html_()`
when the object has these methods. Matrices, polynomials, and graphs therefore
show as real LaTeX in any notebook viewer.

### `nb_write(path, cells, overwrite=false)`

Creates a notebook from `[{cell_type, source}]` in the given order, with the
Sage kernelspec metadata, at nbformat 4.5. Every cell gets an id; the ids come
back in `cell_ids`, in order, and can go straight into `nb_edit`.

The whole notebook is validated before the write, so a magic or a syntax error
returns an error and leaves the target untouched. An existing file without
`overwrite:true` returns `exists`. The parent directory must already exist; it
is never created. `nb_write` never executes and never touches the namespace.

### `nb_read(path, cell_ids=None)`

Reads from disk and nothing else: it does not write, execute, or restart a
kernel. Without `cell_ids` it returns all cells in notebook order; with
`cell_ids` it returns exactly those cells in the order given, and one unknown id
fails the whole call with `unknown_cell_id` and the id in `details`.

Each cell is `{cell_id, cell_type, source}`; code cells also carry the full
`outputs` stored on disk. A notebook that is not UTF-8, or one whose cell text
cannot be sent as UTF-8 (a lone surrogate from a JSON `\ud800` escape), is
`invalid_notebook`: it cannot cross the protocol intact. `nbformat` and `nbformat_minor` are the values **on
disk**: a 4.4 file reports `4` and `4` even though its cells get in-memory ids
so they can be addressed.

### `nb_edit(path, ops)`

Applies ops in order to an existing notebook:

```json
[{"op": "edit", "cell_id": "a1b2c3d4", "source": "x = 2"},
 {"op": "add", "source": "x^2", "cell_type": "code", "after": "a1b2c3d4"},
 {"op": "remove", "cell_id": "e5f6a7b8"}]
```

`add` defaults to `cell_type: code`, appends when `after` is omitted, and
returns its new id in `applied`. `remove` may remove the only cell.

The ops are applied in memory and the **whole result** is re-validated, so a
pre-existing invalid cell blocks an edit that does not fix it. Only then is the
file written once, through a temp file in the same directory and `os.replace`.
On any failure nothing is written: `applied:[]`, `saved:false`, the file is
byte-identical, and `error.details` starts with `op <zero-based index> (<op>):`.
`nb_edit` never creates a file and never executes.

### `interrupt()`

Sends the kernel's interrupt request. `interrupted` is true when the kernel may
be computing and the request was sent; on an idle kernel with no call in flight
it is a no-op returning false. The namespace is untouched, so variables
survive; the interrupted call returns `cell_execution_failed` with
`KeyboardInterrupt` in `details`.

The server tracks one flag, not the kernel's real state, so there is one case
where `interrupted` is true for a computation that has already ended: after a
`timeout`, the abandoned computation may finish on its own, and the flag stays
true until a later call completes or the kernel is restarted. The interrupt
itself is harmless in that window.

### `restart()`

Replaces the kernel. The old namespace is gone and the new kernel is ready when
the call returns, so the next call starts clean. A running computation is
interrupted first; if it does not stop within the shutdown timeout, the kernel
is killed and respawned, and the running call ends with `kernel_died`.
`restarted` is true
only when a new, ready kernel is running; a resolution or start failure is
reported in `error`, never raised.

## Errors

Every tool returns the same envelope. `error` is `null` on success. On failure
it is an object with `code` (the class), `message` (one sentence), and
`details` (a traceback, the offending cell id, or the op index; or `null`).
The three writing tools also return `saved`: true means this call wrote the
file (a no-op edit still writes it). No domain failure is ever raised as an exception.

| Code | Meaning | Tools |
|------|---------|-------|
| `invalid_source` | preparse, parse, or compile failed | `exec`, `nb_run`, `nb_write`, `nb_edit` |
| `cell_execution_failed` | the code raised (includes `KeyboardInterrupt` and a GAP abort) | `exec`, `nb_run` |
| `timeout` | the per-call budget expired; the kernel may still compute, the namespace is kept | `exec` |
| `gap_crash_loop` | GAP crashed three times in one call; the call was interrupted | `exec` |
| `kernel_died` | the kernel died; the namespace was lost and is not restarted automatically | `exec`, `nb_run`, `restart` |
| `sage_not_found` | `SAGE_BIN` / `sage` on `PATH` did not resolve to a usable install | `exec`, `nb_run`, `restart` |
| `not_found` | the notebook file does not exist | `nb_run`, `nb_read`, `nb_edit` |
| `exists` | the target exists and `overwrite` is false | `nb_write` |
| `invalid_notebook` | not JSON, not schema-valid, or a cell id is missing or duplicated | `nb_run`, `nb_read`, `nb_edit`, `nb_write` |
| `unsupported_magic` | a `%` or `!` line in a code cell | `nb_run`, `nb_write`, `nb_edit` |
| `unknown_cell_id` | the id is not in the notebook (`cell_ids`, `edit`, `remove`, `after`) | `nb_read`, `nb_edit` |
| `invalid_op` | an op is malformed or impossible beyond an id lookup | `nb_edit` |
| `write_failed` | the temp write or `os.replace` failed; the file is untouched | `nb_run`, `nb_write`, `nb_edit` |
| `internal` | unexpected failure; `details` holds `Type: message` | all |

## Constants

The tuning values are hardcoded in `server.py` after the imports: the per-call
budget (2400 s), the heartbeat interval (15 s), the shell drain timeout (30 s),
the kernel ready timeout (60 s), the restart shutdown timeout (30 s), the inline
budget (20000 characters), and the GAP crash limit (3). There are no
environment overrides; MCP hosts sanitize the environment. `SAGE_BIN` is the one
exception: it names the Sage executable when `sage` is not on `PATH`.

## Architecture

The server drives the Jupyter kernel protocol with `jupyter_client`, not a
subprocess with a hand-made pipe. That gives clean streams, a real interrupt,
multi-line input, and the Jupyter notebook output format for free.

- **Resolving Sage.** `SAGE_BIN` wins over `which("sage")`. The kernel python is
  the sibling `python3`/`python` of the sage executable, or the answer of
  `sage -c "import sys; print(sys.executable)"`. It never falls back to the
  server's own interpreter: a kernel started by a non-Sage python silently lacks
  Sage. The kernel gets the sage `bin` directory first in `PATH`, so it finds
  `Singular`.
- **Kernel start.** One `KernelManager` with an injected `KernelSpec` runs that
  python with `-m ipykernel_launcher`. Nothing starts at connect time; the
  kernel starts on the first call, under one `asyncio.Lock`, so the first call
  pays about 2 s.
- **Single namespace.** The kernel holds one dict, `__mcp_ns__`, seeded with
  `from sage.all import *` when the kernel starts. Seeding there, not on the
  first call, matters: a SIGINT sent during that import is deferred by the
  import machinery, so a cold first call would ignore `interrupt` and force
  `restart` down its hard-kill path. After seeding, every computation is
  interruptible.
- **Server-side preparse.** Code goes through `sage.repl.preparse` and is then
  parsed and compiled in the server, so `^` is a power (`3^2 == 9`),
  `R.<x> = PolynomialRing(QQ)` works, and `1/2` is a rational. A syntax error
  is caught before the kernel sees the code. Notebook validation uses the same
  check, so a cell that passes validation fails at the same place when it runs.
- **One execution path.** `exec` and every notebook cell go through one
  function. Output messages are collected in order for the call's message id.
  A trailing expression is shown through `display(..., raw=True)` and becomes an
  `execute_result`, so a notebook gets a proper `Out[n]`.
- **One atomic writer.** `nb_write`, `nb_edit`, and `nb_run` all write through
  the same function: a temp file in the target directory, flush, `fsync`,
  `os.replace`.
- **One lock.** `exec` and `nb_run` hold it for the whole computation, and a
  heartbeat reports progress every 15 s so a long call keeps the connection.
  `interrupt` takes no lock, so it works while a call runs. `restart`
  interrupts, waits up to 30 s for the lock, then kills the kernel and starts a
  new one eagerly: a restart that cannot start a kernel fails at `restart` time,
  not later.
- **Kernel death.** The state is cleared during the failing call and the call
  returns `kernel_died` with "the namespace was lost". There is no automatic
  restart from the exec path, because a restart racing the shutdown is a bug.
  The next call, or `restart`, starts a fresh kernel.
- **GAP guard.** Three stderr lines that match `Gap crashed` or `Restarting Gap`
  in one call interrupt the kernel and return `gap_crash_loop` with the
  collected stderr.

## Tests

Run from this directory, with the python that has `fastmcp` (`install.sh`
prints it). The tests need either `sage` on `PATH` or `SAGE_BIN`:

```bash
PY=/path/to/sage/env/bin/python3     # the python that install.sh found
PATH="$(dirname "$PY"):$PATH" "$PY" -m unittest discover -s test -v
# or, without sage on PATH:
SAGE_BIN=/path/to/sage "$PY" -m unittest discover -s test -v
```

Each file should finish in under 30 s except the opt-in long test.

| File | Kernel | Covers |
|------|--------|--------|
| `test/_mcp.py` | — | Shared client: one server process per file plus sync and async call helpers |
| `test/test_sagebin.py` | no | `SAGE_BIN` over `PATH`, sibling python, `sage -c` fallback, the not-found message, the `PATH` prefix |
| `test/test_exec.py` | yes | Values, preparse, persistence, runtime and syntax errors, separate stdout/stderr, timeout then interrupt, restart while busy, interrupt idle/busy, truncation, GAP guard, output shape |
| `test/test_nb_edit.py` | no | Load and validate, `nb_write` ids/order/4.5/`exists`, `nb_read` selection and on-disk versions, `nb_edit` op order, atomicity and hash-equality on failure |
| `test/test_nb_run.py` | yes | Restart first, disk and inline outputs, stop at the first error with `saved:true`, validation failure without restart or write, zero code cells, inline-only truncation, return shape |
| `test/test_long_exec.py` | yes | Opt-in: a ~45 s `exec` returns its exact result, so the call survives without a disconnect |

The long test is skipped unless you ask for it:

```bash
SAGE_MCP_LONG=1 PATH="$(dirname "$PY"):$PATH" "$PY" test/test_long_exec.py -v
```

Each file runs directly (`"$PY" test/test_exec.py -v`), and `test/` has no
`__init__.py`, so a single test uses the file name as the module:

```bash
PYTHONPATH=test "$PY" -m unittest test_exec.TestExec.test_value -v
```

The files use `unittest`, so `pytest test/` also works.

## Requirements

- SageMath 10.x. Tested with Sage 10.8 on Linux.
- `fastmcp` and `nbformat`, installed into the python of your Sage
  (`install.sh` does this).
- `jupyter_client` and `ipykernel`, which are part of the Sage environment.
- Python 3.10 or newer.

## Limits

- No magics and no `!` shell escapes in notebook code cells. They are rejected
  with the offending cell id.
- The kernel has one process and one lock, so one computation runs at a time
  and an interrupt stops that computation. That follows from the one-kernel
  design.

**This is not a sandbox.** The kernel runs the code that the agent sends, with
your user permissions and access to your files. Use a container or a separate
user if you do not trust the agent.
