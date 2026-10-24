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
| `interrupt` | `force`=false | `SIGINT` the running job (it fails with `KeyboardInterrupt`, variables are kept); `force=true` stops the kernel and loses them -- no owner check, so any client sharing the state dir may use it |
| `reset` / `drop` | `session`; `sessions`=[], `all`=false | empty a namespace; free idle sessions (refuses a busy one) |
| `run_file` | `path`*, `lang`="auto", `args`=[], `timeout_s`=120, `stdin`, `cwd` | exit code and duration, then stdout/stderr inline; a non-zero exit is an error carrying both |
| `notebook` | `action`*, `path`*, `out`, `in_place`, `cell_timeout_s`=120, `timeout_s` | `check` / `extract` / `clear` / `run`; all four run in Sage's interpreter, and the tool is advertised only when *that* interpreter has `nbformat`+`nbconvert`+`nbclient` |

## Status resource

`sage://kernel/status` (`application/json`) is the machine-readable form of
`status`, for a client that does not want to parse prose: the kernel's `pid`,
`state` (`idle` / `busy` / `unreachable` / `not running`), uptime, `restarts`,
the `jobs` counters (`run`, `failed`, `interrupted`, `queued`, `last_job`,
`gap_trips`), `last_error` with the job and session that produced it,
`sessions`, `limits` (the `cpu`/`as`/`fsize`/`nproc` values that apply, plus
`source`: `kernel` when it answered, `configured` when it did not), and the log
`path` plus its last 20 lines (read from the file's tail, so it answers while a
job runs). The counters live **in the kernel** and reset when it restarts;
`counters_since` says since when, so they are never read as lifetime totals. The
server adds what only it knows: its own pid, version, live `clients`, `spawns`
and `restarts`. The `status` tool carries the same numbers in one line:
`jobs: N run, N failed, N interrupted; gap-guard: N; restarts: N`. It prints one
more line than that, on purpose: `last job per session: default=j41, …` is what
makes a cancelled `exec` recoverable (the reply is gone, the job is not), so it
is kept and asserted rather than folded into a frozen format.

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

`SAGE_MCP_LOG_MAX` (default 8 MiB, `0` disables) is where `kernel.log` is rotated
to `kernel.log.1` (one generation kept), so `SAGE_MCP_LIMITS`' `fsize` can never
bite the kernel's own log by accident.

`SAGE_MCP_GAP_LIMIT` (default `3`; `0` disables it) is the **GAP crash-loop
guard**. `structure_description()` on hostile input makes GAP crash-restart in a
loop, forking a process per crash; no timeout catches that, because the cell
never finishes. The kernel counts the `Gap crashed` / `Restarting Gap` markers as
they are written and, at the limit, interrupts that job **once** -- the same
`SIGINT` as `interrupt`, so it ends as a `KeyboardInterrupt`, the session keeps
its variables, and `poll` collects it. The result text is prefixed with
`[gap-guard] interrupted after N 'Gap crashed' markers ...`, the trip is logged to
`kernel.log`, and the count is in `status`/the status resource. The evidence is
the **writer**, not the text: Sage prints the markers from
`sage/interfaces/gap.py`, so only a write from that file (on stdout or stderr),
or a stderr write from a job that has already written through it, counts. A job
echoing the words -- or one that merely has `sage.interfaces.gap` imported, which
`from sage.all import *` does for every job -- cannot trip it. If the job catches
the interrupt and runs on, the result says so (`SIGINT was sent but the job
continued`) instead of claiming an interruption that did not happen. Per-process
limits are the backstop for a fork storm that outruns the markers.

## Limits

`SAGE_MCP_LIMITS` bounds what the kernel -- and the children `run_file` and
`notebook` start -- may consume, so a runaway job fails instead of taking the
machine with it:

```bash
SAGE_MCP_LIMITS="cpu=3600,as=8G,fsize=1G,nproc=off"   # the defaults
```

| limit | default | scope | on trip |
| --- | --- | --- | --- |
| `cpu` | off | kernel | `SIGXCPU` is caught: the job fails with `ResourceLimitError`, the kernel keeps running |
| `as` | 8 GiB | kernel + children | `MemoryError` -> job error |
| `fsize` | 1 GiB | kernel + children | `SIGXFSZ`/`EFBIG` -> job error |
| `nproc` | off | **per UID, machine-wide** | `fork`/`Popen` gets `EAGAIN`: the job (or the `run_file`/`notebook` child spawn) fails |
| core | always `0` | kernel + children | no core dumps in `/tmp` |

Sizes take a `K`/`M`/`G` suffix; `off`/`0` disables a limit (the inherited one is
left alone); keys you do not mention keep their default, so
`SAGE_MCP_LIMITS="cpu=60"` only adds a CPU budget. A typo is an error naming the
knob, not a silently ignored key. Only the *soft* limit is lowered
(`hard = min(soft + slack, inherited hard)`), so a job that needs more room gets
it by stopping the kernel (`interrupt(force=true)`) and starting a fresh one with
a different `SAGE_MCP_LIMITS`.

Two details worth knowing before setting them:

- **`cpu` is a budget for the kernel's life**, counted from just after Sage's
  import -- the import itself costs seconds of CPU, and charging it to the first
  job would make `cpu=2` unusable. After a CPU trip the kernel is on its last few
  CPU seconds: restart it with a larger budget.
- **`nproc` is a margin over your UID's tasks, not a count of the kernel's
  children.** Linux counts every process *and thread* of the real UID (so the
  limit is machine-wide, and a value smaller than what you already run breaks
  your session). `nproc=200` therefore means "200 more tasks than I run now"; it
  is armed before each job (and each child spawn) over the settled count, because
  an absolute number captured at boot goes stale. Sage's import starts threads, so
  `nproc` is applied after the import, alongside `cpu`; `as`/`fsize`/core are
  applied *before* it, which matters: Sage peaks near 4 GiB of address space, so
  keep `as` comfortably above that.

A job that trips a limit fails with `ResourceLimitError: RLIMIT_...`, the result
text is prefixed with `[limits] ...`, the kernel survives, and `status` (its
`last error:` and `limits:` lines) plus `sage://kernel/status` (`limits`) say
which limit applies.

## Tests

```bash
# 24 conformance cases, one row each, ~70 s
/path/to/sage-env/bin/python tests/conformance.py        # or --only C13,C15

# 140 checks over MCP stdio (official client), ~195 s
/path/to/sage-env/bin/python tests/acceptance.py

# 10 checks as a Sage script, ~10 s (`singular` must be on PATH)
PATH=/path/to/sage-env/bin:$PATH sage tests/interrupt_ownership.sage

# the frozen tools/list contract (8 tools, 0 mismatches)
/path/to/sage-env/bin/python tests/snapshot_tools.py --compare
```

`acceptance.py` checks the invariants that are easy to regress: busy-vs-dead,
spawn serialisation, stale state files, dead launchers, an honest stop, output
caps, `since` offsets, log isolation, shared-kernel ownership, socket privacy and
the authkey, adoption across a server restart, the three discovery routes, a
cancelled `exec` still being recoverable, the `run_file` contract, the notebook
cycle (including the two-interpreter rule: the tool is advertised iff the
interpreter that runs it can, and the server itself never imports nbformat), the
GAP crash-loop guard (the writer decides, and the note describes what the signal
actually did), the resource limits (each one fails its job and leaves
the kernel alive, children inherit them, the log rotates, `_log` is reentrant), the
status resource
and its counters, `--check` driving a registration in the client's own environment,
the documented `force=true` ownership hole, and that a handler's stray `print`
cannot reach the wire.

`conformance.py` is the matrix of PLAN §6: the 24 protocol-level cases
(C01-C24), one row each, every request through the official client, and a
non-zero exit if any case fails. It owns its state directories, so it never
touches your kernel. Two cases are deliberate about their own setup: C13 warms the
kernel before it cancels (a cold server cannot have a job in flight, and that
harness dependency is not a product verdict), and C15 fails loudly when it cannot
get two servers onto one kernel -- a skipped hard case would prove nothing.

`sage_mcp.py --check` (behind `install.sh --check` and
`simple-sage-mcp-check`) is the 8-check smoke test; its last check runs the
C01/C04/C08/C17/C20 subset of that matrix through the same harness. That subset
spawns **the registration** -- its `command`, `args` and `env` exactly as written
(`SAGE_MCP_TEST_SERVER` carries them to the harness), and the checks before it run
the server in that same environment, never in the checking shell's: a registration
that only works because *your* shell has `SAGE_PYTHON` exported is reported as
broken, the way the client would find it. In an install without `tests/` (pip/uvx
ship no tests) the subset is reported as `SKIP` and left out of the totals -- a
skipped check is never a pass.

`interrupt` is owner-checked; `interrupt(force=true)` is not. It is the escape
hatch for a kernel that is wedged past answering its owner, which makes it
**machine-level**: any server sharing `SAGE_MCP_STATE` can stop the kernel and
lose another client's work. The tool description, `DESIGN.md` and an acceptance
check say so, so the behaviour cannot change silently.

## License

MIT -- see `LICENSE`.
