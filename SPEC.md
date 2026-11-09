# Architecture

How `simple-sagemath-mcp` works inside. This is a design document for readers
who want to understand, audit, or change the server. If you only want to
install it and use the tools, read [README.md](README.md) instead.

Everything here is checkable against the `sagemcp` package under `src/`, which is
the single source of truth; the root `server.py` is only the checkout entry
point into it. There is no daemon and no runtime configuration beyond
`SAGE_BIN`.

---

## 1. Shape of the system

Three processes are involved:

```
MCP host  ──stdio──>  sagemcp/  ──Jupyter protocol──>  ipykernel (Sage python)
```

- **The server** is a FastMCP application that speaks JSON-RPC over stdio. It
  owns one `KernelManager` and keeps no state of its own across restarts.
- **The kernel** is a real Jupyter kernel running the python of the Sage
  installation. It holds one namespace (`__mcp_ns__`) for the life of the
  connection. All computation happens here, never in the server process.
- **Files on disk** are only read: `run_file` runs a `.py` or `.sage` file that
  already exists. The server writes no files.

The server imports Sage (`sage.repl.preparse`) for preparsing but never imports
`sage.all`: only the kernel pays that cost. `import sagemcp` therefore stays
cheap, and the server can be started by any python that has `fastmcp` and
`jupyter_client`. It does need a Sage-backed python for `preparse`, which is why
`install.sh` derives the server interpreter from `sage` itself.

The tools are thin wrappers: resolve arguments, take the right lock, call one of
a few shared functions, and shape the result into the output schema. Every tool
returns through exactly one builder (`_exec_out`, `_job_status_out`, `_restart_out`,
…), so the envelope cannot drift between tools.

---

## 2. Why one persistent kernel

A fresh Sage process costs about 1.7 s and 200 MB before it answers `2+2`, and
the cost repeats for every operation. An agent that loads a 10 MB graph, inspects
it, computes its automorphism group, and then prints one vertex would pay that
cost four times, and would have to reload the graph each time.

The alternative — one kernel per call — is simpler and stateless, but it makes
iterative work (load, poke, load again, compare) either slow or impossible. It
also can't offer `interrupt` or `restart`, which are the two operations a user of
a long-running computation actually needs.

So the design is: **one kernel, one namespace, started lazily, kept warm**.
`restart` exists precisely so the state is explicit and can be wiped on demand.
Named sessions were removed deliberately (see §13).

---

## 3. Finding Sage and launching the kernel

`resolve_kernel_python()` returns `(python, bin_dir)`:

1. `SAGE_BIN` if set, else `shutil.which("sage")`, else the sibling `sage`
   of `realpath(sys.executable)` when that file exists. None → `SageNotFound`.
   The third source exists because an installed console script lives in Sage's
   `bin`, so it still finds Sage when the host hides `PATH`. It locates the
   *launcher* only; the kernel python is still derived from the launcher's bin
   directory, never from `sys.executable`.
2. `bin_dir = dirname(realpath(sage_bin))`.
3. Try `bin_dir/python3`, then `bin_dir/python`. A sibling interpreter is the
   normal layout of conda and of the official Sage tarball.
4. Otherwise ask the launcher: `sage -c "import sys; print(sys.executable)"`,
   keeping the **last non-empty line** (the launcher prints warnings first).
5. If that does not name an existing file → `SageNotFound`.

**It never falls back to `sys.executable`.** A kernel started by a non-Sage
python looks healthy, accepts code, and then fails every Sage import at the first
user call — the worst possible failure mode. A hard `sage_not_found` at start
time is strictly better.

The kernel is started with an injected `KernelSpec`:

```python
KernelSpec(argv=[python, "-m", "ipykernel_launcher", "-f", "{connection_file}"],
           display_name="SageMath", language="sage")
```

injecting the spec instead of relying on `python3` from the kernelspec search
path removes any dependence on what `jupyter kernelspec list` happens to
contain, and makes the resolved interpreter the only thing that decides which
python runs.

`_kernel_env(bin_dir)` copies the process environment and puts `bin_dir` **first
in `PATH`**. This is not cosmetic: Sage shells out to Singular, `gp`, `ecl`, and
friends, which live next to the `sage` executable. Without the prefix, a conda
Sage fails with "Singular not found" the first time it touches a polynomial ring.

Startup sequence (`_start_kernel`), under the kernel lock:

1. resolve the interpreter and start the kernel;
2. `start_channels()`, `wait_for_ready(KERNEL_READY_TIMEOUT)`;
3. **seed the namespace** (below);
4. on any failure, stop the kernel and re-raise — a half-started kernel must not
   survive, or the next call would use dead channels.

### Seeding at start, not on first use

The namespace is seeded by running an empty source through the normal execution
path, with `from sage.all import *` injected by the wrapper. That is a deliberate
choice with a subtle reason: importing `sage.all` takes ~1.5 s, and during an
import **CPython defers signals**. A `SIGINT` sent to a kernel that is inside
that import is not delivered until the import finishes, so a cold first call
would ignore `interrupt` and force `restart` down its hard-kill path. Seeding at
startup means every user call is interruptible.

The seed is also the install check: its stderr is inspected, and an error means
the kernel python cannot import Sage. That raises `SageNotFound` (a broken
install, not a dead kernel), and `_start_kernel` stops the kernel. Without that
check a Sage-less kernel would be accepted as ready, and `restart` would claim
`restarted: true`.

### Packaging

`pyproject.toml` installs the `sagemcp` package under `src/`
(`package-dir = {"" = "src"}`, `packages.find where = ["src"]`), one module per
concern: kernel lifecycle and execution, job records, output shaping, the error
envelope and schemas, the FastMCP app, and one module per tool group. The root `server.py` is not installed; it is the entry point for a
checkout and does nothing but put `src/` on `sys.path` and call
`sagemcp.app.main`. `[project.scripts]` maps
`simple-sagemath-mcp = "sagemcp.app:main"`, and `main(argv=None)` parses
`sys.argv[1:]`, changes to `--directory` if given, and calls `mcp.run()`.
Installed, `python -m sagemcp` runs the same `main`; `python -m server` also
works, but only inside the checkout, where the repo root is on `sys.path`.

`requires-python` is `>=3.10`. `ast.parse(feature_version=(3, 9))` accepts the
source, but the annotations use PEP 604 unions (`str | None`) without
`from __future__ import annotations`, so importing on 3.9 raises `TypeError`;
`test/test_packaging.py` re-derives the floor from every module under
`src/sagemcp/`. `fastmcp` is pinned to `>=4.1,<5` because the strict `output_schema`
enforcement that the common error envelope depends on may change in a major
version. The remaining dependencies (`jupyter_client`, `ipykernel`) are the kernel
APIs the code imports directly.
The install runs on Sage's own interpreter (`sage -pip install .`): the server
imports `sage.repl.preparse` for preparsing, and the kernel imports `sage.all`.

---

## 4. The namespace and the wrapper

The kernel holds one dict, `__mcp_ns__`, created by the wrapper on first use.
User code is `exec`'d with that dict as both globals and locals, so variables,
imports, and mutations survive between calls without any serialization.

Each call sends a generated wrapper, not the user's text:

```python
_mcp_tree = ast.parse(<preparsed source>)
if _mcp_tree.body and isinstance(_mcp_tree.body[-1], ast.Expr):
    last = ast.Expression(_mcp_tree.body.pop().value)
    exec(compile(_mcp_tree, filename, "exec"), ns, ns)
    value = eval(compile(last, filename, "eval"), ns, ns)
    if value is not None:
        display({"text/plain": repr(value)}, raw=True, metadata={"_mcp_trailing": True})
else:
    exec(compile(_mcp_tree, filename, "exec"), ns, ns)
```

Three consequences:

- **The trailing expression is evaluated separately**, so `x = 6` followed by
  `x*7` in one source returns `42`, as in an interactive session.
- **The result is published as a mime bundle**, via `IPython.display.display(...,
  raw=True)`, with `text/plain` set to `repr(value)`. Only that text reaches the
  caller (§6).
- **The filename is the caller's.** `run_file` passes the file's path, so a
  traceback names the file. `exec` passes `<exec>`.

The `_mcp_trailing` metadata key is the contract between the wrapper and the
collector: only a display carrying that key becomes an `execute_result`; every
other one stays a `display_data` (§6).

---

## 5. Server-side preparse

Sage syntax is not Python: `^` is a power, `1/2` is a rational,
`R.<x> = PolynomialRing(QQ)` declares a generator. `sage.repl.preparse`
translates it. Running the translation in the **server** rather than in the
kernel buys three things:

- **Early rejection.** A syntax error surfaces as `invalid_source` before the
  kernel sees anything, so no kernel state is touched.
- **One static check.** `exec` and `run_file` both validate with `_prepare`, so a
  source that passes validation fails at the same place when it actually runs.
- **The kernel stays a dumb interpreter of valid Python**, and the wrapper only
  has to parse an already-valid tree.

`_prepare` also does `ast.parse` + `compile` to catch what `preparse` lets
through, maps `SyntaxError` to a message with the line number, and maps
`UnicodeError` to `invalid_source` (a lone surrogate can come from a JSON
`\uD800` escape and is not valid source).

Preparse is lazy-imported inside `_prepare`, so `import sagemcp` never imports
Sage. `_seed_namespace` passes `prepared=""` to skip it entirely, which keeps
package import and kernel start independent of the server's own Sage.

---

## 6. One execution path

`_run_source(state, source, filename, prepared=..., timeout=...)` is the only
place a kernel executes code. Its call sites are the namespace seed and the job
body that `exec` and `run_file` share (`_run_locked`, §10).
It:

1. marks `state["busy"] = True`;
2. `kc.execute(wrapper, allow_stdin=False)`;
3. reads iopub messages until the matching `status: idle`, collecting only
   messages whose `parent_header.msg_id` matches this call — a stale message
   from a timed-out earlier call can never leak into a later result;
4. classifies messages:
   - `stream` → `{output_type: "stream", name, text}` (stdout and stderr are
     kept as separate outputs and never merged);
   - `execute_result` / `display_data` → mime bundle, marked
     `execute_result` only if it carries `_mcp_trailing`;
   - `error` → `{output_type: "error", ename, evalue, traceback}`;
   - `status: idle` → done.
5. drains the shell reply (`_drain_shell`, bounded by `SHELL_DRAIN_TIMEOUT`) so
   the reply does not sit in the channel and confuse the next call;
6. returns the outputs **in order**.

The text view (`_summarise`, pure) is built from those outputs: `stdout` and
`stderr` are joined per stream, `result` is the `text/plain` of the last
`execute_result`, and `error` is `"Ename: evalue"` from the first error output.
Only `execute_result` can supply a `result`, so a `display(x)` inside
statement-only code never masquerades as the value of the call.

### GAP guard

GAP can crash into a restart loop and flood stderr while never going idle. The
reader counts stderr matches of `Gap crashed|Restarting Gap`; at
`GAP_CRASH_LIMIT` in one call it interrupts the kernel immediately, without
waiting for a shell reply (an interrupted GAP may never send one), and raises
`GapCrashLoop`, which becomes `gap_crash_loop` with the collected stderr in
`details`.

---

## 7. Budgets, truncation, and heartbeats

Three independent limits:

| limit | value | where |
|---|---|---|
| wait budget | `KERNEL_READ_TIMEOUT` = 2400 s | the default `timeout` of `exec` and `run_file`: how long the call waits, not how long its job may run |
| shell-reply drain | `SHELL_DRAIN_TIMEOUT` = 30 s | after the idle message |
| inline payload | `MAX_INLINE_CHARS` = 20000 | the tool response |

`_truncate` charges the budget to `result`, then `stdout`, then `stderr`; a
field is cut only when the earlier fields left budget, and a cut field gets a
`… [truncated N characters]` marker that is not itself counted. A
`truncated: true` is final: the untruncated text is not kept anywhere on the
server, which is why the README says to keep calls small.

While a call runs, `_wait_job` reports progress every `HEARTBEAT_INTERVAL` (15 s)
through `ctx.report_progress`, so a computation that lasts minutes does not look
like a hung connection to the host.

A call that outlives its wait is not a kill. `exec` and `run_file` start a job
(§10) and wait at most their `timeout`. If the job is still running then, the
call returns `status: "running"` with `job_id`, `elapsed`, and the output so far.
The job keeps the kernel and the namespace until it ends. `job_status` reports it
until then, `interrupt` stops it, and `restart` ends it as `interrupted`. The job
itself has no budget.

---

## 10. Concurrency

Two `asyncio.Lock`s, and a strict order between them.

**`_kernel_lock`** serializes *computations*. An `exec` or `run_file` job holds it
from `_run_job` until the job ends, which can be after the call that started it has
returned (below). Kernel helpers are synchronous and assume the caller holds it;
only `_run_job` and `_restart` acquire it.

**Jobs never queue.** `exec` and `run_file` start a job: `kernel._start_job`
registers its record (`jobs.py`) and starts a task that holds the kernel lock for
the whole run. `_claim` checks, with no `await` before the job is registered,
whether a job or another holder has the kernel. If one has, the call gets
`kernel_busy` with the holder's job id, and its work does not start. A job
finishes its record before it releases the lock, so a `job_status` that sees the
job end sees its final envelope.

Retention: the running job and the latest finished job. As each job ends,
`jobs.retire` drops every other finished record, so an older id is `unknown_job`.
Records are in memory only.

**`interrupt` takes neither lock**, by design: it must work while a call holds
the kernel lock. It reads and sets nothing but the `busy` flag, and sends
`km.interrupt_kernel()`. `busy` means "the kernel may be computing" — not "a
call is in flight". While a call's wait has expired, its computation is still
running, so the flag stays set until the job ends, the next completed call, or a
restart; a repeated `interrupt` in that window reports `true` and is harmless. Knowing the
kernel's real state would need a second iopub reader, which would race the call's
own reader and lose messages; the approximation is documented instead.

**`restart`** is the only operation that deliberately disturbs a running call. It
first marks the active job `interrupted`, so the job's record says so whatever its
computation returns:

1. `km.interrupt_kernel()` (a no-op if idle), so the busy call can notice;
2. `wait_for(_kernel_lock.acquire(), SHUTDOWN_TIMEOUT = 30 s)`;
3. if that expires, `km.shutdown_kernel(now=True)` — the in-flight reader sees a
   dead kernel, returns `kernel_died`, and releases the lock;
4. acquire again, again bounded. If *that* expires the call is stuck outside the
   kernel (draining, or preparing code) and `restart` reports `kernel_died`
   ("a running call is still using the kernel; try again") **without touching
   the kernel** — clearing state under a call that may still be using its
   channels is exactly how kernels get leaked;
5. `_stop_kernel` + `_start_kernel` **eagerly**, then clear `busy`.

Starting eagerly is deliberate: a restart that cannot produce a working kernel
must fail at `restart` time, when the caller asked for one, and not silently
leave a broken state for the next unrelated call.

Cancellation is a known limitation: cancelling an MCP tool call cancels the
`asyncio` task but cannot stop the worker thread in the middle of a kernel read.
The lock is released while that thread drains, bounded by the drain and read
timeouts. Fixing it properly needs a supervisor task that outlives the call.

---

## 11. Failure modes

| situation | detection | response | state afterwards |
|---|---|---|---|
| kernel process died | `km.is_alive()` in the read loop, or a channel error | `_died` → `kernel_died` | state cleared under the lock; next call starts a new kernel |
| kernel cannot start | `wait_for_ready` / start exception | `kernel_died` ("did not start") | kernel stopped and forgotten |
| kernel cannot import Sage | seed outputs contain an `error` | `sage_not_found` with the last traceback line | kernel stopped |
| no `sage` at all | `resolve_kernel_python` | `sage_not_found` | unchanged |
| code raises | `error` output | `cell_execution_failed` | namespace mutated as Python left it |
| code is invalid Sage/Python | `_prepare` | `invalid_source` | kernel untouched |
| a call outlives its wait | `timeout` expires | `status: running` with `job_id` | the job keeps the kernel; poll `job_status`, or `interrupt` or `restart` |
| a call while a job holds the kernel | `_claim` | `kernel_busy`, `details` names the job | refused, not queued; the job runs on |
| user cancels the tool call | `asyncio.CancelledError` | task ends | see §10, cancellation |
| anything else | `except Exception` in the tool wrapper | `internal` with `Type: message` | best effort |

An important rule: **no domain failure is raised to the client.** Every one is
converted into the error envelope. Only transport failures (a closed connection,
a protocol error) can make a tool call raise. That is what makes the schemas
declarable and the envelope uniform.

---

## 12. The tool contract

### The envelope

Every tool returns `{…payload…, "error": null | {"code", "message", "details"}}`.
`details` holds a traceback, an offending source fragment, or `null`.

`exec` and `run_file` add `status`, `job_id` and `elapsed` only while the call is
still running (`status` is then `"running"`). A finished call has the five base
keys. `job_status` reports a job's `status`, `elapsed` and output, and its
`result` once the job has finished.

The schemas are declared **per tool** as `output_schema` dicts, built in
`envelope.py` from shared fragments. A field such as `result`, `stdout` or `error`
is defined once, and each tool that returns it copies that definition, so the
tools agree by construction. A tool adds its own fields and its own `code` enum.
FastMCP enforces the schemas strictly (verified against 4.1.3): the payload must
validate, and `error` must be `["object", "null"]` with a per-tool `enum` of the
codes that tool can actually produce. An enum member that is unreachable is
removed rather than documented (e.g. `interrupt` cannot report `kernel_died`,
since it never takes the lock and never starts anything).
`additionalProperties: false` is set everywhere, so a tool that forgets a field
fails at development time instead of surprising a client.

### The codes

| Code | Meaning |
|------|---------|
| `invalid_source` | preparse, parse, or compile failed; for `run_file`, also a suffix other than `.py` or `.sage`, or bytes that are not UTF-8 (`exec`, `run_file`, `job_status`) |
| `file_not_found` | the `run_file` path is not a file (`run_file`) |
| `cell_execution_failed` | the code raised, including `KeyboardInterrupt` and a GAP abort (`exec`, `run_file`, `job_status`) |
| `gap_crash_loop` | GAP crashed three times in one call; the call was interrupted (`exec`, `run_file`, `job_status`) |
| `kernel_busy` | another job or a restart holds the kernel; the call is refused, not queued, and `details` names the job (`exec`, `run_file`) |
| `unknown_job` | no job has this id: it never existed, its record was dropped, or the server restarted (`job_status`) |
| `kernel_died` | the kernel died; the namespace was lost, not restarted automatically (`exec`, `run_file`, `job_status`, `restart`) |
| `sage_not_found` | no usable Sage from `SAGE_BIN`, `sage` on `PATH`, or the sibling `sage` (`exec`, `run_file`, `job_status`, `restart`) |
| `internal` | unexpected failure; `details` holds `Type: message` (all) |

There is no `timeout` code: a call that outlives its wait returns `status: running` (§7).

A code is in a tool's `enum` only when that tool can return it, so this table is
a union: `gap_crash_loop` is `exec`-, `run_file`- and `job_status`-only;
`file_not_found` is `run_file`-only; `unknown_job` is `job_status`-only; and
`sage_not_found` comes from the calls that start a kernel (`exec`, `run_file`,
`job_status`, `restart`).

### Per tool

What each tool promises, which is what its schema enforces.

### `exec(code, timeout=None)`
Returns `{"result", "stdout", "stderr", "truncated", "error"}`: `result` is the `repr()` of the
final expression (empty for statements or `None`), the streams are kept apart, and `truncated` means
the inline text was cut and is not recoverable. The call runs as a job (§10). `timeout` defaults to
2400 s and bounds how long the call waits, not how long the job runs. Past it, the call returns the
same keys plus `status: "running"`, `job_id` and `elapsed`, with the output so far. The kernel stays
busy and the namespace is kept. Poll `job_status` with the id, or call `interrupt` to stop it. Sage
syntax works: `3^2` is `9`, `1/2` is a rational, `R.<x> = PolynomialRing(QQ)` is a ring. A `!` shell
escape or a `%` magic is not Sage syntax, so it is `invalid_source`.

### `run_file(path, timeout=None)`
Runs a `.py` or `.sage` file in the namespace `exec` uses, and returns the same envelope, with the same
job behaviour: `timeout`, `running`, `job_status`, and `kernel_busy`. A relative `path` resolves
against the server's working directory (`--directory`); an absolute one is used as it is. Only the
exact suffixes `.py` and `.sage` are accepted. Any other suffix is `invalid_source`, decided before the
file is looked for; a missing file is `file_not_found`. The file is read once, when the call is made,
as UTF-8 with a leading byte-order mark dropped (`utf-8-sig`). That is a deliberate small deviation
from plain UTF-8, which would hand the BOM to the parser. Bytes that are not UTF-8 are
`invalid_source`. Both suffixes are preparsed, as `exec`'s source is. A traceback names the file,
because the file is compiled under its path. A syntax error reports its line but not the file name.

### `job_status(job_id)`
Reads a job that `exec` or `run_file` started. It never waits and never touches the kernel. `status`
is `running`, `done` (finished without an error result), `error` (finished with one), or
`interrupted` (a restart ended it). While the job runs, `stdout`, `stderr` and `elapsed` are the
output so far. Once it has finished, `result`, `stdout`, `stderr`, `truncated` and `error` are the
values its call would have returned. An unknown id is `unknown_job`. That covers an id that never
existed, an older finished job whose record was dropped, and every id after a server restart. Only
the running job and the latest finished job have records.

### `interrupt()`
Sends the interrupt request. `interrupted` is true when a computation may be running and the request
was sent, otherwise it is a no-op returning false. The namespace survives; the interrupted job ends
with `cell_execution_failed` and `KeyboardInterrupt` in `details`. While a call's wait has expired the
flag is approximate: it stays true until the job ends, so a repeated interrupt is harmless.

### `restart()`
Replaces the kernel. A running computation is interrupted first; if it does not stop within the
shutdown timeout, it is killed and respawned and the running call ends with `kernel_died`. A running
job's record reads `interrupted`, with `kernel_died` as its error.
`restarted` is true only when a new, ready kernel runs.

### Output-shape rules worth knowing

- `exec.result` and `run_file.result` are `repr()` of the trailing expression, `""` when there is none
  or it is `None`.
- `stdout`/`stderr` are separate strings, never concatenated.
---

## 13. Things deliberately not here

- **No sessions, no `list`, no `stop`.** A session is a second namespace with a
  second kernel and a name to keep in sync; it doubles every code path for a
  feature that `restart` already covers ("get me a clean namespace"). One
  kernel keeps `exec` predictable and the server single-lock.
- **No `server.json` / no `Config`.** MCP hosts sanitize the environment and many
  refuse extra files; hardcoded constants plus `SAGE_BIN` cover every real
  deployment. A config file that nobody can reach is worse than no config file.
- **No in-kernel preparse.** The kernel would have to import Sage before the
  first call could be validated, and a syntax error would be a kernel-side
  exception instead of a clean `invalid_source`.
- **No subprocess pipe protocol.** `jupyter_client` gives streams, interrupts,
  and the message protocol for free; a hand-made pipe loses all three.
- **No sandbox.** The kernel runs agent-supplied code with the user's
  permissions. That is documented in the README and is a hard boundary, not an
  oversight.

---

## 14. Tests

`unittest`, no `pytest` requirement, no new dependencies. `test/_mcp.py` starts
**one server process per test run**, and offers `call()` / `call_async()`, `call_raw()`, `wait_job()` and `settle()`, plus `unwrap()`, so each test reads like a tool call. `Server()` starts a second process with other arguments.

| file | kernel | covers |
|---|---|---|
| `test/_mcp.py` | — | shared client, sync/async helpers, `call_raw`, `wait_job`, `settle`, `Server` for a second process |
| `test/test_sagebin.py` | no | `SAGE_BIN` over `PATH`, sibling python, `sage -c` fallback, not-found message, `PATH` prefix, seed/start failures (mocked) |
| `test/test_exec.py` | yes | values, preparse, persistence, runtime/syntax errors, split streams, timeout then interrupt, restart while busy, interrupt idle/busy, truncation, GAP guard, `display` is not the result, output shape |
| `test/test_jobs.py` | yes | running envelope and partial output, `job_status`, `kernel_busy` without queueing, `unknown_job`, interrupt and restart reaching a running job, the retention rule (the class is kernel-free; a live check shows an older job is unknown) |
| `test/test_run_file.py` | yes | `.py` and `.sage` values, preparse, persistence, missing file, bad suffix, bad UTF-8, BOM, syntax error, traceback names the file, relative path against `--directory` (a second server), `kernel_busy`, a long file polled to `done`, interrupt |
| `test/test_tool_list.py` | no | `tools/list` names and order (the five tools), from the shared client and a second server |
| `test/test_packaging.py` | no | `pyproject.toml` metadata, `main(["--help"])` and `--directory`, the sibling-`sage` resolution fallback, and the `requires-python` floor re-derived from `src/sagemcp/*.py` |
| `test/test_tool_docs.py` | no | each tool's `Errors:` docstring line equals its `outputSchema.error.properties.code.enum` |
| `test/test_envelope.py` | no | output schemas: closed objects, each schema's keys equal its builder's keys, the code enums, shared fields defined once |
| `test/test_long_exec.py` | yes | opt-in (`SAGE_MCP_LONG=1`): a ~50 s `exec` returns its exact value, so a long call survives without a disconnect |

Kernel-free where possible: anything that needs an interpreter is mocked. Every file except
`test_long_exec.py` finishes in under 30 s; the long test is the only opt-in.

---

## 15. Known limitations

- **`interrupt` while a call's wait has expired** can report `interrupted: true`
  for a computation that has since finished (§10). Documented, not fixed.
- **Cancellation** cannot stop a worker thread mid-read (§10). Bounded by the
  drain/read timeouts.
- **One computation at a time** per connection. That follows from the one-kernel
  design and is the trade the whole server is built on.
---

## Tool-surface design (maintainer-only)

- Pi (and MCP hosts following the Code Mode philosophy) drops
  `FastMCP(instructions=...)`. Do not put agent-facing facts there. It also does
  not pass a tool's `outputSchema` to the model for direct calls: only `name`,
  `description`, and `inputSchema` are visible. Every fact the agent needs at
  call time must live in the docstring or an input-schema description.
  `outputSchema` remains authoritative for other MCP clients, codemode, and
  FastMCP's runtime validation; do not delete it, but do not rely on it for
  direct-call usability. Do not work around this with a first-call `exec`
  tutorial, a disguised prompt field, or `_hint` metadata; Pi minimizes
  server-pushed context by design.
- Each tool docstring ends with an `Errors:` line naming its `error.code`
  values. That line is duplicated from `outputSchema.error.properties.code.enum`
  because direct callers cannot see the latter; keep the two in sync.
- The error envelope is inlined per tool (not `$ref`): FastMCP strict validation
  needs no resolver. Do not switch to `$ref` without adding one.
- The per-tool `code` enums are operative. Do not remove, merge, or shorten.
- Truncation budget: `MAX_INLINE_CHARS = 20000`. `exec` and `run_file` truncation is
  unrecoverable.
- Plot recipe: full form in `sage_exec`'s docstring. A relative `P.save` path resolves
  against the kernel cwd, not the directory of any file.
- Size is measured by `tools/measure.py`, which reports the wire size
  (`name` + `description` + `inputSchema` + `outputSchema`) and the direct-model
  size (`name` + `description` + `inputSchema`). At this commit the five-tool
  surface is wire 12,037 chars and direct-model 5,797. Re-run `tools/measure.py`
  after a change. The growth is deliberate: the `Errors:` lines and the `P.save`
  cwd note carry facts that direct callers cannot get from `outputSchema`. Do not
  trade them away to hit a wire-size target.

---

## Constants

Name-only index; the values live in `src/sagemcp/constants.py` next to the
comment that explains them.

- `KERNEL_READ_TIMEOUT`
- `MAX_INLINE_CHARS`
- `GAP_CRASH_LIMIT`
- `SHUTDOWN_TIMEOUT`
