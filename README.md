![simple-sagemath-mcp](images/simple-sagemath-banner.png)

# simple-sagemath-mcp

An MCP server that gives an agent a **persistent SageMath kernel**: one warm Sage
process, one namespace, and Jupyter notebooks you can run cell by cell.

It is small on purpose: one file, seven tools, no daemon, no named sessions. The
special point is **native notebook support** — it runs `.ipynb` files cell by
cell and writes the results back, LaTeX and HTML included.

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

Add the block it prints to the MCP config of your host, then restart the host.
For pi that is `~/.pi/agent/mcp.json`; Claude uses `.mcp.json` or
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

`--directory` changes the working directory of the server before it starts, so a
relative path in the agent's code points at your work and not at your home
directory.

If `sage` is not on `PATH`, `install.sh` adds the resolved path to the block,
because the server looks for Sage in `SAGE_BIN` first and then on `PATH`:

```json
  "env": { "SAGE_BIN": "/opt/sage/sage" }
```

If the script cannot find Sage, name the executable yourself:
`SAGE_BIN=/opt/sage/sage ./install.sh`.

A manual install is three steps:

```bash
PY=/path/to/sage/env/bin/python3       # the python of your Sage
"$PY" -m pip install fastmcp nbformat  # one time
"$PY" server.py                        # starts on stdio and waits for input
```

The tool list is read at connect time. Start the host again after a change.

## Why a persistent kernel

A fresh Sage process takes about 1.7 s and 200 MB before it can answer `2+2`.
Without a persistent kernel, an agent that loads a graph, inspects it, and then
asks for its automorphism group loads the file three times.

So this server starts one kernel on the first call, keeps it for the life of the
MCP connection, and keeps everything the agent defines in one namespace.
Variables, imports, and loaded data survive between calls until `restart` or
kernel death.

## Tools

| Tool | Purpose |
|------|---------|
| `exec` | Run Sage code in the kernel namespace and return its output |
| `nb_run` | Restart the kernel, run the code cells of a notebook, write the outputs into it |
| `nb_write` | Write a new notebook from cells and validate it |
| `nb_read` | Read cells and stored outputs from a notebook |
| `nb_edit` | Edit, add, or remove cells of an existing notebook atomically |
| `interrupt` | Stop the running computation. The kernel keeps its state |
| `restart` | Restart the kernel and wipe the namespace |

All seven return the same envelope, with `error: null` on success (see
**Errors**). The three writing tools also return `saved`: true means this call
wrote the file.

### `exec(code, timeout=None)`

```json
{"result": "9", "stdout": "out\n", "stderr": "", "truncated": false,
 "error": null}
```

- `result` — `repr()` of the final expression, as text; empty when the source is
  only statements or the value is `None`.
- `stdout` / `stderr` — what the code wrote on each stream, kept apart.
- `truncated` — the inline text was cut. The full text is **not recoverable**, so
  keep individual calls small.
- `timeout` — seconds for this call, default 2400. On expiry the call returns
  `timeout`, the kernel **keeps computing**, and the namespace is kept. Call
  `interrupt` to stop it, `restart` for the hard way out; a second `exec` waits
  behind the kernel until that computation stops.

Sage syntax works: `3^2` is `9`, `1/2` is a rational, `R.<x> = PolynomialRing(QQ)`
is a polynomial ring.

### `nb_run(path)`

Runs a notebook the way a user would.

1. Load and validate the file: JSON, nbformat schema, unique cell ids, no
   magics, every code cell preparses. A failure returns `invalid_notebook`,
   `unsupported_magic`, `invalid_source`, or `not_found` with `saved:false`; the
   file stays byte-identical and the kernel is **not** restarted.
2. Restart the kernel. Variables from earlier `exec` or `nb_run` calls are gone.
3. Run the code cells in order, with `execution_count` continuing from the
   largest existing count.
4. Stop at the first cell that raises, write the outputs through that cell, and
   return `cell_execution_failed` with `saved:true` and the cells that ran.
5. Otherwise write once, return the cells with their outputs, and **leave the
   kernel running and warm** with the notebook's variables.

A notebook with no code cell is not written (`saved:false`), and `nb_run` never
creates a file. If the kernel dies mid-run, the cells that already ran are still
written. Inline outputs are cut at 20000 characters **by whole output**; on disk
nothing is ever truncated, so `nb_read` recovers the full outputs.

Results keep their mime bundles: `text/plain`, plus `text/latex` from `_latex_()`
and `text/html` from `_html_()` when the object has them — matrices, polynomials,
and graphs render as real LaTeX in any notebook viewer.

### `nb_write(path, cells, overwrite=false)`

Creates a notebook from `[{cell_type, source}]` in the given order, with the Sage
kernelspec, at nbformat 4.5. Every cell gets an id; the ids come back in
`cell_ids` in order, ready for `nb_edit`. The whole notebook is validated before
the write, so a magic or a syntax error leaves the target untouched. An existing
file without `overwrite:true` returns `exists`. The parent directory must already
exist; it is never created. `nb_write` never executes and never touches the
namespace.

Use it to **document your work**: a notebook is the artifact another agent — or
you after a restart — can read with `nb_read`, rerun with `nb_run`, and pick up
from, so the steps and the values that matter do not live only in the chat.

### `nb_read(path, cell_ids=None)`

Reads from disk and nothing else: no write, no execution, no kernel. Without
`cell_ids` it returns all cells in notebook order; with `cell_ids`, exactly those
cells in the order given, and one unknown id fails the whole call with
`unknown_cell_id` and the id in `details`.

Each cell is `{cell_id, cell_type, source}`; code cells also carry the full
`outputs` stored on disk. `nbformat` and `nbformat_minor` are the values **on
disk**, so a 4.4 file reports `4` and `4`. A notebook that is not UTF-8, or whose
cell text cannot be sent as UTF-8, is `invalid_notebook`.

### `nb_edit(path, ops)`

```json
[{"op": "edit", "cell_id": "a1b2c3d4", "source": "x = 2"},
 {"op": "add", "source": "x^2", "cell_type": "code", "after": "a1b2c3d4"},
 {"op": "remove", "cell_id": "e5f6a7b8"}]
```

`add` defaults to `cell_type: code`, appends when `after` is omitted, and returns
its new id in `applied`. `remove` may remove the only cell.

The ops are applied in memory and the **whole result** is re-validated, so a
pre-existing invalid cell blocks an edit that does not fix it. Only then is the
file written, once. On any failure nothing is written: `applied:[]`,
`saved:false`, the file is byte-identical, and `error.details` starts with
`op <zero-based index> (<op>):`. `nb_edit` never creates a file and never
executes.

### `interrupt()`

Sends the kernel's interrupt request. `interrupted` is true when the kernel may
be computing and the request was sent; otherwise it is a no-op returning false.
The namespace is untouched, so variables survive; the interrupted call returns
`cell_execution_failed` with `KeyboardInterrupt` in `details`.

One caveat: the server tracks one flag, not the kernel's real state, so right
after a `timeout` it can report `interrupted:true` for a computation that has
already ended (the interrupt is harmless there). It clears on the next completed
call or a restart.

### `restart()`

Replaces the kernel. The old namespace is gone and the new kernel is ready when
the call returns, so the next call starts clean. A running computation is
interrupted first; if it does not stop within the shutdown timeout, the kernel is
killed and respawned, and the running call ends with `kernel_died`. `restarted` is
true only when a new, ready kernel is running; a failure is reported in `error`,
never raised.

## Errors

`error` is `null` on success. On failure it is `{code, message, details}`:
`message` is one sentence and `details` holds a traceback, the offending cell id,
or the op index (or `null`). No domain failure is ever raised as an exception.

| Code | Meaning | Tools |
|------|---------|-------|
| `invalid_source` | preparse, parse, or compile failed | `exec`, `nb_run`, `nb_write`, `nb_edit` |
| `cell_execution_failed` | the code raised (includes `KeyboardInterrupt` and a GAP abort) | `exec`, `nb_run` |
| `timeout` | the per-call budget expired; the kernel may still compute | `exec` |
| `gap_crash_loop` | GAP crashed three times in one call; the call was interrupted | `exec` |
| `kernel_died` | the kernel died; the namespace was lost and is not restarted automatically | `exec`, `nb_run`, `restart` |
| `sage_not_found` | `SAGE_BIN` / `sage` on `PATH` did not resolve to a usable install | `exec`, `nb_run`, `restart` |
| `not_found` | the notebook file does not exist | `nb_run`, `nb_read`, `nb_edit` |
| `exists` | the target exists and `overwrite` is false | `nb_write` |
| `invalid_notebook` | not JSON, not schema-valid, a bad cell id, or text that is not UTF-8 | `nb_run`, `nb_read`, `nb_edit`, `nb_write` |
| `unsupported_magic` | a `%` or `!` line in a code cell | `nb_run`, `nb_write`, `nb_edit` |
| `unknown_cell_id` | the id is not in the notebook | `nb_read`, `nb_edit` |
| `invalid_op` | an op is malformed or impossible beyond an id lookup | `nb_edit` |
| `write_failed` | the temp write or the rename failed; the file is untouched | `nb_run`, `nb_write`, `nb_edit` |
| `internal` | unexpected failure; `details` holds `Type: message` | all |

## Limits

- No magics and no `!` shell escapes in notebook code cells; they are rejected
  with the offending cell id.
- One kernel and one lock, so one computation runs at a time. That follows from
  the one-kernel design.
- **This is not a sandbox.** The kernel runs the code the agent sends, with your
  user permissions and access to your files. Use a container or a separate user
  if you do not trust the agent.

## Requirements

- SageMath 10.x. Tested with Sage 10.8 on Linux.
- `fastmcp` and `nbformat` in the python of your Sage (`install.sh` does this).
- `jupyter_client` and `ipykernel`, which are part of the Sage environment.
- Python 3.10 or newer.

## Tests

Run from this directory with the python that `install.sh` printed:

```bash
PY=/path/to/sage/env/bin/python3
PATH="$(dirname "$PY"):$PATH" "$PY" -m unittest discover -s test -v
# or, without sage on PATH:
SAGE_BIN=/path/to/sage "$PY" -m unittest discover -s test -v
```

A single test uses its file name as the module, since `test/` has no
`__init__.py`:

```bash
PYTHONPATH=test "$PY" -m unittest test_exec.TestExec.test_value -v
```

Each file finishes in under 30 s. The one long test is opt-in:

```bash
SAGE_MCP_LONG=1 PATH="$(dirname "$PY"):$PATH" "$PY" test/test_long_exec.py -v
```

[SPEC.md](SPEC.md) describes the architecture, the concurrency model, the
notebook format rules, the constants, and the test layout in detail.

## License

MIT. See [LICENSE](LICENSE).
