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
to the install target (if there is one), and then **proves the registration
works** by spawning the server and running `2+2` through it. Any other MCP client
just needs the block it prints:

```json
{
  "mcpServers": {
    "sage": {
      "command": "/path/to/sage-env/bin/python",
      "args": ["/path/to/sage-mcp/sage_mcp.py"],
      "env": {"SAGE_CWD": "/tmp"}
    }
  }
}
```

`pip install git+https://github.com/JGCoelho/simple-sagemath-mcp` works too, and
adds the `simple-sage-mcp` and `simple-sage-mcp-check` commands. Verify any
registration, at any time:

```bash
./install.sh --check
simple-sage-mcp-check /path/to/python /path/to/sage_mcp.py
```

## Tools

| tool | params (required → `*`) | returns |
| --- | --- | --- |
| `exec` | `code`*, `session`="default", `timeout_s`=120 (0 = return at once), `preparse`=true | stdout, then the repr of the last expression; `(no output)` if neither; on timeout the partial stdout + `[still running: 1.0s]` + `[job_id j7 — call poll …]`; after a respawn the output is prefixed `[kernel restarted: …]` |
| `poll` | `job_id`*, `session`="default", `wait_s`=0, `since`=0 | new output since the offset, then `[job finished in 6.0s]` or `[still running 12s; poll again with since=…]` |
| `status` | — | one line of truth: running/up, busy, not running, or unreachable, plus sessions, active jobs, live clients, last error, log path |
| `interrupt` | `force`=false | without `force`: `SIGINT` to the running job (it fails with `KeyboardInterrupt`, the session keeps its variables); with `force=true`: the kernel is stopped and every variable is lost |
| `reset` | `session`="default" | namespace emptied (`False`), session entry kept |
| `drop` | `sessions`=[], `all`=false | frees idle sessions; refuses one that still has a queued or running job |
| `run_file` | `path`*, `lang`="auto", `args`=[], `timeout_s`=120, `stdin`, `cwd` | `exit=0  script.sh  0.1s  bash script.sh` + `--- stdout ---` (+ `--- stderr ---`); non-zero exit is an error with the output attached |
| `notebook` | `action`*, `path`*, `out`, `in_place`, `cell_timeout_s`=120 | `check` / `extract` / `clear` / `run` (`run` reports how many cells ended in error); only advertised when `nbformat`+`nbconvert`+`nbclient` are importable |

## Why `poll` exists

`exec` has a timeout, and that timeout **does not cancel anything**. It is the
point of the design: a Gröbner basis, a factorization or a graph enumeration that
takes twenty minutes is normal work for Sage, and the usual MCP answer -- give up
when the call times out -- either throws that work away or, worse, leaves it
running with no way to read the result.

So `exec` returns a job id instead, and `poll(job_id, since=N)` hands back the
output produced since offset `N`:

```
exec  → prints "starting", [still running: 1.0s], [job_id j3 — call poll …]   (since=15)
poll(job_id="j3", since=15) → "done", [job finished in 41.0s]
```

Nothing is lost between the two calls: the kernel keeps the job's output, so
polling is incremental rather than a re-run, and the job survives the agent
ending its turn -- even an MCP restart, since a new server adopts the surviving
kernel instead of spawning a second one. `wait_s` blocks for the remaining time
when you would rather do that than poll in a loop.

The alternative semantics would be to kill the job on timeout. If you want that,
do it explicitly: `interrupt` first, then `poll` to collect the
`KeyboardInterrupt`. Silence is never interpreted as "cancel".

## Architecture

The server is stdio JSON-RPC 2.0, one object per line, stdlib only. `sage_mcp.py`
is the front end; `sage_client.py` spawns and talks to the kernel over a loopback
socket under a lock, and distinguishes *busy* (alive, not answering: a running
computation holds the GIL) from *gone* (never respawn a busy kernel -- that is how
you lose twenty minutes of work). `sage_kernel.py` is the long-lived Sage
process: it runs jobs on its **main thread** (that is what makes `cysignals`-based
Sage -- Singular, PARI, GAP -- behave), publishes the running job to
`running.json` so `status` and `interrupt` work while the socket is blocked, and
refuses to let a job's failure take the kernel with it.

Two details that are easy to get wrong and are load-bearing here:

- **`SIGINT` cannot kill the kernel.** An interrupt outside a job is logged and
  ignored, and only the client that owns the running job may send it, so a
  subagent cannot cancel your computation.
- **Stopping is honest.** `interrupt(force=true)` tries a graceful shutdown, then
  `SIGTERM`, then `SIGKILL`, and if the signal fails it keeps the state files and
  reports the pid rather than pretending the kernel is gone.

Kernels are shared, not per-call: several Pi sessions or subagents drive one
kernel, jobs queue on it, and it stops when the last server exits. State lives in
`${SAGE_MCP_STATE:-/tmp/sage-mcp-$UID}` (`kernel.json`, `running.json`,
`kernel.log`), never in the package directory.

`run_file` runs a file the agent already wrote, through Sage's interpreter, with
`cwd` set to that file's directory and nothing copied or stored -- `lang=python`
is Sage's python, so `from sage.all import *` just works. `notebook` drives the
Jupyter kernel SageMath ships, writing atomically and never through a pipe (a
Jupyter kernel can outlive the run and would keep the call looking hung).

## Requirements

- Linux.
- Python 3.7+ that can `import sage.all` -- Sage's *own* interpreter, whose `bin/`
  also holds the `sage` binary (that directory is prepended to `PATH`, because
  `import sage.all` loads Singular from there). Conda environments, source builds
  and distro packages all qualify; `SAGE_PYTHON` overrides the choice.
- Any MCP client. Nothing here is Pi-specific: it is one stdio server, no
  dependencies beyond the standard library.

`notebook` additionally needs `nbformat`+`nbconvert`+`nbclient` in that same
interpreter (they ship with SageMath); without them the tool is not advertised.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `SAGE_PYTHON` | our own interpreter, else the python beside `sage` on `PATH` | interpreter used to spawn the kernel (must import `sage.all`) |
| `SAGE_MCP_STATE` | `/tmp/sage-mcp-$UID` | port/pid files, `running.json`, `kernel.log` |
| `SAGE_CWD` | the server's cwd | working directory for `exec` and `run_file` |

## Tests

```bash
# 64 checks, over MCP stdio, ~60 s
/path/to/sage-env/bin/python tests/acceptance.py

# 10 checks, as a Sage script, ~10 s (Sage's own interpreter: it must be on PATH
# for `singular` to load)
PATH=/path/to/sage-env/bin:$PATH \
    sage tests/interrupt_ownership.sage
```

`acceptance.py` drives the server over stdio and checks the invariants that are
easy to regress: busy-vs-dead, spawn serialisation, stale port files, dead
launchers, an honest stop, output caps, `since` offsets, kernel.log isolation,
shared-kernel ownership, `run_file`'s interpreter selection, cwd, args, stdin,
timeout and truncation rules, the notebook cycle, and that stdout carried nothing
but JSON-RPC. `selftest.py` is the 7-check version used by `install.sh --check`.

## License

MIT -- see `LICENSE`.
