![simple-sagemath-mcp](images/simple-sagemath-banner.png)

# simple-sagemath-mcp
An MCP server that gives an agent a persistent SageMath kernel — one warm Sage process, one
namespace, seven tools — and runs `.ipynb` files cell by cell, writing results (LaTeX and HTML
included) back to disk.

## Before you start: you need SageMath
This server does **not** bundle, download, or install SageMath. It drives a SageMath already on the
machine and cannot work without one; if you work with Sage, you almost certainly have it. Check with
`sage --version`.

Everything targets *the python of that Sage*: the server imports Sage's preparser
(`sage.repl.preparse`) to validate code, and the kernel it starts is that python, which imports
`sage.all`. So **`sage -pip` is the only pip that can install it**; `pipx`, `uvx`, and a plain `pip
install` into an unrelated venv cannot be used, because they build an environment on another python
where the Sage import fails.

## Install
Four routes, from fewest lines to most. They all end with a config block for your host.

**One line** — install from git with Sage's pip (works today):

```bash
sage -pip install git+https://github.com/JGCoelho/simple-sagemath-mcp
```

**Three lines** — clone, then install from the checkout; add `--upgrade` later:

```bash
git clone https://github.com/JGCoelho/simple-sagemath-mcp.git
cd simple-sagemath-mcp
sage -pip install .          # later: sage -pip install --upgrade .
```

**Four lines** — let the installer find Sage, install missing dependencies, and print the config
block ready to paste (it changes no config file):

```bash
sage --version
git clone https://github.com/JGCoelho/simple-sagemath-mcp.git
cd simple-sagemath-mcp
./install.sh
```

If `sage` is not on `PATH`, name it: `SAGE_BIN=/opt/sage/sage ./install.sh`.

**No install at all** — copy `server.py` to the machine and write the config by hand: `command` is
the python of your Sage and `args` is `["/path/to/server.py"]`, plus `"--directory",
"/path/to/your/work"` if you want one. The server needs only `fastmcp`, `nbformat`,
`jupyter_client`, `ipykernel`, and Sage's python.

The first two routes put a `simple-sagemath-mcp` console script next to the `sage` executable; it
calls `server:main`. On an externally managed (Debian/Ubuntu, PEP 668) `sagemath`, use `sage -pip
install --user .`, which puts the script in `~/.local/bin` instead. The PyPI name is reserved but not published: `sage -pip install
simple-sagemath-mcp` is future work, not a current command.

## Configure your host
Point the host at the **absolute path** of the console script, because many hosts run MCP servers
with a sanitized environment:

```json
{"mcpServers": {"sage": {
  "command": "/path/to/sageenv/bin/simple-sagemath-mcp",
  "args": ["--directory", "/path/to/your/work"]}}}
```

The install is one top-level module (`server.py`), not a package, so `python -m simple_sagemath_mcp`
does not exist. When the scripts directory is not on the host's `PATH`, the equivalent fallback is:

```json
{ "command": "/path/to/sageenv/bin/python3", "args": ["-m", "server"] }
```

`--directory` changes the server's working directory before the kernel starts, so a relative path in
the agent's code points at your work.

## Other hosts
The command is the same; only the file and the top-level key differ.

**Claude Desktop / Cursor** (`claude_desktop_config.json` or `.cursor/mcp.json`, key `mcpServers`) —
the JSON block above. **Gemini CLI** (`~/.gemini/settings.json`, key `mcpServers`) — the same block.

**Claude Code** (CLI):

```bash
claude mcp add sage -- /path/to/sageenv/bin/simple-sagemath-mcp --directory /path/to/your/work
```

**Codex** (`~/.codex/config.toml`):

```toml
[mcp_servers.sage]
command = "/path/to/sageenv/bin/simple-sagemath-mcp"
args = ["--directory", "/path/to/your/work"]
```

**VS Code** (`.vscode/mcp.json`, note `servers`):

```json
{"servers": {"sage": {"type": "stdio",
  "command": "/path/to/sageenv/bin/simple-sagemath-mcp",
  "args": ["--directory", "/path/to/your/work"]}}}
```

**Zed** (`settings.json`, note `context_servers` and `source`):

```json
{"context_servers": {"sage": {"source": "custom",
  "command": "/path/to/sageenv/bin/simple-sagemath-mcp",
  "args": ["--directory", "/path/to/your/work"]}}}
```

## What differs across hosts
- **Hidden `PATH`.** Use the absolute path to the console script (or `server.py`); optionally
  `"env": {"SAGE_BIN": "/path/to/sage"}`. The server also looks for `sage` next to the interpreter
  running it, so a console script in Sage's `bin` finds Sage with no extra config.
- **One kernel per server process.** Each session gets its own server and namespace; a restart, a
  crash, or a recycled process loses it, so write values to keep into a notebook cell (`nb_write`),
  not only variables.
- **Client-side timeouts.** Hosts often time out a call before the server's 2400 s budget. Split a
  long computation or use `interrupt`; a recorded `timeout` means the budget expired, not that the
  kernel stopped.
- **Remote-only hosts.** This is a stdio server that starts a local kernel; a remote-only host needs
  a local bridge.

## Tools
| Tool | Purpose |
|------|---------|
| `exec` | Run Sage code in the kernel namespace and return its output |
| `nb_run` | Restart the kernel, run a notebook's code cells, write the outputs |
| `nb_write` | Write a new notebook from cells and validate it |
| `nb_read` | Read cells and stored outputs from a notebook |
| `nb_edit` | Edit, add, or remove cells of an existing notebook atomically |
| `interrupt` | Stop the running computation; the kernel keeps its state |
| `restart` | Restart the kernel and wipe the namespace |

All seven return one envelope, with `error: null` on success (see **Errors**); the writing tools
also return `saved`, true when this call wrote the file.

### `exec(code, timeout=None)`
Returns `{"result", "stdout", "stderr", "truncated", "error"}`: `result` is the `repr()` of the
final expression (empty for statements or `None`), the streams are kept apart, and `truncated` means
the inline text was cut and is not recoverable. `timeout` defaults to 2400 s; on expiry the call
returns `timeout`, the kernel **keeps computing**, and the namespace is kept — call `interrupt` to
stop it. Sage syntax works: `3^2` is `9`, `1/2` is a rational, `R.<x> = PolynomialRing(QQ)` is a
ring.

### `nb_run(path)`
Validates first (JSON, schema, unique ids, no magics, every cell preparses); a failure returns
`invalid_notebook`, `unsupported_magic`, `invalid_source`, or `not_found` with `saved:false`, and
the file stays byte-identical. Then it **restarts the kernel**, runs cells in order with
`execution_count` continuing from the largest existing count, and stops at the first cell that
raises, writing outputs through that cell and returning `cell_execution_failed` with `saved:true`.
Otherwise it writes once and leaves the kernel warm.

A notebook with no code cell is not written (`saved:false`), and `nb_run` never creates a file.
Inline outputs are cut at 20000 characters **by whole output**; on disk nothing is truncated.
Results keep their mime bundles: `text/plain`, plus `text/latex` and `text/html` when the object
provides `_latex_()` / `_html_()`.

### `nb_write(path, cells, overwrite=false)`
Creates a notebook from `[{cell_type, source}]`, with the Sage kernelspec, at nbformat 4.5. Every
cell gets an id, returned in `cell_ids` ready for `nb_edit`. The whole notebook is validated before
the write. An existing file without `overwrite:true` returns `exists`; the parent directory must
exist.

### `nb_read(path, cell_ids=None)`
Reads from disk only: no write, no execution, no kernel. Without `cell_ids` it returns all cells in
order; with them, exactly those cells in the order given, and one unknown id fails the call with
`unknown_cell_id`. Each cell is `{cell_id, cell_type, source}`; code cells carry their full
`outputs`.

### `nb_edit(path, ops)`
Ops are `{"op": "edit", "cell_id", "source"}`, `{"op": "add", "source", "cell_type": "code",
"after"}`, and `{"op": "remove", "cell_id"}`. `add` defaults to code, appends when `after` is
omitted, and returns its new id in `applied`; `remove` may remove the only cell. The ops are applied
in memory and the **whole result** is re-validated before one write. On failure nothing is written:
`applied:[]`, `saved:false`, and `error.details` starts with `op <zero-based index> (<op>):`.

### `interrupt()`
Sends the interrupt request. `interrupted` is true when a computation may be running and the request
was sent, otherwise it is a no-op returning false. The namespace survives; the interrupted call
returns `cell_execution_failed` with `KeyboardInterrupt` in `details`. The flag is approximate right
after a `timeout`.

### `restart()`
Replaces the kernel. A running computation is interrupted first; if it does not stop within the
shutdown timeout, it is killed and respawned and the running call ends with `kernel_died`.
`restarted` is true only when a new, ready kernel runs.

## Errors
`error` is `{code, message, details}` on failure, `null` on success. `message` is one sentence; `details` holds a
traceback, a cell id, or an op index (or `null`); no domain failure is raised as an exception.

| Code | Meaning |
|------|---------|
| `invalid_source` | preparse, parse, or compile failed (`exec`, `nb_run`, `nb_write`, `nb_edit`) |
| `cell_execution_failed` | the code raised, including `KeyboardInterrupt` and a GAP abort (`exec`, `nb_run`) |
| `timeout` | the per-call budget expired; the kernel may still compute (`exec`) |
| `gap_crash_loop` | GAP crashed three times in one call; the call was interrupted (`exec`) |
| `kernel_died` | the kernel died; the namespace was lost, not restarted automatically (`exec`, `nb_run`, `restart`) |
| `sage_not_found` | no usable Sage from `SAGE_BIN`, `sage` on `PATH`, or the sibling `sage` |
| `not_found` | the notebook file does not exist (`nb_run`, `nb_read`, `nb_edit`) |
| `exists` | the target exists and `overwrite` is false (`nb_write`) |
| `invalid_notebook` | not JSON, not schema-valid, a bad cell id, or text that is not UTF-8 |
| `unsupported_magic` | a `%` or `!` line in a code cell (`nb_run`, `nb_write`, `nb_edit`) |
| `unknown_cell_id` | the id is not in the notebook (`nb_read`, `nb_edit`) |
| `invalid_op` | an op is malformed or impossible beyond an id lookup (`nb_edit`) |
| `write_failed` | the temp write or the rename failed; the file is untouched |
| `internal` | unexpected failure; `details` holds `Type: message` (all) |

## Limits
- No magics and no `!` shell escapes in notebook code cells; they are rejected with the offending
  cell id. One kernel and one lock, so one computation runs at a time.
- **This is not a sandbox.** The kernel runs the code the agent sends, with your user permissions
  and access to your files.

## Requirements
- SageMath 10.x (tested with 10.8 on Linux); the server module itself needs Python 3.10+.
- `fastmcp`, `nbformat`, `jupyter_client`, and `ipykernel` in the python of your Sage. `sage -pip
  install .` installs all four; `install.sh` installs the first two (`jupyter_client` and
  `ipykernel` come with Sage).

## Tests
Run from this directory with the python of your Sage:

```bash
PY=/path/to/sage/env/bin/python3
PATH="$(dirname "$PY"):$PATH" "$PY" -m unittest discover -s test -v
# or, without sage on PATH:
SAGE_BIN=/path/to/sage "$PY" -m unittest discover -s test -v
# a single file (test/ has no __init__.py):
PYTHONPATH=test "$PY" -m unittest test_packaging -v
# the one long test is opt-in:
SAGE_MCP_LONG=1 PATH="$(dirname "$PY"):$PATH" "$PY" test/test_long_exec.py -v
```

[SPEC.md](SPEC.md) describes the architecture, the concurrency model, the notebook rules, the
constants, and the test layout in detail.

## License
MIT. See [LICENSE](LICENSE).
