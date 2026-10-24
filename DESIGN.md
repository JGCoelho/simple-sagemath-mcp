# Design notes

The parts of the README that were too long to keep there. `README.md` says *what*
the server does; this file says *why* it is built that way.

## On the SDK, and what stayed ours

The MCP layer is the official SDK (`mcp.server.mcpserver.MCPServer`, stdio
transport): schemas and argument validation come from type hints, sync handlers
run on worker threads (so `poll`/`status`/`interrupt` answer while `exec` waits),
and while serving, fd 1 is pointed at stderr, so a handler that prints cannot
corrupt the wire -- a test asserts that rather than trusting it.

This is affordable only because of the split it forced: the *server* needs
`mcp` and nothing else, the *kernel* is Sage's interpreter discovered at runtime.
The hand-rolled predecessor existed because `mcp` would otherwise have had to go
into the Sage environment, where every dependency is a thing that can fail to
build or clash. Two interpreters also mean a Sage crash cannot take the transport
down.

What stayed ours is what no SDK provides: user code on the kernel's main thread,
`timeout` that never cancels (job id + `poll` instead), busy-vs-dead, interrupt
ownership, the crash-loop guard, and the resource limits. Those are the promises
the tool descriptions carry.

## Three details that are easy to get wrong

- **`SIGINT` cannot kill the kernel.** An interrupt outside a job is logged and
  ignored, and only the client that owns the running job may send it, so a
  subagent cannot cancel your computation.
- **`interrupt(force=true)` has no owner check, and that is deliberate.** It is
  the escape hatch for a kernel that is wedged so badly its owner is gone, so it
  is **machine-level**: any MCP server that shares the state directory may stop
  the kernel and destroy another client's work. The refusal message for `SIGINT`
  says "not yours"; `force=true` is documented (README, tool description) and
  pinned by an acceptance check instead of being silently allowed. Removing the
  hole would mean leaving a wedged kernel unstoppable.
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
kernel, jobs queue on it, and it stops when the last server exits. Sessions are
namespaces inside that kernel -- a name to a dict of globals seeded from
`from sage.all import *` -- so `reset` is a `clear()` and `drop` frees a name,
neither of which restarts anything; the server itself holds no user state, which
is why a restarted server can adopt the kernel and still serve `poll`. State lives in
`${SAGE_MCP_STATE:-/tmp/sage-mcp-$UID}` (`kernel.sock`, `kernel.json`,
`running.json`, `kernel.log`), never in the package directory.

## `run_file` and `notebook`

`run_file` runs a file the agent already wrote, through Sage's interpreter, with
`cwd` set to that file's directory and nothing copied or stored -- `lang=python`
is Sage's python, so `from sage.all import *` just works.

`notebook` drives the Jupyter kernel SageMath ships, writing atomically and never
through a pipe (a Jupyter kernel can outlive the run and would keep the call
looking hung).

## Limits: chosen so the kernel survives

`limits.py` is the only place that knows `SAGE_MCP_LIMITS`; the server, the kernel
and `subproc` all import it, so the parsing, the messages and the reporting cannot
drift apart.

Three decisions are not obvious and were forced by measurement:

- **Only the soft limit is lowered.** A job that trips a limit therefore fails
  without taking the kernel with it, and a hard limit below the configured soft
  one is honoured by clamping instead of breaking the boot. Raising a limit is
  the operator's job: stop the kernel and start it with a larger
  `SAGE_MCP_LIMITS`.
- **`as`/`fsize` are applied before `import sage.all`, `cpu` after it.** The
  import peaks near 4 GiB of address space (so `as` must already hold) and costs
  seconds of CPU (so `RLIMIT_CPU`, which counts from `exec`, would be spent before
  the first job if applied earlier). `cpu` therefore means "from now on", and the
  handler exists before the first job can trip it.
- **`nproc` is a margin over the UID's task count, re-armed per job.** Linux
  counts processes *and threads*, per UID, machine-wide, so an absolute number
  from `/proc` is stale the moment another session starts. The kernel arms
  "current tasks + margin" before each job (and `subproc` before each child
  spawn), which is also why `nproc` is applied after the import: Sage's import
  starts threads.

A tripped limit raises `ResourceLimitError(BaseException)` into the running job --
`BaseException` so an `except Exception` in user code cannot turn "this job is too
big" into a `SIGKILL` for the kernel; the hard limit is the backstop if it is
caught anyway. One raise per job is enough: `SIGXFSZ`/`SIGXCPU` arrive again while
the job unwinds, and a second raise would replace the error already recorded
(`Job.limit_guarded`). Outside a job the handler only notes the trip once, because
`SIGXCPU` repeats every second above the soft limit.

The server applies the same `as`/`fsize`/core at startup, so `run_file` and
`notebook` children inherit them, and it re-arms the `nproc` margin immediately
before each child spawn (`subproc._run`) -- the same "current tasks + margin" rule
the kernel uses per job, never at startup, because a margin that small would break
anything the server starts later. `cpu` is the one limit the server leaves alone:
it counts against the *server's* lifetime, not a child's, so a server-level budget
would kill the transport instead of the job. `kernel.log` is rotated at 8 MiB
(`SAGE_MCP_LOG_MAX`) into one `.1` generation, because the log is fd 1/2 and would
otherwise be the first casualty of `fsize`.

## The conformance matrix

`tests/conformance.py` exists because "the acceptance suite is green" says
nothing about *which* wire-level promise broke when a client misbehaves. It runs
C01-C24 from PLAN §6, one row per case, through `mcp.client.stdio` +
`ClientSession` -- the code path a real client uses.

Two cases had to be written against what the SDK actually does, not what the
matrix first assumed, and both differences are asserted rather than glossed over:

- **C06 (unknown argument).** `mcp` 2.3.0 silently ignores an unknown field
  instead of rejecting the call (pydantic's default `extra="ignore"`), so the
  case asserts what matters: no protocol damage, exactly one job ran, and the
  kernel is intact. Making it a protocol error would mean owning argument
  validation the SDK does.
- **C24 (malformed input).** A line that is not JSON is dropped by the SDK's
  stdio reader (debug log, no `-32700` reply) and the connection keeps serving;
  an unknown *method* does get `-32601`. The case asserts both observations and
  that the next valid request is still answered, which is the property the
  matrix is really about.

C13 and C15 are the two cases that depend on harness setup: C13 needs a
`tools/call` still in flight when the cancel lands, and C15 needs two servers
sharing one kernel (pids compared). Both fail with an explicit message when the
setup cannot be established, because a conformance run that silently skips them
would report success for a property it never tested.

## The alternative semantics we rejected

`exec`'s timeout could have killed the job instead of returning a job id. We did
not do that: a Gröbner basis, a factorization or a graph enumeration that takes
twenty minutes is normal work for Sage, and the usual MCP answer -- give up when
the call times out -- either throws that work away or leaves it running with no
way to read the result. If you want a job dead, do it explicitly: `interrupt`
first, then `poll` to collect the `KeyboardInterrupt`. Silence is never
interpreted as "cancel".

## What this is not

No sandbox, no allow-list, no seccomp, no network or filesystem isolation: a job
runs with the server's own privileges, and `SAGE_MCP_LIMITS` is a resource bound
(a runaway job fails) rather than a security boundary. XBP Europe's server is the
one to read if a *sandboxed* subset of Sage is what you want; this is a general
kernel for work you would run yourself, and the two-interpreter split keeps `mcp`
out of the Sage environment rather than keeping code away from your files.

