# Changelog

The tool surface is frozen: eight tools, with the names, parameters and result
texts pinned by `tests/snapshots/tools-list.json`. Changes that would alter any
of them are breaking and get a major version.

## 1.0.0

First release.

- **The kernel is a process, not a call.** `exec` runs on the kernel's main thread
  (so `cysignals`-based Sage behaves), one job at a time, and its `timeout_s`
  bounds the wait, not the work: the result carries a job id and `poll(job_id,
  since=N)` returns only the output produced after offset `N`. `interrupt` is the
  only cancel, and it is owner-checked.
- **Sessions are namespaces in that one kernel** -- `exec`, `reset`, `drop` -- and
  the kernel outlives the server: a restarted server adopts it, so a job started
  before the restart is still collectable, and the counters reset only when the
  kernel itself restarts.
- **Two interpreters, on purpose.** The server needs any Python 3.10+ with `mcp`;
  the kernel is Sage's own interpreter, discovered at runtime (`SAGE_PYTHON`, then
  `sage -c`, then the python beside `sage`). `mcp` never has to be installed into
  the Sage environment, and a Sage crash cannot take the transport down.
- **Transport** is an `AF_UNIX` socket in a `0700` state directory with a
  `multiprocessing.connection` authkey; foreign keys are refused, a busy kernel is
  never respawned, and stopping is honest (graceful, then `SIGTERM`, then
  `SIGKILL`, reporting the pid if it fails).
- **`run_file` and `notebook`** run files that already exist, in Sage's
  interpreter, and return the output inline; nothing is copied or stored.
  `notebook` is advertised only when the interpreter that executes it can.
- **GAP crash-loop guard** (`SAGE_MCP_GAP_LIMIT`, default 3): the markers count
  only when written by `sage/interfaces/gap.py`, and the result says whether the
  interrupting `SIGINT` actually ended the job.
- **Resource limits** (`SAGE_MCP_LIMITS`: `cpu`, `as`, `fsize`, `nproc`, core
  always 0) are soft-only, applied to the kernel and the children it starts, and a
  trip fails the job rather than the kernel.
- **Observability**: the `status` tool and the `sage://kernel/status` resource
  (jobs, gap trips, last error with its job and session, sessions, limits and
  their source, log tail), readable while a job holds the kernel.
- **Install**: `install.sh` (discovers the interpreter, writes the registration,
  proves it works), `simple-sage-mcp --help`, and `--check`/`simple-sage-mcp-check`
  which drives the registration in the client's own environment.
- Verified by 144 acceptance checks over the official MCP client, a 24-case
  protocol conformance matrix, 10 Sage-side interrupt-ownership checks, and a
  frozen `tools/list` snapshot (8 tools, 0 mismatches).
