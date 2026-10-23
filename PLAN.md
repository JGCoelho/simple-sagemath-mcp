# sage-mcp rebuild — survey + plan

## Survey (this machine)

| item | value |
| --- | --- |
| Sage | 10.8, conda env `sageenv`, python 3.14.4 |
| python | the Sage environment's `bin/python` |
| `sage` wrapper | same env, `sage -q --simple-prompt` |
| `sage.all` import | 1.6 s **only if** the env `bin/` is first in `PATH` (else `FeatureNotPresentError: singular`) |
| kernelspec | `sagemath` present in the env |
| mcp SDK | 2.3.0 (its `mcp.server.mcpserver` API is what the *old* server.py assumed) |
| nbformat / nbconvert | present (nbconvert 7.17.1, nbclient 0.8.0) |
| pypdf / PyPDF2 | **absent**; `pip install` blocked (proxy 403 to files.pythonhosted.org) |
| pdf tooling present | `pdftotext` (poppler), `mutool`, `gs` — usable CLI backends |
| writable paths | `$HOME`, `/tmp`, `sageenv` site-packages; **`~/.pi` is a read-only bind mount** |

### Blocker (needs a human)

`~/.pi` was mounted read-only at the time (an ext4 mount with `ro`), and the
sandbox's `denyWrite` for `./.pi` resolved to that same path because the
agent's cwd is `$HOME` — so both the sandbox policy *and* the kernel refuse writes to
`~/.pi/agent/` (probe: `echo x > ~/.pi/agent/sage-mcp/_probe` → `Read-only file system`).
Also, `mcp.json` lives in that same directory and cannot be edited by this session.

Therefore the tree was built and tested elsewhere first; it now lives in
`~/.pi/agent/sage-mcp/` (each MCP has its own folder under `~/.pi/agent`) and
`install.sh` only rewrites the `mcp.json` entry.  `~/.pi` is writable again.

## Architecture

```
~/.pi/agent/sage-mcp/
├── sage_mcp.py       MCP stdio server (JSON-RPC 2.0, 9 tools) — nothing but JSON on stdout
├── sage_client.py    spawn serialisation, hello/op timeouts, KernelBusy vs unreachable, 64 MB cap
├── sage_kernel.py    long-lived Sage process: main-thread executor, per-job stream router
├── runfile.py        `run_file`: execute a file that already exists, output inline
├── nbrun.py          `notebook`: check / extract / clear / run (nbconvert → files)
├── tests/acceptance.py  drives the server over stdio, runs the §9 suite
├── install.sh        register in mcp.json (no copying)
└── README.md
```

State dir (port file, spawn lock, kernel.log): `$SAGE_MCP_STATE`, else `/tmp/sage-mcp-<uid>`
(never inside the package dir: that dir is read-only at runtime on this box).

### Wire protocol (kernel ⇄ client, 127.0.0.1, one JSON line per request)

`hello`, `submit{session,code,preparse,wait}`, `wait{job_id,timeout,since}`,
`poll{job_id,since}`, `reset{session}`, `drop{sessions,all}`, `status`, `interrupt`,
`shutdown`. `submit`/`wait` block **in the connection thread**, never the executor thread.

Kernel state file `.run/kernel.json`: `{pid, port, started, version}` written atomically
after the listener binds; the client only trusts it when `started >= spawn_mark - 5`
**and** `pid == child.pid` (rule 13), and removes any stale file before spawning.

### Threading model (rule 1)

Kernel process: 1 executor thread (`main`, runs `exec`/`eval` → safe for cysignals
`sig_on()`), N connection threads (enqueue + read state only). The MCP server: 1 reader
loop + 1 thread per request so `interrupt`/`kill`/`poll` are answerable while an `exec`
is waiting on its timeout.

### Behaviour checklist → implementation

| # | rule | where |
| --- | --- | --- |
| 1 | user code on main thread, one job at a time | `sage_kernel._executor_main` |
| 2 | `timeout_s` never kills a job | `sage_mcp._tool_exec` / `wait` op |
| 3 | per-job stdout/stderr router, real stream when idle | `sage_kernel._Router` + `Job.append` |
| 4 | kernel log → original stderr → `kernel.log` | `_log()` writes the saved stream |
| 5 | SIGINT cannot kill the kernel | `try/except KeyboardInterrupt` around `queue.get` + dispatch |
| 6 | no job kills the kernel | `except BaseException` / `finally job.done.set()` |
| 7 | preparse → BOM strip → split trailing `Expr` | `_run_job`, `_syntax_report` attaches preparsed src |
| 8 | repr ≤ 4000 chars, job output ≤ 4 MB + notice | `REPR_CAP`, `OUTPUT_CAP` |
| 9 | `time.sleep` accepts floats | `_patch_sleep` at kernel start |
| 10 | sessions never auto-evicted; `drop` refuses busy ones | `op_drop` → `kept[name] = job_id` |
| 11 | busy ≠ dead; `KernelBusy` never respawns | `KernelBusy` vs `KernelDown`, `ensure()` |
| 12 | spawn serialised by `O_CREAT\|O_EXCL` lock, reclaim on stale mtime | `_spawn_locked` |
| 13 | only trust port files written after this spawn | `started >= mark-5`, `pid == proc.pid` |
| 14 | dead launcher reported at once with log tail | `proc.poll()` in the wait loop |
| 15 | only JSON-RPC on stdout | `sys.stdout` swapped to stderr, `_emit()` writes saved fd |
| 16 | `interrupt` keeps state, `kill` is last resort | tool descriptions |
| 17 | `kill` honest, keeps state files on failure | `Client.kill()` |
| 18 | `nb run` atomic, child output to files | `nbrun.run` |
| 19 | 64 MB client read cap | `_recv_line` |

## Order of work

1. `sage_kernel.py` → smoke test with a raw socket client.
2. `sage_client.py` → spawn/kill/busy tests.
3. `sage_mcp.py` → JSON-RPC + the kernel tools.
4. `runfile.py` + `nbrun.py` (`run_file`, `notebook`), then the JSON-RPC layer
   for the nine tools.
5. `tests/acceptance.py` → full §9 suite, iterate until green.
6. `install.sh` + `README.md`.

## Split

`pdf` moved to its own server (`~/.pi/agent/pdf-mcp`) because it is unrelated to
the kernel.

`run_file` and `notebook` briefly lived in a separate `lab-mcp`, but they came
back: `notebook` drives the Jupyter kernel SageMath ships (it needs Sage's
environment), and `run_file`'s `lang=python` is Sage's interpreter. One server,
one environment.

The first attempt at `run_file` was an artifact *store* (copy the script into
`$ARTIFACT_HOME/<id>/`, keep stdout/stderr/meta.json, an index, immutable ids).
That was a misreading of the requirement: the agent writes the file with its own
tools, so the tool only has to run *that* file and hand back the output.
