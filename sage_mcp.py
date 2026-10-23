#!/usr/bin/env python3
"""sage_mcp.py -- MCP (stdio, JSON-RPC 2.0) front end for the Sage kernel.

Nine tools: the kernel itself (exec, poll, reset, drop, status, interrupt,
kill) plus the two tools that need Sage's own interpreter (run_file: execute a
file the agent already wrote, inline results; notebook: drive the Jupyter kernel
SageMath ships).  PDFs are a separate server, pdf-mcp.

Rule 15: nothing but JSON-RPC may reach stdout.  The real stdout is captured at
start-up and every diagnostic is redirected to stderr, so a stray print, a
warning or a traceback can never corrupt the transport.
"""

import json
import os
import sys
import threading
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import nbrun  # noqa: E402
import runfile  # noqa: E402
from sage_client import (  # noqa: E402
    KernelBusy,
    KernelDown,
    KernelError,
    format_uptime,
    get_client,
)

VERSION = "1.0.0"
PROTOCOL_FALLBACK = "2025-06-18"


class ToolError(Exception):
    """Tool-level failure: reported to the model with isError=true."""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _fmt_secs(value):
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        value = 0.0
    if value < 100:
        return "%.1f" % value
    return "%.0f" % value


def _join(*parts):
    out = ""
    for part in parts:
        if part is None:
            continue
        text = str(part)
        if not text:
            continue
        text = text.rstrip("\n")
        out = text if not out else out + "\n" + text
    return out


def _body(job):
    parts = [job.get("stdout") or ""]
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
    parts = [job.get("stdout") or ""]
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


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------
def tool_exec(code, session="default", timeout_s=120, preparse=True):
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
    except KernelError as exc:
        raise ToolError(str(exc))

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
    except KernelError as exc:
        raise ToolError(str(exc))

    job_id = reply["job_id"]
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
    except KernelError as exc:
        raise ToolError(str(exc))
    return _join(prefix, _job_text(job, job_id))


MAX_POLL_WAIT = 3600.0


def tool_poll(job_id, since=0, wait_s=0):
    if not job_id:
        raise ToolError("job_id is required")
    offset = int(_as_float(since, 0))
    if offset < 0:
        offset = 0
    wait = _as_float(wait_s, 0.0)
    if wait < 0:
        wait = 0.0
    wait = min(wait, MAX_POLL_WAIT)
    client = get_client()
    try:
        if wait > 0:
            # Optional blocking collection: the kernel waits on the job's done
            # event and returns the same snapshot `poll` would.
            reply = client.op(
                "wait",
                _timeout=wait + 15.0,
                job_id=str(job_id),
                timeout=wait,
                since=offset,
            )
        else:
            reply = client.op("poll", _timeout=10.0, job_id=str(job_id), since=offset)
    except KernelBusy as exc:
        raise ToolError("kernel busy: %s" % exc)
    except KernelError as exc:
        raise ToolError(str(exc))
    job = reply["job"]
    parts = [job.get("stdout") or ""]
    if job.get("state") == "done":
        if job.get("error"):
            parts.append(job["error"])
        elif job.get("repr"):
            parts.append(job["repr"])
        duration = job.get("duration_s")
        parts.append(
            "[job finished in %ss]" % _fmt_secs(duration if duration is not None else job.get("elapsed_s"))
        )
    elif job.get("state") == "queued" and job.get("blocked"):
        parts.append("[queued: another job is running]")
    else:
        parts.append(
            "[still running %ss; poll again with since=%d]"
            % (_fmt_secs(job.get("elapsed_s")), job.get("offset") or 0)
        )
    return _join(*parts)


def tool_reset(session="default"):
    session = str(session or "default")
    client = get_client()
    try:
        reply = client.op("reset", _timeout=10.0, session=session)
    except KernelError as exc:
        raise ToolError(str(exc))
    return "session '%s' reset (namespace empty)" % session


def tool_drop(sessions=None, all=False):
    names = []
    for item in sessions or []:
        names.append(str(item))
    if not names and not all:
        raise ToolError("nothing to drop: pass sessions=[...] or all=true")
    client = get_client()
    try:
        reply = client.op("drop", _timeout=10.0, sessions=names, all=bool(all))
    except KernelError as exc:
        raise ToolError(str(exc))
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


def tool_status():
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
    live = (snapshot.get("clients") or {}).get("live")
    if live:
        others = int(live) - 1
        lines.append(
            "clients: %d (this server%s)"
            % (live, " + %d other%s" % (others, "" if others == 1 else "s") if others > 0 else "")
        )
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


def tool_interrupt():
    client = get_client()
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


def tool_kill():
    client = get_client()
    result = client.kill()
    if not result.get("ok"):
        raise ToolError(result["text"])
    return result["text"]


def tool_run_file(path, lang="auto", args=None, timeout_s=None, stdin=None, cwd=None):
    try:
        return runfile.run(
            path=path,
            lang=lang,
            args=args,
            timeout_s=timeout_s,
            stdin=stdin,
            cwd=cwd,
        )
    except runfile.RunError as exc:
        raise ToolError(str(exc))


def tool_notebook(
    action, path=None, out=None, in_place=False, cell_timeout_s=120, timeout_s=None
):
    action = str(action or "").strip().lower()
    if action not in ("check", "run", "clear", "extract"):
        raise ToolError(
            "action must be one of check, run, clear, extract (got %r)" % (action,)
        )
    if not path:
        raise ToolError("notebook needs a path to an .ipynb file")
    try:
        if action == "check":
            return nbrun.check(path)
        if action == "extract":
            return nbrun.extract(path)
        if action == "clear":
            return nbrun.clear(path, out=out, in_place=bool(in_place))
        return nbrun.run(
            path,
            out=out,
            in_place=bool(in_place),
            cell_timeout_s=int(_as_float(cell_timeout_s, 120)),
            timeout_s=None if timeout_s is None else _as_float(timeout_s, 0),
            state_dir=get_client().state_dir,
        )
    except nbrun.NotebookError as exc:
        raise ToolError(str(exc))
    except OSError as exc:
        raise ToolError("%s: %s" % (type(exc).__name__, exc))


# --------------------------------------------------------------------------
# schemas
# --------------------------------------------------------------------------
_SESSION_PROP = {
    "type": "string",
    "default": "default",
    "description": "Namespace to run in. State persists per session between exec calls.",
}

TOOLS = [
    {
        "name": "exec",
        "description": (
            "Run Sage/Python in the long-lived kernel: stdout, then the repr of the last "
            "expression (or '(no output)'). Code runs on the kernel's main thread, one "
            "job at a time, so cysignals Sage code works; state persists per session. "
            "timeout_s is NOT a computation limit: on expiry you get the partial output "
            "and a job id, and the job keeps running (collect it with poll). "
            "timeout_s=0 returns at once. A submitted job waits behind the running one "
            "(\"[queued: another job is running]\"); if that one holds the GIL the "
            "submit itself is refused after ~10 s (\"[kernel busy ...]\", the job was "
            "not submitted) -- check status or interrupt, then retry."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Code to execute."},
                "session": _SESSION_PROP,
                "timeout_s": {
                    "type": "number",
                    "default": 120,
                    "description": "Seconds to wait for the result; 0 = return at once. "
                    "Never cancels the job.",
                },
                "preparse": {
                    "type": "boolean",
                    "default": True,
                    "description": "Apply the Sage preparser (2^10 -> 1024, R.<x> = ...). "
                    "false runs raw Python (2^10 is XOR).",
                },
            },
            "required": ["code"],
        },
    },
    {
        "name": "poll",
        "description": (
            "Collect a job started by exec. since = the offset the previous call printed "
            "(0 to start), so you get only new output. wait_s blocks until the job "
            "finishes or that many seconds pass (0 = return immediately, the default)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Job id such as j7."},
                "since": {
                    "type": "integer",
                    "default": 0,
                    "description": "Character offset already consumed; the previous call "
                    "prints the next value.",
                },
                "wait_s": {
                    "type": "number",
                    "default": 0,
                    "description": "Block up to this many seconds waiting for the job to "
                    "finish (0 = return immediately, the default).",
                },
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "reset",
        "description": "Empty a session's namespace but keep the session (see drop).",
        "inputSchema": {
            "type": "object",
            "properties": {"session": _SESSION_PROP},
            "required": [],
        },
    },
    {
        "name": "drop",
        "description": (
            "Free idle sessions; refuses one with a queued/running job (kept, and the "
            "job id is reported). all=true drops every idle session."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sessions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Session names to drop.",
                },
                "all": {
                    "type": "boolean",
                    "default": False,
                    "description": "Drop every session that has no queued/running job.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "status",
        "description": (
            "Kernel state (running / busy / not running) with pid and uptime, one line "
            "per session with its name count, active jobs, the last error, the log path."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "interrupt",
        "description": (
            "SIGINT the running computation: the job fails with KeyboardInterrupt and the "
            "session KEEPS its state -- the first thing to try for a runaway job. Refused "
            "when the running job belongs to another MCP client; a no-op when idle: SIGINT "
            "can never kill the kernel."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "kill",
        "description": (
            "Last resort: stop the kernel (a graceful shutdown, then SIGTERM, then "
            "SIGKILL). All sessions and "
            "variables are lost; the next exec starts a fresh kernel and says so. If the "
            "signal fails the state files are kept and the pid reported, so a live kernel "
            "is never orphaned."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "run_file",
        "description": (
            "Run a file that already exists and return its output inline: exit "
            "code, duration, stdout and stderr. Nothing is copied or stored -- the "
            "file you name is the file that runs, with cwd = its own directory. "
            "lang=auto guesses from the extension and honours a #! for executables; "
            "lang=python uses Sage's own interpreter (so `from sage.all import *` "
            "works) and lang=sage shells out to `sage`. A non-zero exit or a timeout "
            "is an error, with the output attached; the timeout kills the whole "
            "process group."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The file to run (absolute or relative to this server's cwd).",
                },
                "lang": {
                    "type": "string",
                    "enum": ["auto", "bash", "python", "sage", "exec"],
                    "description": "Interpreter; auto uses the extension, then the #! line.",
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Arguments appended to the command line.",
                },
                "timeout_s": {
                    "type": "number",
                    "default": 120,
                    "description": "Seconds before the process group is killed.",
                },
                "stdin": {"type": "string", "description": "Text fed to the file on stdin."},
                "cwd": {
                    "type": "string",
                    "description": "Working directory (default: the file's own directory).",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "notebook",
        "description": (
            "Jupyter notebook helper for the kernels SageMath ships. check: validate "
            "and summarise. extract: print the code cells. clear: strip outputs "
            "(atomically). run: execute with nbconvert (atomically, to "
            "<name>.out.ipynb unless out/in_place is given) and report how many cells "
            "ended in error."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["check", "run", "clear", "extract"],
                },
                "path": {"type": "string", "description": ".ipynb file."},
                "out": {
                    "type": "string",
                    "description": "Output path for run/clear (default: <name>.out.ipynb "
                    "for run, the input file for clear).",
                },
                "in_place": {
                    "type": "boolean",
                    "default": False,
                    "description": "Overwrite the input notebook.",
                },
                "cell_timeout_s": {
                    "type": "number",
                    "default": 120,
                    "description": "Per-cell execution timeout for run.",
                },
                "timeout_s": {
                    "type": "number",
                    "description": "Overall wall clock budget for run.",
                },
            },
            "required": ["action", "path"],
        },
    },
]

HANDLERS = {
    "exec": tool_exec,
    "poll": tool_poll,
    "reset": tool_reset,
    "drop": tool_drop,
    "status": tool_status,
    "interrupt": tool_interrupt,
    "kill": tool_kill,
    "run_file": tool_run_file,
    "notebook": tool_notebook,
}


def _visible_tools():
    """`notebook` needs nbformat+nbconvert+nbclient; the rest are unconditional."""
    if nbrun.available():
        return TOOLS
    return [tool for tool in TOOLS if tool["name"] != "notebook"]


# --------------------------------------------------------------------------
# JSON-RPC plumbing
# --------------------------------------------------------------------------
_REAL_OUT = None
_OUT_LOCK = threading.Lock()
_ACTIVE = 0
_ACTIVE_COND = threading.Condition()


def _emit(payload):
    data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    with _OUT_LOCK:
        try:
            buffer = getattr(_REAL_OUT, "buffer", None)
            if buffer is not None:
                buffer.write(data)
                buffer.flush()
            else:
                _REAL_OUT.write(data.decode("utf-8"))
                _REAL_OUT.flush()
        except Exception:
            pass


def _err(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _ok(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _call_tool(params):
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise ToolError("arguments must be an object")
    handler = HANDLERS.get(name)
    if handler is None or name not in [t["name"] for t in _visible_tools()]:
        raise ToolError(
            "unknown tool %r (available: %s)"
            % (name, ", ".join(t["name"] for t in _visible_tools()))
        )
    try:
        text = handler(**arguments)
    except ToolError as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    except TypeError as exc:
        return {
            "content": [{"type": "text", "text": "bad arguments for %s: %s" % (name, exc)}],
            "isError": True,
        }
    except (KernelBusy, KernelDown, KernelError) as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    except Exception as exc:
        sys.stderr.write("sage-mcp: %s failed:\n%s\n" % (name, traceback.format_exc()))
        sys.stderr.flush()
        return {
            "content": [
                {"type": "text", "text": "%s: %s" % (type(exc).__name__, exc)}
            ],
            "isError": True,
        }
    return {"content": [{"type": "text", "text": text if isinstance(text, str) else str(text)}]}


def _handle(msg_id, method, params):
    if method == "initialize":
        requested = params.get("protocolVersion") or PROTOCOL_FALLBACK
        result = {
            "protocolVersion": requested,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "sage-mcp", "version": VERSION},
            "instructions": (
                "A persistent Sage kernel. exec runs code on the kernel's main thread "
                "(state persists per session); timeout_s never cancels a job -- collect "
                "it with poll. interrupt keeps the session's variables; kill is the "
                "last resort. run_file executes a file the agent already wrote and "
                "returns the output inline (python = Sage's interpreter); notebook "
                "drives SageMath's Jupyter kernel."
            ),
        }
        return _ok(msg_id, result)
    if method == "ping":
        return _ok(msg_id, {})
    if method == "tools/list":
        return _ok(msg_id, {"tools": _visible_tools()})
    if method == "tools/call":
        return _ok(msg_id, _call_tool(params))
    if method == "resources/list":
        return _ok(msg_id, {"resources": []})
    if method == "resources/templates/list":
        return _ok(msg_id, {"resourceTemplates": []})
    if method == "prompts/list":
        return _ok(msg_id, {"prompts": []})
    return _err(msg_id, -32601, "method not found: %s" % (method,))


def _serve(msg_id, method, params):
    try:
        reply = _handle(msg_id, method, params)
    except ToolError as exc:
        reply = _ok(
            msg_id,
            {"content": [{"type": "text", "text": str(exc)}], "isError": True},
        )
    except Exception:
        sys.stderr.write("sage-mcp: %s crashed:\n%s\n" % (method, traceback.format_exc()))
        sys.stderr.flush()
        reply = _err(msg_id, -32603, "internal error in %s" % (method,))
    finally:
        global _ACTIVE
        with _ACTIVE_COND:
            _ACTIVE -= 1
            _ACTIVE_COND.notify_all()
    _emit(reply)


def main(argv=None):
    global _REAL_OUT
    for stream, errors in ((sys.stdin, "replace"), (sys.stdout, None), (sys.stderr, "replace")):
        try:
            stream.reconfigure(encoding="utf-8", errors=errors)
        except (AttributeError, ValueError):
            pass
    _REAL_OUT = sys.stdout
    # Rule 15 from the very first line: only _emit() may touch the real stdout.
    sys.stdout = sys.stderr

    client = get_client()
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                _emit(_err(None, -32700, "parse error"))
                continue
            if not isinstance(msg, dict):
                _emit(_err(None, -32600, "invalid request"))
                continue
            msg_id = msg.get("id")
            method = msg.get("method")
            if not isinstance(method, str):
                if msg_id is not None:
                    _emit(_err(msg_id, -32600, "invalid request: no method"))
                continue
            params = msg.get("params") or {}
            if not isinstance(params, dict):
                params = {}
            if msg_id is None:  # notification (e.g. notifications/initialized)
                continue
            global _ACTIVE
            with _ACTIVE_COND:
                _ACTIVE += 1
            threading.Thread(
                target=_serve, args=(msg_id, method, params), daemon=True
            ).start()
    except KeyboardInterrupt:
        pass
    finally:
        # Let in-flight calls finish: a piped client may close stdin early.
        with _ACTIVE_COND:
            _ACTIVE_COND.wait_for(lambda: _ACTIVE <= 0, timeout=30.0)
        try:  # never leave an orphaned Sage process behind -- but another MCP
            # server (another Pi session or subagent) may share this kernel, so
            # only the last one out may stop it.
            client.stop_if_last()
        except Exception:
            pass


if __name__ == "__main__":
    main()
