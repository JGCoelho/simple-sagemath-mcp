# simple-sage-mcp

An MCP server that gives an agent a **persistent SageMath kernel** over stdio: one
long-lived Sage process it can run code in, interrupt, inspect and stop -- without
hanging the agent and without orphaning a kernel.

## Quickstart

SageMath must be installed and its own interpreter must be able to `import
sage.all` (`SAGE_PYTHON` overrides the choice). Then, from a checkout:

```bash
./install.sh
```

That finds a working interpreter, writes the `sage` entry into the `mcp.json` next
to the install target, and **proves the registration works** by spawning the
server and running `2+2` through it. Any other MCP client needs the block it
prints:

```json
{
  "mcpServers": {
    "sage": {
      "command": "/path/to/python-with-mcp/bin/python",
      "args": ["/path/to/sage-mcp/sage_mcp.py"],
      "env": {"SAGE_PYTHON": "/path/to/sage-env/bin/python", "SAGE_CWD": "/tmp"}
    }
  }
}
```

`pip install git+https://github.com/JGCoelho/simple-sagemath-mcp` works too. Verify
any registration at any time with `./install.sh --check` or
`simple-sage-mcp-check /path/to/python /path/to/sage_mcp.py`.

## Tools

| tool | params (required → `*`) | returns |
| --- | --- | --- |
| `exec` | `code`*, `session`="default", `timeout_s`=120 (0 = return at once), `preparse`=true | stdout, then the repr of the last expression; on timeout the partial output plus a job id to `poll` |
| `poll` | `job_id`*, `wait_s`=0, `since`=0 | new output since the offset, ending in `[job finished in 6.0s]` or `[still running 12s; …]` |
| `status` | — | kernel/session/job/client state, last error, log path |
| `interrupt` | `force`=false | `SIGINT` the running job (it fails with `KeyboardInterrupt`, variables are kept); `force=true` stops the kernel and loses them |
| `reset` / `drop` | `session`; `sessions`=[], `all`=false | empty a namespace; free idle sessions (refuses a busy one) |
| `run_file` | `path`*, `lang`="auto", `args`=[], `timeout_s`=120, `stdin`, `cwd` | exit code and duration, then stdout/stderr inline; a non-zero exit is an error carrying both |
| `notebook` | `action`*, `path`*, `out`, `in_place`, `cell_timeout_s`=120, `timeout_s` | `check` / `extract` / `clear` / `run`; advertised only when `nbformat`+`nbconvert`+`nbclient` import |

## Why `poll` exists

`exec`'s timeout **does not cancel anything**: a Gröbner basis or a graph
enumeration that takes twenty minutes is normal work for Sage, and giving up on
timeout throws that work away. So `exec` returns a job id and
`poll(job_id, since=N)` hands back the output produced since offset `N` --
incremental, not a re-run. The job survives the agent's turn, and even an MCP
restart (a new server adopts the surviving kernel); `wait_s` blocks instead of
looping. To cancel, do it explicitly: `interrupt`, then `poll` to collect the
`KeyboardInterrupt`. Silence is never read as "cancel".

## Why Sage, not Python

The kernel preparses by default, so this is **Sage's REPL**, not Python with
`sage.all` imported: `2^10` is 1024, `1/2` stays the rational `1/2`, `type(2)` is
`Integer`, and `R.<x> = QQ[]` works. A plain Python kernel (ipykernel with
`from sage.all import *`) silently gets all of these wrong or fails outright.

## Architecture

`sage_mcp.py` is the stdio front end, built on the official MCP Python SDK
(`MCPServer`, stdio transport) so schemas, argument validation and the protocol
come from the SDK rather than from hand-rolled JSON-RPC; `sage_client.py` talks to
the kernel over a private Unix socket (HMAC-authenticated, `0700` directory),
under a lock,
distinguishing *busy* (alive, not answering) from *gone* (never respawn a busy
kernel); `sage_kernel.py` runs jobs on the kernel's **main thread**, so
`cysignals`-based Sage (Singular, PARI, GAP) behaves, and refuses to let a job's
failure or an idle `SIGINT` take the kernel with it. Sessions share one kernel,
which stops when the last server exits. See [DESIGN.md](DESIGN.md) for the rest.

## Requirements

- Linux.
- **Two interpreters, on purpose.** The *server* is any Python 3.10+ with the
  `mcp` package (that is all it needs); the *kernel* is Sage's own interpreter,
  found at runtime by `SAGE_PYTHON`, then `sage -c 'import sys;print(sys.executable)'`,
  then the python beside `sage` on `PATH`. A Sage crash therefore cannot take the
  transport down, and `mcp` never has to be installed into the Sage environment.
- Any MCP client: one stdio server.
- `notebook` needs `nbformat`+`nbconvert`+`nbclient` in that same interpreter
  (they ship with SageMath).

## Configuration

`SAGE_PYTHON` (the interpreter used to spawn the kernel, which must import
`sage.all`), `SAGE_MCP_STATE` (`/tmp/sage-mcp-$UID`: socket and pid files,
`running.json`, `kernel.log`, created `0700`) and `SAGE_CWD` (working directory
for `exec` and `run_file`, default the server's cwd).

## Tests

```bash
# 84 checks over MCP stdio (official client), ~85 s
/path/to/sage-env/bin/python tests/acceptance.py

# 10 checks as a Sage script, ~10 s (`singular` must be on PATH)
PATH=/path/to/sage-env/bin:$PATH sage tests/interrupt_ownership.sage
```

`acceptance.py` checks the invariants that are easy to regress: busy-vs-dead,
spawn serialisation, stale state files, dead launchers, an honest stop, output
caps, `since` offsets, log isolation, shared-kernel ownership, socket privacy and
the authkey, adoption across a server restart, the three discovery routes, a
cancelled `exec` still being recoverable, the `run_file` contract, the notebook
cycle, and that a handler's stray `print` cannot reach the wire. `selftest.py` is
the 7-check version behind `install.sh --check`.

## License

MIT -- see `LICENSE`.
