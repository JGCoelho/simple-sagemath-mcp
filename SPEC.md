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
                        │
                        └── reads and writes .ipynb files on disk
```

- **The server** is a FastMCP application that speaks JSON-RPC over stdio. It
  owns one `KernelManager`, keeps no state of its own across restarts, and
  writes notebooks atomically.
- **The kernel** is a real Jupyter kernel running the python of the Sage
  installation. It holds one namespace (`__mcp_ns__`) for the life of the
  connection. All computation happens here, never in the server process.
- **Files on disk** are the only durable output. `run_file` runs a `.py` or
  `.sage` file that already exists. The deprecated notebook tools read and write
  `.ipynb` files, and `nb_run` writes results into one. The server never stores
  results elsewhere.

The server imports Sage (`sage.repl.preparse`) for preparsing but never imports
`sage.all`: only the kernel pays that cost. `import sagemcp` therefore stays
cheap, and the server can be started by any python that has `fastmcp` and
`nbformat`. It does need a Sage-backed python for `preparse`, which is why
`install.sh` derives the server interpreter from `sage` itself.

The tools are thin wrappers: resolve arguments, take the right lock, call one of
a few shared functions, and shape the result into the output schema. Every tool
returns through exactly one builder (`_exec_out`, `_nb_run_out`, `_restart_out`,
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
check a Sage-less kernel would be accepted as ready, `restart` would claim
`restarted: true`, and `nb_run` would write the import traceback into the
notebook as the outputs of its first cell.

### Packaging

`pyproject.toml` installs the `sagemcp` package under `src/`
(`package-dir = {"" = "src"}`, `packages.find where = ["src"]`), one module per
concern: kernel lifecycle and execution, notebook I/O, notebook execution, output
shaping, the error envelope and schemas, the FastMCP app, and one module per tool
group. The root `server.py` is not installed; it is the entry point for a
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
version. The remaining dependencies (`nbformat`, `jupyter_client`,
`ipykernel`) are the notebook and kernel APIs the code imports directly.
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
        display({mime bundle}, raw=True, metadata={"_mcp_trailing": True})
else:
    exec(compile(_mcp_tree, filename, "exec"), ns, ns)
```

Three consequences:

- **The trailing expression is evaluated separately**, so `x = 6` followed by
  `x*7` in one source returns `42`, exactly as a notebook's last line would.
- **The result is published as a mime bundle**, via `IPython.display.display(...,
  raw=True)`: `text/plain` is `repr(value)`, plus `text/latex` from `_latex_()`
  and `text/html` from `_html_()` when the object has them. Matrices,
  polynomials, and graphs therefore render as real LaTeX in a notebook viewer,
  and the mime bundle is the same shape Jupyter itself would store.
- **The filename is the caller's.** `run_file` passes the file's path, so a
  traceback names the file. `exec` passes `<exec>`, and a notebook cell passes
  `<path>:<cell id>`.

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
  kernel sees anything, so a broken cell cannot poison a `nb_run` half-way
  through and no kernel state is touched.
- **One static check shared by everything.** `exec`, `run_file`, `nb_write`,
  `nb_edit`, and `nb_run` validate with the same `_prepare`, so a cell that passes validation
  fails at the same place when it actually runs.
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
place a kernel executes code. Its call sites are the namespace seed, the job body
that `exec` and `run_file` share (`_run_locked`, §10), and each cell of `nb_run`.
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
| wait budget | `KERNEL_READ_TIMEOUT` = 2400 s | the default `timeout` of `exec` and `run_file`: how long the call waits, not how long its job may run. A notebook cell gets the same figure as its own limit |
| shell-reply drain | `SHELL_DRAIN_TIMEOUT` = 30 s | after the idle message |
| inline payload | `MAX_INLINE_CHARS` = 20000 | the tool response |

`_truncate` charges the budget to `result`, then `stdout`, then `stderr`; a
field is cut only when the earlier fields left budget, and a cut field gets a
`… [truncated N characters]` marker that is not itself counted. A
`truncated: true` is final: the untruncated text is not kept anywhere on the
server, which is why the README says to keep calls small.

`nb_run` truncates **by whole outputs**, not by characters: the first output
that would exceed the budget clears that cell's outputs and every later cell's
outputs and sets `truncated: true`. That keeps the inline view valid,
nbformat-shaped JSON (a half-cut mime bundle would not be), and `nb_read`
recovers the rest from disk. **On disk nothing is ever truncated.**

While a call runs, `_locked` reports progress every `HEARTBEAT_INTERVAL` (15 s)
through `ctx.report_progress`, so a computation that lasts minutes does not look
like a hung connection to the host.

A call that outlives its wait is not a kill. `exec` and `run_file` start a job
(§10) and wait at most their `timeout`. If the job is still running then, the
call returns `status: "running"` with `job_id`, `elapsed`, and the output so far.
The job keeps the kernel and the namespace until it ends. `job_status` reports it
until then, `interrupt` stops it, and `restart` ends it as `interrupted`. The job
itself has no budget.

A notebook cell still has one. When a cell exceeds `KERNEL_READ_TIMEOUT`,
`_run_source` raises `ExecTimeout`, and the kernel **keeps computing**. Any partial
outputs travel with the exception (`ExecTimeout.outputs`), so `nb_run` can still
write what the cell printed, and the run ends with `cell_execution_failed`.
`interrupt` is the way out; `restart` is the hard way.

---

## 8. Notebook rules

The notebook tools are deprecated (§12): they are served only with
`--legacy-notebook-tools`. The rules in this section are theirs.

### Load (`_load_nb`)

Reads **bytes** and decodes explicitly, then:

1. `json` → `nbformat.reads(text, as_version=4)` → `nbformat.validate`;
2. pre-4.5 notebooks get ids **in memory** (the file is not modified by
   `nb_read`). The ids are deterministic (`cell-<index>`, see `_legacy_id`):
   random ids would make the documented `nb_read` → `nb_edit` chain fail with
   `unknown_cell_id` on the second call;
3. ids must be present and unique → `invalid_notebook` naming the offender
   (addressing is by id, so this is load-bearing);
4. every cell's text must encode as UTF-8 → `invalid_notebook`. A lone surrogate
   from a JSON `\uD800` escape is legal JSON and legal nbformat but cannot cross
   the JSON-RPC response; refusing the notebook is the only way to answer
   without killing the connection;
5. the **on-disk** `nbformat`/`nbformat_minor` are returned, read from the raw
   JSON (4.4 → `4`,`4`; 4.0 → `4`,`0`; v3 → `3`,`0`), even though the cells
   carry upgraded in-memory ids.

### Validate (`_validate_cells`)

Per **code** cell, in order: reject `%`/`!` as the first non-space character of
any line (`unsupported_magic`, `details = "<cell_id>: <line>"`), then run
`_prepare` (`invalid_source`, same detail shape). Markdown and raw cells are
never validated as code.

### Atomic write (`_save_nb`)

One implementation with three callers (`nb_write`, `nb_edit`, `nb_run`):

1. resolve a symlink first, so the **real** file is replaced and the link
   survives (a plain `os.replace` on the link path would replace the link);
2. the parent directory must already exist — never created;
3. `tempfile.NamedTemporaryFile(dir=parent, suffix=".tmp", delete=False)` — a
   temp in the **same directory**, so `os.replace` is atomic (same filesystem);
4. `nbformat.write` at 4.5, `flush`, `os.fsync`;
5. `os.chmod(tmp, mode of the file being replaced, else 0644)` — a fresh temp is
   0600, and without this every write would silently narrow a 0644 notebook;
6. `os.replace(tmp, target)`; on any failure unlink the temp and raise
   `write_failed`.

The guarantee that follows: a reader (or a crash) sees either the old file or
the new file, never a partial one. `nb_edit` additionally applies **all** ops and
re-validates the whole result in memory before writing anything, so `applied ==
[]` and the file is byte-identical whenever the call fails.

---

## 9. `nb_run` semantics

`nb_run` is "run this notebook the way a user would", so:

1. **Validate before touching anything.** Load and validate the file first. A
   validation failure returns `invalid_notebook`, `unsupported_magic`,
   `invalid_source`, or `not_found` with `saved: false`, and the kernel is
   **not** restarted — a typo in a notebook must not cost the agent its session.
2. **Restart the kernel**, so the run is reproducible and not contaminated by
   earlier `exec` calls.
3. Execute code cells in order, with `execution_count` continuing from the
   largest count already in the file + 1 (the Jupyter convention for appending
   cells).
4. **Stop at the first cell that raises.** Write the notebook with the outputs
   through that cell (`saved: true`, `cell_execution_failed`, `cells` = the
   cells that ran); later cells keep whatever was on disk. Work already done is
   never thrown away. The same holds for a cell that exceeds the kernel budget
   (partial outputs are kept) and for kernel death mid-run (`kernel_died` with
   the completed cells written).
5. Otherwise write once, return the cells with their outputs, and **leave the
   kernel running and warm** with the notebook's variables, so the agent can
   continue interactively after a run.

A notebook with no code cell is not written (`saved: false`, `cells: []`), and
`nb_run` never creates a file: only `nb_write` does.

---

## 10. Concurrency

Two `asyncio.Lock`s, and a strict order between them.

**`_nb_lock`** serializes notebook *file mutations*. `nb_run` holds it across
load → validate → run → write; `nb_write` and `nb_edit` hold it for their whole
call. Without it, a `nb_edit` that returned `saved: true` could be silently
reverted by a `nb_run` that had loaded the file before the edit and wrote its
stale copy afterwards. This was a real bug found in review, and the lock is the
fix.

**`_kernel_lock`** serializes *computations*. `nb_run` holds it for the whole run
(through `_locked`, which also emits heartbeats). An `exec` or `run_file` job holds
it from `_run_job` until the job ends, which can be after the call that started it
has returned (below). Kernel helpers are synchronous and assume the caller holds
it; only `_locked`, `_run_job` and `_restart` acquire it.

**Order is always notebook lock, then kernel lock.** `nb_run` is the only tool
that takes both, and it takes them in that order; nothing takes them the other
way, so the two cannot deadlock.

**Jobs never queue.** `exec` and `run_file` start a job: `kernel._start_job`
registers its record (`jobs.py`) and starts a task that holds the kernel lock for
the whole run. `_claim` checks, with no `await` before the job is registered,
whether a job or another holder has the kernel. If one has, the call gets
`kernel_busy` with the holder's job id, and its work does not start. The notebook
tools check the same way, including `nb_read` and `nb_write`, which never touch
the kernel. A job finishes its record before it releases the lock, so a
`job_status` that sees the job end sees its final envelope. The last
`JOB_RETENTION` finished records are kept; the running job is never dropped.
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
| kernel dies mid-`nb_run` | same | `kernel_died`, completed cells written | as above |
| kernel cannot start | `wait_for_ready` / start exception | `kernel_died` ("did not start") | kernel stopped and forgotten |
| kernel cannot import Sage | seed outputs contain an `error` | `sage_not_found` with the last traceback line | kernel stopped |
| no `sage` at all | `resolve_kernel_python` | `sage_not_found` | unchanged |
| code raises | `error` output | `cell_execution_failed` | namespace mutated as Python left it |
| code is invalid Sage/Python | `_prepare` | `invalid_source` | kernel untouched |
| a call outlives its wait | `timeout` expires | `status: running` with `job_id` | the job keeps the kernel; poll `job_status`, or `interrupt` or `restart` |
| a call while a job holds the kernel | `_claim` | `kernel_busy`, `details` names the job | refused, not queued; the job runs on |
| a notebook cell outlives `KERNEL_READ_TIMEOUT` | `ExecTimeout` in `_run_source` | `cell_execution_failed`, partial outputs kept | kernel still computing, `busy` stays set |
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
`details` holds a traceback, an offending cell id, an op index, or `null`. The
three writing tools add `saved` (true = this call wrote the file; a no-op edit
still writes, so `saved` stays truthful).

`exec` and `run_file` add `status`, `job_id` and `elapsed` only while the call is
still running (`status` is then `"running"`). A finished call has the five base
keys. `job_status` reports a job's `status`, `elapsed` and output, and its
`result` once the job has finished.

The schemas are declared **per tool** as `output_schema` dicts, because FastMCP
enforces them strictly (verified against 4.1.3): the payload must validate, and
`error` must be `["object", "null"]` with a per-tool `enum` of the codes that
tool can actually produce. An enum member that is unreachable is removed rather
than documented (e.g. `interrupt` cannot report `kernel_died`, since it never
takes the lock and never starts anything). `saved` is `required` on
`nb_run`/`nb_write`/`nb_edit`, and `additionalProperties: false` everywhere, so a
tool that forgets a field fails at development time instead of surprising a
client.

One deviation is forced by FastMCP: it has no `input_schema=` override, so input
schemas are derived from the function signatures. `cells` and `ops` are therefore
`list[dict[str, Any]]` (validated in the wrapper, `invalid_op` on a malformed
shape) rather than the more precise `oneOf` shape. The output schemas are not
affected.

### The codes

| Code | Meaning |
|------|---------|
| `invalid_source` | preparse, parse, or compile failed; for `run_file`, also a suffix other than `.py` or `.sage`, or bytes that are not UTF-8 (`exec`, `run_file`, `job_status`, `nb_run`, `nb_write`, `nb_edit`) |
| `file_not_found` | the `run_file` path is not a file (`run_file`) |
| `cell_execution_failed` | the code raised, including `KeyboardInterrupt` and a GAP abort (`exec`, `run_file`, `job_status`, `nb_run`) |
| `gap_crash_loop` | GAP crashed three times in one call; the call was interrupted (`exec`, `run_file`, `job_status`) |
| `kernel_busy` | another job or a notebook run holds the kernel; the call is refused, not queued, and `details` names the job (`exec`, `run_file`, `nb_run`, `nb_write`, `nb_read`, `nb_edit`) |
| `unknown_job` | no job has this id: it never existed, its record was dropped, or the server restarted (`job_status`) |
| `kernel_died` | the kernel died; the namespace was lost, not restarted automatically (`exec`, `run_file`, `job_status`, `nb_run`, `restart`) |
| `sage_not_found` | no usable Sage from `SAGE_BIN`, `sage` on `PATH`, or the sibling `sage` (`exec`, `run_file`, `job_status`, `nb_run`, `restart`) |
| `not_found` | the notebook file does not exist (`nb_run`, `nb_read`, `nb_edit`) |
| `exists` | the target exists and `overwrite` is false (`nb_write`) |
| `invalid_notebook` | not JSON, not schema-valid, a bad cell id, or text that is not UTF-8 (`nb_run`, `nb_write`, `nb_read`, `nb_edit`) |
| `unsupported_magic` | a `%` or `!` line in a code cell (`nb_run`, `nb_write`, `nb_edit`) |
| `unknown_cell_id` | the id is not in the notebook (`nb_read`, `nb_edit`) |
| `invalid_op` | an op is malformed or impossible beyond an id lookup (`nb_edit`) |
| `write_failed` | the temp write or the rename failed; the file is untouched (`nb_run`, `nb_write`, `nb_edit`) |
| `internal` | unexpected failure; `details` holds `Type: message` (all) |

There is no `timeout` code: a call that outlives its wait returns `status: running` (§7).

A code is in a tool's `enum` only when that tool can return it, so this table is
a union: `gap_crash_loop` is `exec`-, `run_file`- and `job_status`-only;
`file_not_found` is `run_file`-only; `unknown_job` is `job_status`-only; `exists`
is `nb_write`-only; `invalid_op` is `nb_edit`-only; and `sage_not_found` comes from
the calls that start a kernel (`exec`, `run_file`, `job_status`, `nb_run`, `restart`).

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
existed, a record that was dropped, and every id after a server restart.

### `nb_run(path)` (deprecated)
Validates first (JSON, schema, unique ids, no magics, every cell preparses); a failure returns
`invalid_notebook`, `unsupported_magic`, `invalid_source`, or `not_found` with `saved:false`, and
the file stays byte-identical. Then it **restarts the kernel**, runs cells in order with
`execution_count` continuing from the largest existing count, and stops at the first cell that
raises, writing outputs through that cell and returning `cell_execution_failed` with `saved:true`.
Otherwise it writes once and leaves the kernel warm.

A notebook with no code cell is not written (`saved:false`), and `nb_run` never creates a file.
Inline outputs are cut at 20000 characters **by whole output**; on disk nothing is truncated.
Results keep their mime bundles: `text/plain`, plus `text/latex` and `text/html` when the object
provides `_latex_()` / `_html_()`.

### `nb_write(path, cells, overwrite=false)` (deprecated)
Creates a notebook from `[{cell_type, source}]`, with the Sage kernelspec, at nbformat 4.5. Every
cell gets an id, returned in `cell_ids` ready for `nb_edit`. The whole notebook is validated before
the write. An existing file without `overwrite:true` returns `exists`; the parent directory must
exist.

### `nb_read(path, cell_ids=None)` (deprecated)
Reads from disk only: no write, no execution, no kernel. Without `cell_ids` it returns all cells in
order; with them, exactly those cells in the order given, and one unknown id fails the call with
`unknown_cell_id`. Each cell is `{cell_id, cell_type, source}`; code cells carry their full
`outputs`.

### `nb_edit(path, ops)` (deprecated)
Ops are `{"op": "edit", "cell_id", "source"}`, `{"op": "add", "source", "cell_type": "code",
"after"}`, and `{"op": "remove", "cell_id"}`. `add` defaults to code, appends when `after` is
omitted, and returns its new id in `applied`; `remove` may remove the only cell. The ops are applied
in memory and the **whole result** is re-validated before one write. On failure nothing is written:
`applied:[]`, `saved:false`, and `error.details` starts with `op <zero-based index> (<op>):`.

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
- `nb_read` reports the **on-disk** nbformat versions and the **full** on-disk
  outputs; `nb_run`'s inline view is the truncated one.
- `nb_edit` returns the new ids from `add` in `applied`, in op order, so the
  caller can address the cell it just created.

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
  and the notebook output format for free; a hand-made pipe loses all three.
- **No magics or `!` shell escapes** in notebook cells. They are rejected with
  the cell id. Supporting them would mean running IPython's input transformers in
  the kernel and re-implementing its state, for a feature that mostly duplicates
  `exec`.
- **No sandbox.** The kernel runs agent-supplied code with the user's
  permissions. That is documented in the README and is a hard boundary, not an
  oversight.

---

## 14. Tests

`unittest`, no `pytest` requirement, no new dependencies. `test/_mcp.py` starts
**one server process per test run**, with the notebook flag (the nb tests share a kernel with `exec`), and offers `call()` / `call_async()`, `call_raw()`, `wait_job()` and `settle()`, plus `unwrap()`, so each test reads like a tool call. `Server()` starts a second process with other arguments.

| file | kernel | covers |
|---|---|---|
| `test/_mcp.py` | — | shared client (started with `--legacy-notebook-tools`), sync/async helpers, `call_raw`, `wait_job`, `settle`, `Server` for a second process |
| `test/test_sagebin.py` | no | `SAGE_BIN` over `PATH`, sibling python, `sage -c` fallback, not-found message, `PATH` prefix, seed/start failures (mocked) |
| `test/test_exec.py` | yes | values, preparse, persistence, runtime/syntax errors, split streams, timeout then interrupt, restart while busy, interrupt idle/busy, truncation, GAP guard, `display` is not the result, output shape |
| `test/test_jobs.py` | yes | running envelope and partial output, `job_status`, `kernel_busy` without queueing, `unknown_job`, interrupt and restart reaching a running job, finished-record retention (no kernel) |
| `test/test_run_file.py` | yes | `.py` and `.sage` values, preparse, persistence, missing file, bad suffix, bad UTF-8, BOM, syntax error, traceback names the file, relative path against `--directory` (a second server), `kernel_busy`, a long file polled to `done`, interrupt |
| `test/test_tool_list.py` | no | default `tools/list` names and order; the four legacy tools only with the flag, after `restart` |
| `test/test_nb_edit.py` | no | legacy tools: load/validate, UTF-8 and legacy-id rules, `nb_write`, `nb_read` selection and disk versions, op order, atomicity and hash equality, mode and symlink preservation |
| `test/test_nb_run.py` | yes | legacy tools: restart first, disk vs inline outputs, stop-at-error with `saved:true`, validation failure without restart or write, zero code cells, inline-only truncation, a concurrent edit is not lost |
| `test/test_packaging.py` | no | `pyproject.toml` metadata, `main(["--help"])` and `--directory`, the sibling-`sage` resolution fallback, and the `requires-python` floor re-derived from `src/sagemcp/*.py` |
| `test/test_tool_docs.py` | no | each default tool's `Errors:` docstring line equals its `outputSchema.error.properties.code.enum`; the deprecated nb tools are checked the same way, and their summaries say DEPRECATED |
| `test/test_long_exec.py` | yes | opt-in (`SAGE_MCP_LONG=1`): a ~50 s `exec` returns its exact value, so a long call survives without a disconnect |

Kernel-free where possible: anything that needs an interpreter is mocked, and
the notebook tools are tested through the MCP surface anyway, so their file
semantics are covered without paying 2 s per test. Every file except
`test_long_exec.py` finishes in under 30 s; the long test is the only opt-in.

---

## 15. Known limitations

- **`interrupt` while a call's wait has expired** can report `interrupted: true`
  for a computation that has since finished (§10). Documented, not fixed.
- **Cancellation** cannot stop a worker thread mid-read (§10). Bounded by the
  drain/read timeouts.
- **Input schemas** for `cells`/`ops` are looser than the plan's `oneOf` (§12),
  enforced in the wrapper instead.
- **One computation at a time** per connection. That follows from the one-kernel
  design and is the trade the whole server is built on.
- **`sage_not_found` for `nb_write`/`nb_edit`** is reported as `internal`: those
  tools never start a kernel, so the code is unreachable there.

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
  unrecoverable; `nb_run` truncation is recoverable with `nb_read`. When the
  inline budget is exceeded, the overflowing code cell and every later code
  cell come back as `outputs:[]`. A host may also truncate a very large
  `nb_read` result; the full text is then in the file the host names.
- Plot recipe: full form in `sage_exec`'s docstring; short form in
  `sage_nb_run`'s (nb_run writes to disk; the recipe still applies). `Image` is
  seeded into the kernel namespace, so `display(Image(filename='p.png'))` works
  without an extra import. A relative `P.save` path resolves against the kernel
  cwd, not the notebook's directory.
- `nb_edit`'s `add` op takes `after` as an existing cell id (omit it to append)
  and takes no `cell_id`. `nb_write` with `overwrite:true` re-mints all cell ids.
- `nb_write.cells` item schema now declares `{cell_type, source}`. Keep it in
  sync with the implementation's `extra = set(spec) - {"cell_type","source"}`.
- `nb_edit.ops` deliberately has no item schema; per-op shapes differ.
- Size is measured by `tools/measure.py`, which reports the wire size
  (`name` + `description` + `inputSchema` + `outputSchema`) and the direct-model
  size (`name` + `description` + `inputSchema`). The figures that were recorded
  here predate `run_file` and the deprecation; re-run `tools/measure.py` for the
  current surface (the default surface is wire 11,592 chars, direct-model 5,864).
  The increase is deliberate: the `Errors:` lines, the `nb_edit`
  clarifications, and the `P.save` cwd note carry facts that direct callers
  cannot get from `outputSchema`. Do not trade them away to hit a wire-size
  target.

---

## Constants

Name-only index; the values live in `src/sagemcp/constants.py` next to the
comment that explains them.

- `KERNEL_READ_TIMEOUT`
- `JOB_RETENTION`
- `MAX_INLINE_CHARS`
- `GAP_CRASH_LIMIT`
- `SHUTDOWN_TIMEOUT`
