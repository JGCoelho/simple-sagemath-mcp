# sage-mcp

An MCP server that gives an agent a **persistent Sage** it can interrupt, poll
and kill, without ever hanging or orphaning a kernel. Nine tools: the kernel
itself (`exec`, `poll`, `reset`, `drop`, `status`, `interrupt`, `kill`) plus the
two file tools that need Sage's own environment — `run_file` (execute a file
the agent already wrote, inline results) and `notebook` (drive the Jupyter
kernel SageMath ships).

PDFs are a separate server, `pdf-mcp`.  MIT licensed; Python 3.7+, stdlib only.

Stdio JSON-RPC 2.0, one JSON object per line; every tool result is
`{"content":[{"type":"text","text":"..."}]}`, failures add `"isError": true`.
Nothing but JSON-RPC is ever written to the server's stdout.

## Tools

| tool | params (required → `*`) | returns |
| --- | --- | --- |
| `exec` | `code`*, `session`="default", `timeout_s`=120 (0 = return at once), `preparse`=true | stdout, then the repr of the last expression; `(no output)` if neither; on timeout the partial stdout + `[still running: 1.0s]` + `[job_id j7 — call poll ...]`; after a respawn the output is prefixed `[kernel restarted: ...]` |
| `poll` | `job_id`*, `since`=0, `wait_s`=0 | new stdout + `[job finished in 3.0s]` / `[still running 12.3s; poll again with since=57]` / `[queued: another job is running]` / the error |
| `reset` | `session`="default" | `session 'x' reset (namespace empty)` |
| `drop` | `sessions`=[…], `all`=false | `dropped: …` / `kept 'x': job j3 …` / `not found: …` / `remaining: …` |
| `status` | — | `kernel: running, up 12s (pid 4242)`, one `session <name>: N names` line per session, `active jobs: …`, `last error: …`, `log: <path>` |
| `interrupt` | — | `SIGINT sent …` (the job gets `KeyboardInterrupt`, the session keeps its variables) |
| `kill` | — | `kernel stopped; the next exec starts a fresh one …`, or the honest failure plus the pid |
| `run_file` | `path`*, `lang`=`auto`\|`bash`\|`python`\|`sage`\|`exec`, `args`, `timeout_s`=120, `stdin`, `cwd` | the head line (`exit=0  hello.sh  0.1s  bash hello.sh`), `cwd=` `lang=`, then `--- stdout ---` and, if any, `--- stderr ---` |
| `notebook` | `action`* ∈ `check`,`run`,`clear`,`extract`; `path`*, `out`, `in_place`=false, `cell_timeout_s`=120, `timeout_s` | `OK   <file> v4.5 kernel=sagemath cells=3 (code=2, with-output=0)`, `wrote <file> (cells with errors: N)`, `cleared outputs in N code cells`, or the code cells |

`notebook` is only advertised when `nbformat`+`nbconvert`+`nbclient` import.

## Why it is built this way

* **User code runs on the main thread, one job at a time.** Sage's cysignals
  layer calls `sig_on()`, which is main-thread-only: `factor()` on a worker
  thread raises `SignalError: Segmentation fault`. Connection threads only
  enqueue jobs and read state.
* **`timeout_s` is not a computation limit.** The job keeps running; `poll`
  collects it. Nothing kills a job for being slow.
* **Per-job output routing.** `sys.stdout`/`sys.stderr` are replaced by routers
  that append to the *currently running job's* buffer and fall back to the real
  stream when idle, so `print()` never leaks into MCP stdout. The kernel's own
  log goes to the original stderr (`kernel.log`), never through the router.
* **`SIGINT` cannot kill the kernel.** An interrupt outside a job is logged and
  ignored; inside a job it becomes that job's `KeyboardInterrupt`. An interrupt
  while a job is still queued does not cancel it.
* **No job may kill the kernel** (`except BaseException`, traceback into
  `job.error`, `job.done.set()` in a `finally`).
* **Preparse, then split the trailing expression**: `preparse(source, True)`,
  strip a leading BOM, `ast.parse(mode="exec")`, pop a trailing `ast.Expr`,
  `exec` the rest and `eval`/`repr` the tail. A `SyntaxError` carries the
  *preparsed* source — the only way to debug a bad rewrite.
* **Caps:** `repr` at 4000 chars, job output at 4 MB (with a notice), client
  response read at 64 MB.
* **`time.sleep` is patched** to accept floats: the preparser turns
  `time.sleep(0.4)` into `sleep(RealNumber('0.4'))` and `__index__` would round
  that to 0, making the sleep return instantly.
* **Sessions are never evicted on their own.** `reset` empties a namespace but
  keeps the entry; `drop` is the only way to free one and it refuses a session
  with a queued or running job (`kept[name] = job_id`).
* **A busy kernel is not a dead kernel.** If the port accepts but `hello`
  times out, the kernel is alive and holding the GIL: `KernelBusy`, which never
  triggers a respawn (that would orphan a live kernel *and* start a second one
  on a new port).
* **Spawn is serialised** with `O_CREAT|O_EXCL` on `spawn.lock`; a second client
  waits for the first one's kernel. The lock is reclaimed only when its mtime is
  older than the wait budget.
* **Only a port file written after this spawn began is trusted**
  (`started >= spawn_mark - 5`, plus a pid *and* `/proc/<pid>/cmdline` match):
  a stale file that survives deletion is not evidence of *our* kernel.
* **A dead launcher is reported immediately** (`proc.poll()`) with the last ~12
  lines of `kernel.log`, not as a generic timeout after 180 s.
* **`kill` is honest.** If the signal fails the state files are deliberately
  left alone and the pid is reported, so a live kernel is never orphaned (and a
  second one never spawned next to it).
* **Several MCP servers share one kernel.** Each carries a client id, the kernel
  reports who is still connected (`status`), and a server exits without killing
  a job another one is collecting.

## Layout

```
sage_mcp.py     MCP stdio server, JSON-RPC 2.0, the nine tools
sage_client.py  spawn/lock/connect; KernelBusy vs unreachable; 64 MB read cap
sage_kernel.py  the long-lived Sage process: main-thread executor + job router
runfile.py      `run_file`: execute an existing file, output inline, nothing stored
nbrun.py        `notebook`: check / extract / clear / run (nbconvert, atomic)
selftest.py     `--check`: spawn, initialize, tools/list, `2+2`, kill -- 7 checks
tests/acceptance.py            63 checks driving the server over stdio
tests/interrupt_ownership.sage 10 checks: only a job's own client may SIGINT it
install.sh      (re)register in mcp.json, then prove it works; drop stale modules
pyproject.toml  `pip install .` / `pip install git+...` (MIT, stdlib only)
```

`run_file` never writes anything: the agent creates the file with its own
tools (bash, an editor, a heredoc) and passes the path; that file is what runs,
with `cwd` = its own directory, so relative paths inside it behave as written.
`lang=python` is Sage's interpreter, which is the whole point of routing this
through the Sage server: `from sage.all import *` works without a venv dance.
`notebook run` writes atomically (temp file + `os.replace`) and the nbconvert
child writes to a *file*, never a pipe: a Jupyter kernel can outlive the run
and would keep an inherited pipe open, making the call look hung after the
notebook was already written.

## Runtime state

State lives outside the package directory (which may be read-only):

```
${SAGE_MCP_STATE:-/tmp/sage-mcp-$UID}/kernel.json   {pid, port, started}
                                     /spawn.lock
                                     /kernel.log
```

Environment: see [Configuration](#configuration) (`SAGE_MCP_STATE`,
`SAGE_PYTHON`, `SAGE_CWD`).

The kernel is stopped when the MCP server exits cleanly, so no Sage process is
left behind; if the server is killed hard, the next server adopts the surviving
kernel (its port file is still valid).

## Requirements

- Linux.
- Python 3.7+ that can `import sage.all` — i.e. Sage's *own* interpreter, whose
  `bin/` also holds the `sage` binary (the interpreter's directory is prepended
  to `PATH`, because `import sage.all` loads Singular from there).
- Any MCP client. Nothing here is Pi-specific: it is one stdio server.

`notebook` additionally needs `nbformat`+`nbconvert`+`nbclient` in that same
interpreter (they ship with SageMath); without them the tool is simply not
advertised.

## Install

Two ways, both ending in the same stdio server:

```bash
pip install git+https://github.com/JGCoelho/simple-sagemath-mcp   # console scripts
./install.sh                                                      # from a checkout
```

- **pip** installs `simple-sage-mcp` (and `simple-sage-mcp-check`) into
  whichever environment you pip into. That environment must be Sage's, or point
  `SAGE_PYTHON` at Sage's interpreter: the console script is only as capable as
  the kernel it can spawn.
- **`./install.sh`** picks the first interpreter that can `import sage.all` (or
  `$SAGE_PYTHON`), copies nothing when the server already lives in the target
  directory, and then **verifies the result** by spawning the server against a
  throwaway state dir and running `2+2` through it. If that fails it prints the
  kernel log and exits non-zero, so a mis-registration cannot pass silently.

Then:

- **Pi**: `install.sh` writes the `sage` entry into `~/.pi/agent/mcp.json` and
  prints the `AGENTS.md` section to paste in. Restart Pi to pick up the tools.
- **Any other client**: if there is no `mcp.json` next to the install target, it
  prints the JSON block to add to your client's config (see
  `example.mcp.json`). Point `command` at Sage's python and `args[0]` at
  `sage_mcp.py`; that is the whole configuration.
- **Afterwards, whenever in doubt** — a different machine, an edited config, an
  agent reporting trouble:

```bash
./install.sh --check                       # tests the registration in mcp.json
python selftest.py --config ~/.config/mcp.json
simple-sage-mcp-check /path/python /path/sage_mcp.py
```

`--check` reads the `sage` entry that is actually configured and drives it over
stdio: spawn, `initialize`, `tools/list`, `exec` of `2+2`, `status`, `kill` —
7 checks, every failure printed with the kernel log that explains it (the usual
cause is a `command` that is not Sage's interpreter). It runs against
`$TMPDIR/sage-selftest-*`, so the live kernel is never touched.

```bash
SAGE_PYTHON=/path/to/sage-env/bin/python ./install.sh   # choose the interpreter
./install.sh /opt/sage-mcp                              # install elsewhere
PI_AGENT_DIR=/tmp/fakeagent ./install.sh                # scratch agent dir
```

## License

MIT — see `LICENSE`.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `SAGE_PYTHON` | our own interpreter, else the python beside `sage` on `PATH` | interpreter used to spawn the kernel (must import `sage.all`) |
| `SAGE_MCP_STATE` | `/tmp/sage-mcp-$UID` | port/pid files, `running.json`, `kernel.log` |
| `SAGE_CWD` | the server's cwd | working directory for `exec` and `run_file` |

## Tests

```bash
# 63 checks, over MCP stdio, ~60 s
/path/to/sage-env/bin/python tests/acceptance.py

# 10 checks, as a Sage script, ~10 s (Sage's own interpreter: it must be on PATH
# for `singular` to load)
PATH=/path/to/sage-env/bin:$PATH \
    sage tests/interrupt_ownership.sage
```

`acceptance.py` drives the server over stdio and checks the invariants that are
easy to regress: busy-vs-dead, spawn serialisation, stale port files, dead
launchers, honest kill, output caps, `since` offsets, kernel.log isolation,
shared-kernel ownership, run_file's interpreter selection, cwd, args, stdin,
timeout and truncation rules, the notebook cycle, and that stdout carried nothing but JSON-RPC.
