## Sage (MCP: `sage`)

A persistent Sage kernel, not a proof checker. Tools: `exec`, `poll`, `reset`,
`drop`, `status`, `interrupt`, `kill`, `run_file`, `notebook`.

- `exec(code, session="default", timeout_s=120, preparse=true)` runs Sage code on
  the kernel's main thread; state persists per session between calls. There is no
  separate start call: the first `exec` spawns the kernel. One job runs at a time,
  so a concurrent call is queued (`[queued: another job is running]`).
  `timeout_s=0` returns the job id at once without collecting any output.
  `timeout_s` never cancels the job: on timeout you get the partial stdout plus
  `[job_id jN ...]` — the job keeps running, collect it with `poll`.
- `poll(job_id, since=N, wait_s=T)` returns only new output from offset `N` (0 to
  start); `wait_s` blocks up to T seconds for the job to finish. Use
  `wait_s` rather than polling in a loop, and print progress sparsely (job output
  is capped at 4 MB; a truncated repr at 4000 chars).
- `interrupt` sends SIGINT: the job fails with `KeyboardInterrupt` and the session
  KEEPS its variables — the first thing to try for a runaway computation. It
  refuses when the running job belongs to another MCP client (another Pi session
  or subagent), and does nothing when the kernel is idle.
- `kill` stops the kernel (all variables lost); the next `exec` starts a fresh one
  and prefixes its output with `[kernel restarted: ...]`.
- `status` shows pid/uptime, live MCP clients, sessions, active jobs, last error
  and the log path (`${SAGE_MCP_STATE:-/tmp/sage-mcp-$UID}/kernel.log`).
- Sessions are never evicted; use `drop` to free one (it refuses sessions with a
  running job). Use `reset` to empty a namespace but keep the session.
- Several Pi sessions/subagents share one kernel: their jobs queue on it, and it is
  stopped only when the last of them exits (a subagent finishing cannot kill your
  run).
- Long jobs: set `timeout_s` generously in the first `exec`.
- To verify that state was really lost (after `kill`), test `'name' in globals()`:
  short names such as `n`, `N`, `e`, `i` and `pi` are Sage predefines (`N` is
  `numerical_approx`), so a `NameError` probe can resolve to an unrelated
  function and lie.
- Do not use Sage for file editing or shell commands, only for math or
  calculations.
- `run_file(path, lang="auto", args=[], timeout_s=120, stdin=None, cwd=None)`
  runs a file **you wrote yourself** and returns its output inline: exit code,
  duration, stdout, stderr. Nothing is copied, stored or indexed, and `cwd`
  defaults to the file's own directory. `lang=auto` uses the extension and
  honours a `#!` for executables; `lang="python"` is Sage's interpreter, so
  `from sage.all import *` just works (no venv); `lang="sage"` shells out to
  `sage`. A non-zero exit or a timeout is an error with the output attached (the
  timeout kills the whole process group).
- `notebook(action="check"|"extract"|"clear"|"run", path=...)` validates,
  prints the code cells, strips outputs, or executes an `.ipynb` with nbconvert
  (default output `<name>.out.ipynb`, atomic) using the Jupyter kernel SageMath
  ships; `run` reports how many cells ended in error.

PDFs are another MCP server (`pdf`), not this one.
