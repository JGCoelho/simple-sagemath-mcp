<p align="center">
  <img src="https://raw.githubusercontent.com/JGCoelho/simple-sagemath-mcp/main/images/simple-sagemath-banner.png"
       alt="simple-sagemath-mcp" width="820">
</p>

# simple-sage-mcp

An MCP server that gives an agent a **persistent SageMath kernel** over stdio: one
long-lived Sage process that code is run in, interrupted, inspected and stopped.
Work outlives the call that started it, and the kernel outlives the server that
spawned it.

## Quickstart

SageMath must be installed, and its own interpreter must be able to `import
sage.all`. From a checkout:

```bash
./install.sh
```

That finds a working interpreter, writes the `sage` entry into the `mcp.json` next
to the install target, and **proves the registration works** by spawning the server
and running `2+2` through it. Another client needs the block it prints:

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

`pip install git+https://github.com/JGCoelho/simple-sagemath-mcp` works too. Check a
registration at any time with `./install.sh --check` or
`simple-sage-mcp-check /path/to/python /path/to/sage_mcp.py`. `sage_mcp.py --help`
lists the options and the environment variables.

## Persistent state and sessions

### One kernel per state directory

The kernel is a separate process that the server spawns on demand and then talks to
over a private socket. Its identity is the **state directory**,
`${SAGE_MCP_STATE:-/tmp/sage-mcp-$UID}`:

```
kernel.sock   the listener (0700, AF_UNIX -- nothing on loopback to dial)
kernel.key    32-byte HMAC key the clients must present (0600)
kernel.json   pid, start time, log path -- what `status` reads when the kernel is busy
running.json  the job in flight, rewritten per job, so `status`/`interrupt` still work
kernel.log    the kernel's own log (rotated at `SAGE_MCP_LOG_MAX`, 8 MiB by default)
```

Everything that points at that directory shares one kernel: several Pi sessions,
subagents, a restarted server. Jobs queue on it and run one at a time, on the
kernel's main thread. The first `exec` starts it (`status` says `not running` until
then); when the **last** server exits and the kernel is idle it stops itself. Point
two configurations at the same `SAGE_MCP_STATE` and they share the kernel; point
them at different ones and they cannot see each other.

Nothing lives in the package directory, and nothing is stored per call: `run_file`
and `notebook` work on the files you name, in place.

### Sessions are namespaces inside that kernel

A session is **a name for a namespace**, not a process: `session="default"` unless
you say otherwise, any other name is created the first time it is used. Each
namespace starts as a copy of `from sage.all import *`, and from then on holds only
what that session's own code defines. So `a = 1` in session `work` is invisible in
`default`, and both persist between calls -- the whole point of the kernel.

`reset` empties one namespace (it does not wait for the session to go idle, so do
not reset a session whose job you still want). `drop` frees the name, refusing a
session with a job in flight. Neither restarts the kernel, and neither touches other
sessions.

### What survives what

| event | variables | job in flight | counters |
| --- | --- | --- | --- |
| `exec` hits its `timeout_s` | kept | **keeps running**; `poll` collects it | live |
| the agent's turn ends | kept | keeps running | live |
| the MCP server exits or is restarted | kept -- the next server adopts the kernel | keeps running; the new server's `poll` collects it | live in the kernel |
| `interrupt` | kept | fails with `KeyboardInterrupt` | `interrupted` + 1 |
| `notebook` / `run_file` | unaffected | unaffected (different process) | -- |
| `interrupt(force=true)` | **lost** | stopped mid-flight | reset with the kernel |
| the kernel exits on its own, or `/tmp` is cleaned | lost | lost | lost |

The kernel is the authority on all of it; the server keeps no state of its own
except its identity and its view of the socket. That is why a `poll` works after a
server restart, and why counts (`jobs: N run, ...`) reset exactly when the kernel
does -- `counters_since` says since when.

## Tools

| tool | params (required → `*`) | returns |
| --- | --- | --- |
| `exec` | `code`*, `session`="default", `timeout_s`=120 (0 = return at once), `preparse`=true | stdout, then the repr of the last expression; on timeout the partial output plus a job id to `poll` |
| `poll` | `job_id`*, `wait_s`=0, `since`=0 | new output since the offset, ending in `[job finished in 6.0s]` or `[still running 12s; ...]` |
| `status` | -- | kernel/session/job/client state, last error, limits, log path |
| `interrupt` | `force`=false | `SIGINT` the running job (it fails with `KeyboardInterrupt`, variables kept); `force=true` stops the kernel and loses them |
| `reset` / `drop` | `session`; `sessions`=[], `all`=false | empty a namespace; free idle sessions |
| `run_file` | `path`*, `lang`="auto", `args`=[], `timeout_s`=120, `stdin`, `cwd` | exit code and duration, then stdout/stderr inline; a non-zero exit is an error carrying both |
| `notebook` | `action`*, `path`*, `out`, `in_place`, `cell_timeout_s`=120, `timeout_s` | `check` / `extract` / `clear` / `run`; all four run in Sage's interpreter, and the tool is advertised only when *that* interpreter has `nbformat`+`nbconvert`+`nbclient` |

Five short transcripts with the real output -- a job outliving its `exec`, an
interrupt, `run_file`, sessions, notebooks -- are in [`examples/`](https://github.com/JGCoelho/simple-sagemath-mcp/tree/main/examples).

## Jobs, and why `poll` exists

`exec`'s timeout **cancels nothing**: a Gröbner basis or a graph enumeration that
takes twenty minutes is normal work for Sage, and a timeout that threw it away would
be a bug. So `exec` answers with a job id (or the finished output, if it got there
within `timeout_s`), and `poll(job_id, since=N)` returns the output produced since
offset `N` -- incremental, never a re-run. To cancel, do it explicitly: `interrupt`,
then `poll` to collect the `KeyboardInterrupt`. Silence is never read as "cancel".

`interrupt` is owner-checked: only the client whose job is running may send it.
`interrupt(force=true)` is not, on purpose -- it is the escape hatch for a kernel
wedged past answering its owner, so it is **machine-level**: any client sharing the
state directory can stop the kernel and destroy another's work. `DESIGN.md`, the
tool description and a test say so, so it cannot change quietly.

## Why Sage, not Python

The kernel preparses by default, so this is **Sage's REPL**, not Python with
`sage.all` imported: `2^10` is 1024, `1/2` stays the rational `1/2`, `type(2)` is
`Integer`, and `R.<x> = QQ[]` works. An ipykernel with `from sage.all import *`
silently gets all of these wrong.

## Architecture

`sage_mcp.py` is the stdio front end, built on the official MCP Python SDK
(`MCPServer`), so schemas, validation and the protocol come from the SDK.
`sage_client.py` spawns and talks to the kernel: it holds a lock, presents the
authkey, and distinguishes *busy* (alive, not answering -- a running computation
holds the GIL) from *gone*, because respawning a busy kernel is how twenty minutes
of work disappears. `sage_kernel.py` is the long-lived process: it runs jobs on the
**main thread** so `cysignals`-based Sage (Singular, PARI, GAP) behaves, publishes
the in-flight job to `running.json`, and refuses to let a job's failure or an idle
`SIGINT` take the kernel with it. `subproc.py` is the child-process core behind
`run_file` and `notebook`, `limits.py` the `SAGE_MCP_LIMITS` knob. See
[DESIGN.md](https://github.com/JGCoelho/simple-sagemath-mcp/blob/main/DESIGN.md) for the reasoning and `tests/conformance.py` for the
protocol contract.

`sage://kernel/status` (`application/json`) is the machine-readable form of
`status`: `pid`, `state`, uptime, `restarts`, the `jobs` counters, `last_error` with
the job and session that produced it, `sessions`, `limits` (with `source: kernel`
when the kernel answered, `configured` when it did not), and the log tail -- read
from the file, so it answers while a job runs.

## Requirements

- Linux.
- **Two interpreters, on purpose.** The *server* is any Python 3.10+ with the `mcp`
  package, and nothing else; the *kernel* is Sage's own interpreter, found at
  runtime by `SAGE_PYTHON`, then `sage -c 'import sys;print(sys.executable)'`, then
  the python beside `sage` on `PATH`. A Sage crash cannot take the transport down,
  and `mcp` never has to be installed into the Sage environment.
- `notebook` needs `nbformat`+`nbconvert`+`nbclient` in the *kernel's* interpreter
  (they ship with SageMath).

## Configuration

| variable | default | meaning |
| --- | --- | --- |
| `SAGE_PYTHON` | discovered | interpreter that must `import sage.all` -- the kernel |
| `SAGE_MCP_STATE` | `/tmp/sage-mcp-$UID` | the state directory above; the kernel's identity |
| `SAGE_CWD` | server's cwd | working directory for `exec` and `run_file` |
| `SAGE_MCP_LIMITS` | `cpu=off,as=8G,fsize=1G,nproc=off` | per-process limits, below |
| `SAGE_MCP_GAP_LIMIT` | `3` (`0` disables) | GAP crash-loop guard, below |
| `SAGE_MCP_LOG_MAX` | `8` MiB (`0` disables) | rotate `kernel.log` to `kernel.log.1`, one generation |

`SAGE_MCP_LIMITS` bounds what the kernel and the children `run_file`/`notebook` spawn
may consume, so a runaway job fails instead of taking the machine down:

| limit | default | scope | on trip |
| --- | --- | --- | --- |
| `cpu` | off | kernel, from just after Sage's import | `SIGXCPU` caught: the job fails with `ResourceLimitError` |
| `as` | 8 GiB | kernel + children | `MemoryError` → job error |
| `fsize` | 1 GiB | kernel + children | `SIGXFSZ`/`EFBIG` → job error |
| `nproc` | off | **per UID, machine-wide** | `fork`/`Popen` gets `EAGAIN` → that job or child spawn fails |
| core | always `0` | kernel + children | no core dumps |

Sizes take `K`/`M`/`G`; `off`/`0` leaves the inherited limit alone; unmentioned keys
keep their default, so `SAGE_MCP_LIMITS="cpu=60"` only adds a CPU budget. A typo is
an error naming the knob. Only the *soft* limit is lowered, so a job that needs more
room gets it by restarting the kernel (`interrupt(force=true)`) under a different
`SAGE_MCP_LIMITS`. `nproc` is a margin over the tasks your UID already runs, armed
before each job -- not a count of the kernel's children. A job that trips a limit
fails with `ResourceLimitError: RLIMIT_...`, its text is prefixed with `[limits]`,
the kernel survives, and `status` says which limit applies.

`SAGE_MCP_GAP_LIMIT` is the **GAP crash-loop guard**. `structure_description()` on
hostile input makes GAP crash-restart in a loop, forking a process per crash, and no
timeout catches it because the cell never finishes. The kernel counts the
`Gap crashed` / `Restarting Gap` markers as they are written and, at the limit,
`SIGINT`s that job once -- the same signal as `interrupt`, so it ends as a
`KeyboardInterrupt`, the session keeps its variables, and `poll` collects it. The
result is prefixed with `[gap-guard] interrupted after N 'Gap crashed' markers ...`,
the trip is logged, and `status` counts it. The evidence is the **writer**, not the
text: only a write from `sage/interfaces/gap.py` (stdout or stderr) -- or a stderr
write from a job that has already written through it -- counts, so echoing the words
cannot trip it, and importing the module (which `from sage.all import *` does for
every session) is not evidence. If the job catches the interrupt and runs on, the
result says `SIGINT was sent but the job continued` instead of claiming an
interruption that did not happen.

## Tests

```bash
# 24 conformance cases, one row each, ~70 s
/path/to/sage-env/bin/python tests/conformance.py        # or --only C13,C15

# 144 checks over MCP stdio through the official client, ~200 s
/path/to/sage-env/bin/python tests/acceptance.py

# 10 checks as a Sage script, ~10 s (`singular` must be on PATH)
PATH=/path/to/sage-env/bin:$PATH sage tests/interrupt_ownership.sage

# the frozen tools/list contract (8 tools, 0 mismatches)
/path/to/sage-env/bin/python tests/snapshot_tools.py --compare
```

`acceptance.py` guards what is easy to regress: busy-vs-dead, spawn serialisation,
stale state files, dead launchers, an honest stop, output caps, `since` offsets, log
isolation, shared-kernel ownership, socket privacy and the authkey, adoption across a
server restart, the three discovery routes, a cancelled `exec` still being
recoverable, the `run_file` contract, both halves of the two-interpreter rule for
`notebook`, the GAP guard (writer-based, and honest about a swallowed signal), the
limits (each fails its job and leaves the kernel alive), the status resource and its
counters, and `--check` driving a registration in the client's own environment.

`sage_mcp.py --check` (behind `install.sh --check` and `simple-sage-mcp-check`) is
the 8-check smoke test; its last check runs the C01/C04/C08/C17/C20 subset of the
matrix **against the registration** -- its `command`, `args` and `env` exactly as
written -- so a registration that only works because *your* shell exports
`SAGE_PYTHON` is reported as broken, the way the client would find it. Without
`tests/` (pip/uvx ship none) that subset is `SKIP` and left out of the totals; a
skipped check is never a pass.

## License

MIT -- see `LICENSE`. What changed in each release: `CHANGELOG.md`.
