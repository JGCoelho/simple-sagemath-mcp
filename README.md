![simple-sagemath-mcp](images/simple-sagemath-banner.png)

# simple-sagemath-mcp
An MCP server that gives an agent a persistent SageMath kernel — one warm Sage process, one
namespace, seven tools — and runs `.ipynb` files cell by cell, writing results (LaTeX and HTML
included) back to disk.

## Before you start: you need SageMath
This server does **not** bundle, download, or install SageMath. It drives a SageMath already on the
machine and cannot work without one; _if you work with Sage, you almost certainly have it._
Check with `sage --version`.

Everything targets *the python of that Sage*: the server imports Sage's preparser
(`sage.repl.preparse`) to validate code, and the kernel it starts is that python, which imports
`sage.all`. So **`sage -pip` is the only pip that can install it**; `pipx`, `uvx`, and a plain `pip
install` into an unrelated venv _cannot_ be used, because they build an environment on another python
where the Sage import fails.

## Install
Three routes, from fewest lines to most. They all end with a config block for your host.

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

**Four lines, no pip** — let the script in this repo do the checks: it finds Sage, installs the
dependencies it is missing, and prints the block ready to paste. It installs no package and changes
no config file.

```bash
sage --version
git clone https://github.com/JGCoelho/simple-sagemath-mcp.git
cd simple-sagemath-mcp
./install.sh
```

If `sage` is not on `PATH`, name it: `SAGE_BIN=/opt/sage/sage ./install.sh`.

_**If any of this gives you trouble, hand it over: paste this README to your coding agent and ask it
to install the server and write the config.**_ It can run the commands, find the python of your Sage,
and check that the host starts the server.

Note: The two pip routes put a `simple-sagemath-mcp` console script next to the `sage` executable; it
calls `server:main`. On an externally managed (Debian/Ubuntu, PEP 668) `sagemath`, use `sage -pip
install --user .`, which puts the script in `~/.local/bin` instead. The third route needs no
package at all: the config names the python of your Sage and `server.py`.

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

All seven return one envelope, with `error: null` on success; the writing tools also return
`saved`, true when this call wrote the file. [SPEC.md](SPEC.md) §12 has the per-tool behaviour and
the full table of error codes.

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
