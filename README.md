![simple-sagemath-mcp](images/simple-sagemath-banner.png)

# simple-sagemath-mcp
An MCP server that gives an agent a persistent SageMath kernel — one warm Sage process, one
namespace, five tools — and runs `.py` and `.sage` files in it.

## Before you start: you need SageMath
This server does **not** bundle, download, or install SageMath. It drives a SageMath already on the
machine and cannot work without one; _if you work with Sage, you almost certainly have it._ Check
with `sage --version`; what actually matters is that Sage's python can `import sage.all`.

Everything targets *the python of that Sage*: the server imports Sage's preparser
(`sage.repl.preparse`) to validate code, and the kernel it starts is that python, which imports
`sage.all`. So **`sage -pip` is the only pip that can install it**; `pipx`, `uvx`, and a plain `pip
install` into an unrelated venv _cannot_ be used: they build an environment on another python, where
the Sage import fails.

## Install
Three routes, from fewest lines to most; each ends with a config block for your host.

**One line** — install from git with Sage's pip:

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

If `sage` is not on `PATH`, name it: `SAGE_BIN=/opt/sage/sage ./install.sh`. On an externally managed
(Debian/Ubuntu, PEP 668) `sagemath`, use `sage -pip install --user .`, which puts the console script
in `~/.local/bin` instead of next to `sage`. The third route installs nothing: the config names the
python of your Sage and the repo's `server.py`. Stuck? Paste this README to your coding agent and ask
it to install the server and write the config.

## Configure your host
Point the host at the **absolute path** of the console script, because many hosts run MCP servers
with a sanitized environment:

```json
{"mcpServers": {"sage": {
  "command": "/path/to/sageenv/bin/simple-sagemath-mcp",
  "args": ["--directory", "/path/to/your/work"]}}}
```

The code is the `sagemcp` package under `src/`; both pip routes install it and create that script.
When the scripts directory is not on the host's `PATH`, name the interpreter instead:

```json
{ "command": "/path/to/sageenv/bin/python3", "args": ["-m", "sagemcp"] }
```

From a checkout, name `server.py` itself (`"args": ["/path/to/repo/server.py"]`); `python -m server`
works there too. `--directory` changes the server's working directory before the kernel starts, so a
relative path in the agent's code points at your work.

## Other hosts
Same command; only the file and the top-level key differ. **Codex** (`~/.codex/config.toml`):

```toml
[mcp_servers.sage]
command = "/path/to/sageenv/bin/simple-sagemath-mcp"
args = ["--directory", "/path/to/your/work"]
```

**VS Code** (`.vscode/mcp.json`, note `servers`):

```json
{"servers": {"sage": {"type": "stdio", "command": "/path/to/sageenv/bin/simple-sagemath-mcp", "args": ["--directory", "/path/to/your/work"]}}}
```

## What differs across hosts
- **Hidden `PATH`.** Use the absolute path to the console script (or `server.py`); optionally
  `"env": {"SAGE_BIN": "/path/to/sage"}`. The server also looks for `sage` next to the interpreter
  running it, so a script in Sage's `bin` finds Sage unaided.
- **One kernel per server process.** Each session gets its own namespace; a restart, a crash, or a
  recycled process loses it, so keep code you want again in a `.py` or `.sage` file and run it with
  `run_file`, not only in variables.
- **Client-side timeouts.** Hosts often time out a call before the default 2400 s wait. Pass a
  `timeout` below the host's limit: the call returns `status: running` with a `job_id`, the job keeps
  running, and `job_status` reports it until it ends.
- **Remote-only hosts** need a local bridge: this is stdio, and it starts a local kernel.

## Tools
| Tool | Purpose |
|------|---------|
| `exec` | Run Sage code in the kernel namespace and return its output |
| `run_file` | Run a `.py` or `.sage` file in the same namespace and return its output |
| `job_status` | Read a job that `exec` or `run_file` started: its status and output so far |
| `interrupt` | Stop the running computation; the kernel keeps its state |
| `restart` | Restart the kernel and wipe the namespace |

`exec` and `run_file` run as jobs. A call that outlasts its `timeout` (default 2400 s) returns
`status: running`, a `job_id`, `elapsed` and the output so far, and the job keeps the kernel. The
timeout bounds the wait, not the job: nothing stops a job for running long. Poll `job_status` with the
id until it reports `done`, `error` or `interrupted`. While a job holds the kernel, other calls return
`kernel_busy`; they are not queued. The server keeps the running job and the latest finished job; an
older id returns `unknown_job`, and a restart drops every record.

`interrupt` keeps the namespace; the job it stopped ends with `execution_failed` and
`KeyboardInterrupt`. `restart` ends a running job as `interrupted`, with `kernel_died` as its error.
`run_file` resolves a relative path against `--directory` and accepts only those two suffixes. It
reads the file once, as UTF-8 with a leading byte-order mark dropped; other bytes are
`invalid_source`, and a traceback names the file.

Every tool returns one envelope, with `error: null` on success. [SPEC.md](SPEC.md) §12 has the
per-tool behaviour and the full table of error codes.

## Limits
- No magics and no `!` shell escapes: `exec` and `run_file` reject them as `invalid_source`. One
  kernel and one lock, so one job runs at a time; a second call gets `kernel_busy`, it is not queued.
- **This is not a sandbox.** The kernel runs the code the agent sends, with your user permissions
  and access to your files.

## Requirements
- SageMath 10.x (tested with 10.8 on Linux); the server code itself needs Python 3.10+.
- `fastmcp`, `jupyter_client`, and `ipykernel` in the python of your Sage. The pip routes install all
  three; `install.sh` installs `fastmcp` (`jupyter_client` and `ipykernel` come with Sage).

## Tests
Run from this directory with the python of your Sage:

```bash
PY=/path/to/sage/env/bin/python3
PATH="$(dirname "$PY"):$PATH" "$PY" -m unittest discover -s test -v
SAGE_BIN=/path/to/sage "$PY" -m unittest discover -s test -v   # sage not on PATH
PYTHONPATH=test "$PY" -m unittest test_packaging -v             # one file (no __init__.py)
SAGE_MCP_LONG=1 PATH="$(dirname "$PY"):$PATH" "$PY" test/test_long_exec.py -v  # opt-in
```

[SPEC.md](SPEC.md) covers the architecture, the concurrency model, the constants and the test layout.

## License
MIT. See [LICENSE](LICENSE).
