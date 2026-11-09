![simple-sagemath-mcp](images/simple-sagemath-banner.png)

# simple-sagemath-mcp
An MCP server that gives an agent a persistent SageMath kernel — one warm Sage process, one
namespace, five tools — and runs `.py` and `.sage` files in it. Its deprecated notebook tools, behind
a flag, run `.ipynb` files cell by cell and write results (LaTeX and HTML included) back to disk.

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
calls into the `sagemcp` package. On an externally managed (Debian/Ubuntu, PEP 668) `sagemath`, use
`sage -pip install --user .`, which puts the script in `~/.local/bin` instead. The third route
installs nothing: the config names the python of your Sage and the repo's `server.py`.

## Configure your host
Point the host at the **absolute path** of the console script, because many hosts run MCP servers
with a sanitized environment:

```json
{"mcpServers": {"sage": {
  "command": "/path/to/sageenv/bin/simple-sagemath-mcp",
  "args": ["--directory", "/path/to/your/work"]}}}
```

The code is the `sagemcp` package under `src/`. Both pip routes install it, and `server.py` at the
repo root is the entry point for a checkout (`python server.py`); installed, the equivalent of the
console script is `python -m sagemcp`. So when the scripts directory is not on the host's `PATH`, the
fallback is:

```json
{ "command": "/path/to/sageenv/bin/python3", "args": ["-m", "sagemcp"] }
```

`python -m server` also works, but only from the checkout, where the repo root is already on
`sys.path`; do not use it in a config for an installed copy.

`--directory` changes the server's working directory before the kernel starts, so a relative path in
the agent's code points at your work. `--legacy-notebook-tools` also serves the deprecated notebook
tools; see [Tools](#tools).

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
  crash, or a recycled process loses it, so keep code you want again in a `.py` or `.sage` file and
  run it with `run_file`, not only in variables.
- **Client-side timeouts.** Hosts often time out a call before the server's 2400 s budget. Pass a
  `timeout` below the host's limit: a call past it returns `status: running` with a `job_id`, the job
  keeps running, and `job_status` reports it until it ends. `interrupt` stops it.
- **Remote-only hosts.** This is a stdio server that starts a local kernel; a remote-only host needs
  a local bridge.

## Tools
| Tool | Purpose |
|------|---------|
| `exec` | Run Sage code in the kernel namespace and return its output |
| `run_file` | Run a `.py` or `.sage` file in the same namespace and return its output |
| `job_status` | Read a job that `exec` or `run_file` started: its status and output so far |
| `interrupt` | Stop the running computation; the kernel keeps its state |
| `restart` | Restart the kernel and wipe the namespace |

`exec` and `run_file` run as jobs. A call that outlasts its `timeout` (default 2400 s) returns
`status: running`, a `job_id`, `elapsed` and the output so far, and the job keeps the kernel. Poll
`job_status` with the id until it reports `done`, `error` or `interrupted`. While a job holds the
kernel, other calls return `kernel_busy`; they are not queued.

`run_file` takes a path to a `.py` or `.sage` file, and a relative path is resolved against
`--directory`. Only those two suffixes are accepted. The file runs in the same namespace as `exec`, so
its names stay for later calls.

**Deprecated: notebook tools.** `nb_run`, `nb_write`, `nb_read` and `nb_edit` work on `.ipynb` files.
They are not served by default. Start the server with `--legacy-notebook-tools` to get them, for
example `"args": ["--directory", "/path/to/your/work", "--legacy-notebook-tools"]`. Use `run_file` for
code.

Every tool returns one envelope, with `error: null` on success. The writing tools also return `saved`,
true when this call wrote the file. [SPEC.md](SPEC.md) §12 has the per-tool behaviour and the full
table of error codes.

## Limits
- No magics and no `!` shell escapes: `exec` and `run_file` reject them as `invalid_source`, and the
  deprecated notebook tools reject them with the offending cell id. One kernel and one lock, so one
  job runs at a time; a second call gets `kernel_busy`, it is not queued.
- **This is not a sandbox.** The kernel runs the code the agent sends, with your user permissions
  and access to your files.

## Requirements
- SageMath 10.x (tested with 10.8 on Linux); the server code itself needs Python 3.10+.
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
# one test file at a time (test/ has no __init__.py):
PYTHONPATH=test "$PY" -m unittest test_packaging -v
# the one long test is opt-in:
SAGE_MCP_LONG=1 PATH="$(dirname "$PY"):$PATH" "$PY" test/test_long_exec.py -v
```

[SPEC.md](SPEC.md) describes the architecture, the concurrency model, the notebook rules, the
constants, and the test layout in detail.

## License
MIT. See [LICENSE](LICENSE).
