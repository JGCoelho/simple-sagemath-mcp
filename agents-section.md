## Sage (MCP: `sage`)

A persistent Sage kernel, not a proof checker. Tools: `exec`, `poll`, `reset`,
`drop`, `status`, `interrupt`, `run_file`, `notebook`.

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
- Sessions are namespaces inside one shared kernel: `a = 1` in session `work` is
  invisible in `default`, and both persist between calls, across turns, and across
  an MCP server restart (a new server adopts the surviving kernel, so `poll` still
  collects a job the old one started). Use distinct session names when parallel
  subagents must not collide, and print progress sparsely -- output is capped.
- `interrupt` sends SIGINT: the job fails with `KeyboardInterrupt` and the session
  KEEPS its variables — the first thing to try for a runaway computation. It
  refuses when the running job belongs to another MCP client (another Pi session
  or subagent), and does nothing when the kernel is idle.
- `interrupt(force=true)` stops the kernel instead of the job: all variables are
  lost, the next `exec` starts a fresh one and prefixes its output with
  `[kernel restarted: ...]`. It has **no owner check** (unlike plain
  `interrupt`): it is the escape hatch for a wedged kernel, so any client sharing
  the state directory can use it and lose another client's work.
- A GAP crash-restart loop (e.g. `structure_description()` on hostile input) is
  caught by the guard when *GAP's own interface* writes the markers: the job's
  result starts with `[gap-guard] interrupted after N 'Gap crashed' markers ...`,
  the job ended as `KeyboardInterrupt`, the session keeps its variables, and
  `poll` collects it. If the job catches the interrupt and continues, the result
  says `SIGINT was sent but the job continued` instead. Do not retry the same
  input -- try a different invariant or representation. `SAGE_MCP_GAP_LIMIT`
  (default 3, `0` disables) changes the threshold; your own prints of that text
  do not trip it.
- Resource limits (server-side `SAGE_MCP_LIMITS`: `cpu`, `as=8G`, `fsize=1G`,
  `nproc`, and `RLIMIT_CORE=0` always) bound the kernel and the children
  `run_file`/`notebook` start. A job that trips one fails with
  `ResourceLimitError: RLIMIT_...` (or `MemoryError`, or `EAGAIN` from `fork`)
  and the kernel survives: collect it with `poll`. `cpu` counts from after
  Sage's import; `nproc` is a margin over the tasks your UID already runs
  (machine-wide), not a count of the kernel's children.
- `status` shows pid/uptime, live MCP clients, sessions, active jobs, the last
  error, the limits, the log path
  (`${SAGE_MCP_STATE:-/tmp/sage-mcp-$UID}/kernel.log`) and one line of counters:
  `jobs: N run, N failed, N interrupted; gap-guard: N; restarts: N`.
- `sage://kernel/status` (`application/json`) is the same thing for a client that
  wants data instead of prose: kernel state/pid/uptime/restarts, `jobs` counters,
  `last_error`, `sessions`, `limits`, and the last 20 lines of `kernel.log`. The counters
  reset when the kernel restarts; `counters_since` says since when.
- Sessions are never evicted; use `drop` to free one (it refuses sessions with a
  running job). Use `reset` to empty a namespace but keep the session.
- Several Pi sessions/subagents share one kernel: their jobs queue on it, and it is
  stopped only when the last of them exits (a subagent finishing cannot kill your
  run).
- Long jobs: set `timeout_s` generously in the first `exec`. Long waits are
  safe: `exec`, `poll`, `run_file` and `notebook` emit
  `notifications/progress` every 30 s while they wait, and a progress
  notification resets the client's per-request timeout (pi's `mcp.json` sets
  `"timeout": 600` for `sage`; `SAGE_MCP_PROGRESS_SLICE` changes the interval if a
  client's own timeout is shorter than that). A client that asked for no progress token gets
  the same calls as before, so a wait beyond that timeout still wants
  `timeout_s=0` plus `poll`.
- To verify that state was really lost (after `interrupt(force=true)`), test `'name' in globals()`:
  short names such as `n`, `N`, `e`, `i` and `pi` are Sage predefines (`N` is
  `numerical_approx`), so a `NameError` probe can resolve to an unrelated
  function and lie.
- The kernel preparses by default, so **integer literals are Sage `Integer`s,
  not Python `int`s**. Python's `statistics.mean`, `variance`, `stdev`,
  `pstdev`, `harmonic_mean` and `quantiles` then fail with `TypeError:
  unsupported operand type(s) for *: 'method' and 'method'` -- Sage exposes
  `numerator`/`denominator` as *methods* and `fractions.Fraction` multiplies
  them. Pass Python ints (`statistics.mean([int(x) for x in data])`) or use
  `fmean`, `median`, `mode` or `geometric_mean`, which never build a Fraction;
  `preparse=false` avoids it too but loses `2^10` and `R.<x>`.
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
