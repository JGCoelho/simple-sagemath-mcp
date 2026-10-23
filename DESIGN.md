# Design notes

The parts of the README that were too long to keep there. `README.md` says *what*
the server does; this file says *why* it is built that way.

## Why the protocol is hand-rolled

The server is stdio JSON-RPC 2.0, one object per line, stdlib only. The stdio
layer is a few hundred lines of `json.dumps` over a pipe, which is what makes
`pip install` enough: the server must run under Sage's *own* interpreter, where
every dependency is another thing that can fail to build or clash with the Sage
environment.

The price is that only what these tools need is implemented -- `initialize`,
`tools/list`, `tools/call`, `ping` and empty `resources`/`prompts` lists. There
is no capability negotiation, no progress notifications, no tool annotations, no
`structuredContent`, and `notifications/cancelled` is deliberately ignored (a
timeout is not a cancel). Descriptions and text results carry that semantic load
instead. An `mcp`-SDK proxy can be put in front if the newer bits are needed; the
wire is plain JSON-RPC 2.0 and the tools are ordinary functions.

## Three details that are easy to get wrong

- **`SIGINT` cannot kill the kernel.** An interrupt outside a job is logged and
  ignored, and only the client that owns the running job may send it, so a
  subagent cannot cancel your computation.
- **Stopping is honest.** `interrupt(force=true)` tries a graceful shutdown, then
  `SIGTERM`, then `SIGKILL`, and if the signal fails it keeps the state files and
  reports the pid rather than pretending the kernel is gone.
- **The socket is private.** It is an `AF_UNIX` socket, not a TCP port, so there
  is nothing on loopback for another local user to dial. The state directory is
  created `0700` and must be owned by you: on a shared machine `/tmp` lets
  someone else create `/tmp/sage-mcp-$UID` first, so ownership is checked (and a
  loosened mode is tightened) before a kernel is allowed to run there.

## Busy vs. gone

`sage_client.py` spawns and talks to the kernel over a Unix domain socket inside
a private (0700) state directory, under a lock, and distinguishes *busy* (alive,
not answering: a running computation holds the GIL) from *gone* (never respawn a
busy kernel -- that is how you lose twenty minutes of work).

`sage_kernel.py` is the long-lived Sage process: it runs jobs on its **main
thread** (that is what makes `cysignals`-based Sage -- Singular, PARI, GAP --
behave), publishes the running job to `running.json` so `status` and `interrupt`
work while the socket is blocked, and refuses to let a job's failure take the
kernel with it.

Kernels are shared, not per-call: several Pi sessions or subagents drive one
kernel, jobs queue on it, and it stops when the last server exits. State lives in
`${SAGE_MCP_STATE:-/tmp/sage-mcp-$UID}` (`kernel.sock`, `kernel.json`,
`running.json`, `kernel.log`), never in the package directory.

## `run_file` and `notebook`

`run_file` runs a file the agent already wrote, through Sage's interpreter, with
`cwd` set to that file's directory and nothing copied or stored -- `lang=python`
is Sage's python, so `from sage.all import *` just works.

`notebook` drives the Jupyter kernel SageMath ships, writing atomically and never
through a pipe (a Jupyter kernel can outlive the run and would keep the call
looking hung).

## The alternative semantics we rejected

`exec`'s timeout could have killed the job instead of returning a job id. We did
not do that: a Gröbner basis, a factorization or a graph enumeration that takes
twenty minutes is normal work for Sage, and the usual MCP answer -- give up when
the call times out -- either throws that work away or leaves it running with no
way to read the result. If you want a job dead, do it explicitly: `interrupt`
first, then `poll` to collect the `KeyboardInterrupt`. Silence is never
interpreted as "cancel".
