#!/usr/bin/env python3
"""sage_mcp.py -- MCP server (official SDK, stdio) in front of a Sage kernel.

Eight tools: the kernel itself (exec, poll, reset, drop, status, interrupt --
force=true stops the kernel, not just the job) plus the two that need Sage's
own interpreter (run_file, notebook).  This process needs only `mcp`; user code
runs in a separate kernel started with Sage's interpreter, which
`sage_client.resolve_python()` discovers (DESIGN.md: two interpreters).

`--check` (also `simple-sage-mcp-check`) drives a *registered* server through
the official client against a throwaway state dir, so an install can be proven
without touching the live kernel.
"""

import asyncio
import functools
import json
import os
import shutil
import sys
import tempfile
import time
from typing import Annotated, Literal

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from pydantic import Field  # noqa: E402

import subproc  # noqa: E402
from sage_client import (  # noqa: E402
    KernelBusy,
    KernelDown,
    KernelError,
    format_uptime,
    get_client,
)

__version__ = "1.0.0"
SESSION_DOC = "Namespace to run in. State persists per session between exec calls."

mcp = MCPServer(
    name="sage",
    version=__version__,
    instructions=(
        "A persistent Sage kernel. exec runs code on the kernel's main thread "
        "(state persists per session); timeout_s never cancels a job -- collect "
        "it with poll. interrupt keeps the session's variables; interrupt with "
        "force=true stops the kernel itself, and is the last resort. run_file "
        "executes a file the agent already wrote and "
        "returns the output inline (python = Sage's interpreter); notebook "
        "drives SageMath's Jupyter kernel."
    ),
)

# The job each session submitted last, so an exec whose *reply* was cancelled
# can still be collected: status names it, poll(job_id) drains it.
LAST_JOB = {}

# helpers


def _fmt_secs(value):
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        value = 0.0
    return "%.1f" % value if value < 100 else "%.0f" % value


def _join(*parts):
    out = ""
    for part in parts:
        text = "" if part is None else str(part).rstrip("\n")
        if text:
            out = text if not out else out + "\n" + text
    return out


def _body(job):
    parts = [job.get("note"), job.get("stdout") or ""]
    if job.get("error"):
        parts.append(job["error"])
    elif job.get("repr"):
        parts.append(job["repr"])
    text = _join(*parts)
    return text if text.strip() else "(no output)"


def _job_text(job, job_id):
    """Text for an exec/poll result that may still be running."""
    if job.get("state") == "done":
        return _body(job)
    parts = [job.get("note"), job.get("stdout") or ""]
    if job.get("state") == "queued":
        if job.get("blocked"):
            parts.append("[queued: another job is running]")
        # else: just accepted and waiting for the executor's next tick (ms)
    else:
        parts.append("[still running: %ss]" % _fmt_secs(job.get("elapsed_s")))
    parts.append(
        "[job_id %s — call poll with job_id=%r, since=%d]"
        % (job_id, job_id, job.get("offset") or 0)
    )
    return _join(*parts)


def _as_float(value, default):
    if value is None:
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ToolError("expected a number, got %r" % (value,))


def _iso(when):
    """A timestamp as the resource reports it (UTC, seconds)."""
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(when)))
    except (TypeError, ValueError, OSError):
        return None


def _log_tail_lines(path, lines=20, budget=8192):
    """The last `lines` of kernel.log, read from its tail only (never readlines)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - budget))
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    rows = data.splitlines()
    if size > budget:
        rows = rows[1:]  # the first line of the window may be cut in half
    return [row for row in rows if row.strip()][-lines:]


def status_json():
    """PLAN §5: the kernel's counters plus the few facts only the server knows."""
    client = get_client()
    snap = client.status_snapshot(timeout=2.0)
    state = snap.get("state")
    known = state == "running"
    jobs = snap.get("jobs") or {}
    if known:
        kernel_state = "busy" if jobs.get("running") else "idle"
    else:
        kernel_state = "busy" if state == "busy" else (
            "unreachable" if state == "unreachable" else "not running"
        )
    last = snap.get("last_error")
    log_path = snap.get("log") or client.log_path
    return {
        "server": {
            "version": __version__,
            "pid": os.getpid(),
            "clients": (snap.get("clients") or {}).get("live"),
            "spawns": client.spawns,
            "restarts": client.restarts,
        },
        "kernel": {
            "pid": snap.get("pid"),
            "state": kernel_state,
            "uptime_s": snap.get("uptime_s"),
            "restarts": client.restarts,
            "started": _iso(snap.get("started")),
            "counters_since": _iso(snap.get("counters_since")),
        },
        "sessions": sorted(snap.get("sessions") or {}),
        "jobs": {
            "run": jobs.get("run"),
            "failed": jobs.get("failed"),
            "interrupted": jobs.get("interrupted"),
            "queued": len(jobs.get("queued") or []) if known else None,
            "last_job": jobs.get("last_job"),
            "gap_trips": snap.get("gap_trips"),
        },
        "last_error": (
            {
                "error": last.get("error"),
                "job": last.get("job"),
                "session": last.get("session"),
                "when": _iso(last.get("when")),
            }
            if last
            else None
        ),
        "limits": None,  # filled in by the limits work (PLAN §7)
        "log": {"path": log_path, "tail": _log_tail_lines(log_path)},
    }


@mcp.resource("sage://kernel/status", mime_type="application/json")
def kernel_status() -> str:
    """Machine-readable status: kernel counters, last error and the log tail."""
    return json.dumps(status_json(), indent=2)


def _tool_errors(fn):
    """Kernel trouble is anticipated: tell the model, keep the message."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (KernelBusy, KernelDown, KernelError) as exc:
            raise ToolError(str(exc))

    return wrapper

# tools


@mcp.tool(name="exec", structured_output=False)


@_tool_errors


def tool_exec(
    code: Annotated[str, Field(description="Code to execute.")],
    session: Annotated[str, Field(description=SESSION_DOC)] = "default",
    timeout_s: Annotated[
        float,
        Field(description="Seconds to wait for the result; 0 = return at once. "
                          "Never cancels the job."),
    ] = 120,
    preparse: Annotated[
        bool,
        Field(description="Apply the Sage preparser (2^10 -> 1024, R.<x> = ...). "
                          "false runs raw Python (2^10 is XOR)."),
    ] = True,
) -> str:
    """Run Sage/Python in the long-lived kernel: stdout, then the repr of the last expression (or '(no output)'). Code runs on the kernel's main thread, one job at a time, so cysignals Sage code works; state persists per session. timeout_s is NOT a computation limit: on expiry you get the partial output and a job id, and the job keeps running (collect it with poll). timeout_s=0 returns at once. A submitted job waits behind the running one ("[queued: another job is running]"); if that one holds the GIL the submit itself is refused after ~10 s ("[kernel busy ...]", the job was not submitted) -- check status or interrupt, then retry."""
    if os.environ.get("SAGE_MCP_STDOUT_NOISE"):
        # Rule 15 probe (tests/acceptance.py): prove fd 1 is not the wire.
        print("sage-mcp: stray handler stdout", flush=True)
    if not isinstance(code, str):
        raise ToolError("code must be a string")
    session = str(session or "default")
    timeout = _as_float(timeout_s, 120)
    client = get_client()
    try:
        state, restarted, reason = client.ensure()
    except KernelBusy as exc:
        return _join(
            "[kernel busy: %s]" % exc,
            "[nothing was submitted — wait, call status, or call interrupt]",
        )

    prefix = ""
    if restarted and reason:
        prefix = "[kernel restarted: %s (pid %s)]" % (reason, state["pid"])

    try:
        reply = client.op(
            "submit",
            _timeout=10.0,
            session=session,
            code=code,
            preparse=bool(preparse),
            wait=0,
        )
    except KernelBusy as exc:
        return _join(
            prefix,
            "[kernel busy: %s]" % exc,
            "[the job was not submitted; call status or interrupt, then retry]",
        )

    job_id = reply["job_id"]
    # Before the (possibly long) wait, and before this request can be
    # cancelled: the job id must stay recoverable from status.
    LAST_JOB[session] = job_id
    job = reply["job"]
    if timeout <= 0:  # 0 = return at once
        return _join(prefix, _job_text(job, job_id))

    try:
        reply = client.op(
            "wait", _timeout=timeout + 5.0, job_id=job_id, timeout=timeout, since=0
        )
        job = reply["job"]
    except KernelBusy:
        # The kernel cannot answer right now (a computation holds the GIL).
        # The job keeps running: report what we last saw and how to collect it.
        pass
    return _join(prefix, _job_text(job, job_id))

MAX_POLL_WAIT = 3600.0


@mcp.tool(name="poll", structured_output=False)


@_tool_errors


def tool_poll(
    job_id: Annotated[str, Field(description="Job id such as j7.")],
    since: Annotated[
        int,
        Field(description="Character offset already consumed; the previous call "
                          "prints the next value."),
    ] = 0,
    wait_s: Annotated[
        float,
        Field(description="Block up to this many seconds waiting for the job to "
                          "finish (0 = return immediately, the default)."),
    ] = 0,
) -> str:
    """Collect a job started by exec. since = the offset the previous call printed (0 to start), so you get only new output. wait_s blocks until the job finishes or that many seconds pass (0 = return immediately, the default)."""
    if not job_id:
        raise ToolError("job_id is required")
    offset = max(0, int(_as_float(since, 0)))
    wait = min(max(_as_float(wait_s, 0.0), 0.0), MAX_POLL_WAIT)
    client = get_client()
    try:
        if wait > 0:
            # Optional blocking collection: the kernel waits on the job's done
            # event and returns the same snapshot `poll` would.
            reply = client.op(
                "wait", _timeout=wait + 15.0, job_id=str(job_id), timeout=wait, since=offset
            )
        else:
            reply = client.op("poll", _timeout=10.0, job_id=str(job_id), since=offset)
    except KernelBusy as exc:
        raise ToolError("kernel busy: %s" % exc)
    job = reply["job"]
    parts = [job.get("note"), job.get("stdout") or ""]
    if job.get("state") == "done":
        if job.get("error"):
            parts.append(job["error"])
        elif job.get("repr"):
            parts.append(job["repr"])
        duration = job.get("duration_s")
        parts.append(
            "[job finished in %ss]"
            % _fmt_secs(duration if duration is not None else job.get("elapsed_s"))
        )
    elif job.get("state") == "queued" and job.get("blocked"):
        parts.append("[queued: another job is running]")
    else:
        parts.append(
            "[still running %ss; poll again with since=%d]"
            % (_fmt_secs(job.get("elapsed_s")), job.get("offset") or 0)
        )
    return _join(*parts)


@mcp.tool(name="reset", structured_output=False)


@_tool_errors


def tool_reset(
    session: Annotated[str, Field(description=SESSION_DOC)] = "default",
) -> str:
    """Empty a session's namespace but keep the session (see drop)."""
    session = str(session or "default")
    get_client().op("reset", _timeout=10.0, session=session)
    return "session '%s' reset (namespace empty)" % session


@mcp.tool(name="drop", structured_output=False)


@_tool_errors


def tool_drop(
    sessions: Annotated[
        list[str] | None, Field(description="Session names to drop.")
    ] = None,
    all: Annotated[
        bool,
        Field(description="Drop every session that has no queued/running job."),
    ] = False,
) -> str:
    """Free idle sessions; refuses one with a queued/running job (kept, and the job id is reported). all=true drops every idle session."""
    names = [str(item) for item in sessions or []]
    if not names and not all:
        raise ToolError("nothing to drop: pass sessions=[...] or all=true")
    reply = get_client().op("drop", _timeout=10.0, sessions=names, all=bool(all))
    lines = []
    dropped = reply.get("dropped") or []
    kept = reply.get("kept") or {}
    missing = reply.get("missing") or []
    remaining = reply.get("remaining") or []
    if dropped:
        lines.append("dropped: " + ", ".join(dropped))
    for name, job_id in kept.items():
        lines.append(
            "kept '%s': job %s is still queued or running (poll it, or interrupt first)"
            % (name, job_id)
        )
    if missing:
        lines.append("not found: " + ", ".join(missing))
    lines.append("remaining: " + (", ".join(remaining) if remaining else "(none)"))
    return "\n".join(lines)


@mcp.tool(name="status", structured_output=False)


def tool_status() -> str:
    """Kernel state (running / busy / not running) with pid and uptime, one line per session with its name count, active jobs, the last error, the log path."""
    client = get_client()
    snapshot = client.status_snapshot(timeout=5.0)
    state = snapshot.get("state")
    log_path = snapshot.get("log") or ""
    if state == "running":
        head = "kernel: running, up %s (pid %s)" % (
            format_uptime(snapshot.get("uptime_s")),
            snapshot.get("pid"),
        )
    elif state == "busy":
        try:
            import time as _time

            uptime = _time.time() - float(snapshot.get("started") or 0)
        except Exception:
            uptime = 0.0
        head = (
            "kernel: busy, up %s (pid %s) — the connection was accepted but hello did "
            "not answer in time, i.e. a computation holds the GIL. It is alive: poll "
            "your job, or interrupt." % (format_uptime(uptime), snapshot.get("pid"))
        )
    elif state == "absent":
        note = snapshot.get("note")
        if note:
            head = "kernel: not running (%s; the next exec starts a fresh one)" % note
        else:
            head = "kernel: not running (it has not been started yet; the next exec starts it)"
    elif state == "dead":
        head = (
            "kernel: not running (state file points at pid %s, which is gone; "
            "the next exec starts a fresh one)" % snapshot.get("pid")
        )
    else:  # unreachable
        head = "kernel: not running (pid %s does not answer: %s)" % (
            snapshot.get("pid"),
            snapshot.get("error"),
        )
    lines = [head]
    sessions = snapshot.get("sessions") or {}
    for name in sorted(sessions):
        lines.append("session %s: %d names" % (name, sessions[name]))
    jobs = snapshot.get("jobs") or {}
    active = ["%s (queued)" % j for j in jobs.get("queued") or []] + [
        "%s (running)" % j for j in jobs.get("running") or []
    ]
    if state in ("busy", "unreachable"):
        # The socket is not answering, but the kernel publishes the running job
        # to a file exactly so this case can be reported truthfully.
        running = client.running_job(wait=0.2)
        if running:
            active.append(
                "%s (running, session %s, owner %s)"
                % (running.get("job"), running.get("session"), running.get("owner") or "?")
            )
        lines.append(
            "active jobs: "
            + (", ".join(active) if active else "unknown (the kernel is not answering)")
        )
    else:
        lines.append("active jobs: " + (", ".join(active) if active else "none"))
    if state == "running":
        lines.append(
            "jobs: %d run, %d failed, %d interrupted; gap-guard: %d; restarts: %d"
            % (
                jobs.get("run", 0),
                jobs.get("failed", 0),
                jobs.get("interrupted", 0),
                snapshot.get("gap_trips") or 0,
                client.restarts,
            )
        )
    else:
        lines.append(
            "jobs: unknown (the kernel is not answering); gap-guard: %d; restarts: %d"
            % (snapshot.get("gap_trips") or 0, client.restarts)
        )
    if LAST_JOB:
        lines.append(
            "last job per session: "
            + ", ".join("%s=%s" % (name, LAST_JOB[name]) for name in sorted(LAST_JOB))
        )
    live = (snapshot.get("clients") or {}).get("live")
    if live:
        others = int(live) - 1
        note = " + %d other%s" % (others, "" if others == 1 else "s") if others > 0 else ""
        lines.append("clients: %d (this server%s)" % (live, note))
    last = snapshot.get("last_error")
    if last:
        lines.append(
            "last error: %s (job %s, session %s)"
            % (last.get("error") or "?", last.get("job"), last.get("session"))
        )
    else:
        lines.append("last error: none")
    lines.append("log: " + log_path)
    return "\n".join(lines)


@mcp.tool(name="interrupt", structured_output=False)


def tool_interrupt(
    force: Annotated[
        bool,
        Field(description="Stop the kernel itself (all state is lost), not just the job."),
    ] = False,
) -> str:
    """SIGINT the running computation: the job fails with KeyboardInterrupt and the session KEEPS its state -- the first thing to try for a runaway job. Refused when the running job belongs to another MCP client; a no-op when idle: SIGINT can never kill the kernel. force=true is the last resort instead: stop the whole kernel (graceful shutdown, then SIGTERM, then SIGKILL), losing every session's variables -- for a computation that ignores SIGINT, e.g. a wedged C-level loop. The next exec starts a fresh kernel and says so; if the signal fails the state files are kept and the pid reported, so a live kernel is never orphaned."""
    client = get_client()
    if force:
        result = client.kill()
        if not result.get("ok"):
            raise ToolError(result["text"])
        return result["text"]
    pid, message, refused = client.interrupt()
    if refused:
        raise ToolError(message)
    if pid is None:
        return message
    return _join(
        "SIGINT sent to kernel pid %s" % pid,
        "if a job was running it became that job's KeyboardInterrupt and the session "
        "kept its variables (poll to collect the error); if the kernel was idle the "
        "signal was ignored, because SIGINT cannot kill the kernel.",
    )


@mcp.tool(name="run_file", structured_output=False)


def tool_run_file(
    path: Annotated[
        str,
        Field(description="The file to run (absolute or relative to this server's cwd)."),
    ],
    lang: Annotated[
        Literal["auto", "bash", "python", "sage", "exec"],
        Field(description="Interpreter; auto uses the extension, then the #! line."),
    ] = "auto",
    args: Annotated[
        list[str] | None, Field(description="Arguments appended to the command line.")
    ] = None,
    timeout_s: Annotated[
        float, Field(description="Seconds before the process group is killed.")
    ] = 120,
    stdin: Annotated[str | None, Field(description="Text fed to the file on stdin.")] = None,
    cwd: Annotated[
        str | None,
        Field(description="Working directory (default: the file's own directory)."),
    ] = None,
) -> str:
    """Run a file that already exists and return its output inline: exit code, duration, stdout and stderr. Nothing is copied or stored -- the file you name is the file that runs, with cwd = its own directory. lang=auto guesses from the extension and honours a #! for executables; lang=python uses Sage's own interpreter (so `from sage.all import *` works) and lang=sage shells out to `sage`. A non-zero exit or a timeout is an error, with the output attached; the timeout kills the whole process group."""
    try:
        return subproc.run_file(
            path=path, lang=lang, args=args, timeout_s=timeout_s, stdin=stdin, cwd=cwd
        )
    except subproc.SubprocessError as exc:
        raise ToolError(str(exc))


@_tool_errors


def tool_notebook(
    action: Annotated[Literal["check", "run", "clear", "extract"], Field()],
    path: Annotated[str, Field(description=".ipynb file.")],
    out: Annotated[
        str | None,
        Field(description="Output path for run/clear (default: <name>.out.ipynb "
                          "for run, the input file for clear)."),
    ] = None,
    in_place: Annotated[
        bool, Field(description="Overwrite the input notebook.")
    ] = False,
    cell_timeout_s: Annotated[
        float, Field(description="Per-cell execution timeout for run.")
    ] = 120,
    timeout_s: Annotated[
        float | None, Field(description="Overall wall clock budget for run.")
    ] = None,
) -> str:
    """Jupyter notebook helper for the kernels SageMath ships. check: validate and summarise. extract: print the code cells. clear: strip outputs (atomically). run: execute with nbconvert (atomically, to <name>.out.ipynb unless out/in_place is given) and report how many cells ended in error."""
    action = str(action or "").strip().lower()
    if action not in ("check", "run", "clear", "extract"):
        raise ToolError(
            "action must be one of check, run, clear, extract (got %r)" % (action,)
        )
    if not path:
        raise ToolError("notebook needs a path to an .ipynb file")
    try:
        if action == "check":
            return subproc.check(path)
        if action == "extract":
            return subproc.extract(path)
        if action == "clear":
            return subproc.clear(path, out=out, in_place=bool(in_place))
        return subproc.run_notebook(
            path,
            out=out,
            in_place=bool(in_place),
            cell_timeout_s=int(_as_float(cell_timeout_s, 120)),
            timeout_s=None if timeout_s is None else _as_float(timeout_s, 0),
            state_dir=get_client().state_dir,
        )
    except subproc.SubprocessError as exc:
        raise ToolError(str(exc))
    except OSError as exc:
        raise ToolError("%s: %s" % (type(exc).__name__, exc))

# `notebook` needs nbformat+nbconvert+nbclient; the rest are unconditional.
if subproc.available():
    mcp.tool(name="notebook", structured_output=False)(tool_notebook)

# --check: prove that a registration really works (post-install smoke test)
CHECK_TIMEOUT = 90.0
KERNEL_TOOLS = ("exec", "poll", "reset", "drop", "status", "interrupt", "run_file")

CHECK_DOC = """\
Usage: sage_mcp.py --check [--config mcp.json] [--name sage] [--timeout S]
       sage_mcp.py --check <command> [server-script]

Spawns the given (or registered) server against a throwaway SAGE_MCP_STATE and
checks, through the official MCP client: spawn, initialize, tools/list, the
kernel tools, exec `2+2`, status, and interrupt(force=true).  Exit 0 only when
every check passed; the caller's live kernel is never touched."""


def _mcp_entry(path, name="sage"):
    """(command, args, env) for `name` in an mcp.json-style file."""
    with open(path) as fh:
        data = json.load(fh)
    servers = data.get("mcpServers") or data.get("servers") or {}
    entry = servers.get(name)
    if not isinstance(entry, dict) or not entry.get("command"):
        raise SystemExit("error: no %r entry in %s" % (name, path))
    return entry["command"], [str(arg) for arg in entry.get("args") or []], (
        entry.get("env") or {}
    )


def _log_tail(state_dir, lines=6):
    """The end of kernel.log: the launcher's own words about a failed spawn."""
    try:
        with open(os.path.join(state_dir, "kernel.log"), "r", errors="replace") as fh:
            tail = fh.read()[-4000:].rstrip().splitlines()
    except OSError:
        return ""
    return "\n".join(tail[-lines:]) if tail else ""

async def _tool_text(session, name, arguments, timeout):
    result = await asyncio.wait_for(session.call_tool(name, arguments), timeout)
    text = "".join(
        getattr(block, "text", "")
        for block in result.content or []
        if getattr(block, "type", None) == "text"
    )
    return text, bool(result.is_error)

async def _probe(session, state_dir, timeout):
    """The seven checks, each bounded and each recording its own failure."""
    out = []

    async def step(name, call, judge):
        try:
            value = await asyncio.wait_for(call(), timeout)
        except Exception as exc:
            out.append((name, False, "%s: %s" % (type(exc).__name__, exc)))
            return None
        ok, detail = judge(value)
        out.append((name, bool(ok), detail))
        return value

    def _first(text):
        return (text.splitlines() or [""])[0]

    async def tool(name, arguments):
        return await _tool_text(session, name, arguments, timeout)

    await step(
        "initialize",
        session.initialize,
        lambda info: (
            bool(info.server_info.name),
            "%s %s" % (info.server_info.name, info.server_info.version),
        ),
    )
    names = await step(
        "tools/list",
        session.list_tools,
        lambda listing: (
            bool(listing.tools),
            ", ".join(tool.name for tool in listing.tools),
        ),
    )
    advertised = [tool.name for tool in names.tools] if names else []
    missing = [name for name in KERNEL_TOOLS if name not in advertised]
    out.append(("the kernel tools are advertised", not missing, ", ".join(missing) or "all present"))

    def judge_exec(result):
        text, is_error = result
        detail = _first(text) if text else "(no text)"
        if is_error or "4" not in text:
            tail = _log_tail(state_dir)
            detail += "\nkernel.log:\n" + tail if tail else ""
        return not is_error and "4" in text, detail

    await step("exec runs Sage code", lambda: tool("exec", {"code": "print(2+2)"}), judge_exec)
    await step(
        "status",
        lambda: tool("status", {}),
        lambda result: (not result[1] and "kernel: running" in result[0], _first(result[0])),
    )
    await step(
        "interrupt(force=true)",
        lambda: tool("interrupt", {"force": True}),
        lambda result: (not result[1], _first(result[0])),
    )
    return out


def check(command, args, extra_env=None, timeout=CHECK_TIMEOUT):
    """Run the smoke test; return a list of (name, ok, detail)."""
    state_dir = tempfile.mkdtemp(prefix="sage-check-")
    label = "spawn " + " ".join([command] + list(args))
    results = []
    env = os.environ.copy()
    env.update({str(k): str(v) for k, v in (extra_env or {}).items()})
    env["SAGE_MCP_STATE"] = state_dir  # never the caller's live kernel
    env.setdefault("SAGE_CWD", "/tmp")
    env["PATH"] = os.path.dirname(command) + os.pathsep + env.get("PATH", "")
    params = StdioServerParameters(command=command, args=list(args), env=env, cwd="/tmp")

    async def probe():
        try:
            async with stdio_client(params, errlog=sys.stderr) as (read, write):
                async with ClientSession(read, write) as session:
                    results.append((label, True, ""))
                    results.extend(await _probe(session, state_dir, timeout))
        except BaseException as exc:  # a broken server must be a failed check
            results.append(
                (label if not results else "the server answered the handshake",
                 False, "%s: %s" % (type(exc).__name__, exc))
            )

    try:
        asyncio.run(probe())
    finally:
        if not results:
            results.append((label, False, "the server did not start"))
        for name, ok, detail in results:
            print("[%s] %s" % ("PASS" if ok else "FAIL", name))
            for line in str(detail).rstrip().splitlines() if detail else []:
                print("       " + line)
        shutil.rmtree(state_dir, ignore_errors=True)
    return results


def check_main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--check":
        argv.pop(0)
    command, args, extra_env, timeout = None, [], {}, CHECK_TIMEOUT
    config, wanted = None, "sage"
    while argv:
        flag = argv.pop(0)
        if flag in ("-h", "--help"):
            print(CHECK_DOC.strip())
            return 0
        if flag == "--config":
            config = argv.pop(0)
        elif flag == "--name":
            wanted = argv.pop(0)
        elif flag == "--timeout":
            timeout = float(argv.pop(0))
        elif flag.startswith("-"):
            raise SystemExit("error: unknown option %s" % flag)
        elif command is None:
            command = flag
            args = [argv.pop(0)] if argv and not argv[0].startswith("-") else []
        else:
            args.append(flag)
    if command is None and config is not None:  # --config/--name in any order
        command, args, extra_env = _mcp_entry(config, wanted)
    if command is None:
        print(CHECK_DOC.strip())
        return 2
    results = check(command, args, extra_env, timeout=timeout)
    failed = [name for name, ok, _ in results if not ok]
    print()
    print("%d checks, %d failed" % (len(results), len(failed)))
    return 0 if not failed else 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if "--check" in argv:
        return check_main(argv)
    client = get_client()
    try:
        mcp.run(transport="stdio")
    finally:
        # Never leave an orphaned Sage process behind -- but another MCP server
        # (another Pi session or subagent) may share this kernel, so only the
        # last one out may stop it.
        try:
            client.stop_if_last()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
