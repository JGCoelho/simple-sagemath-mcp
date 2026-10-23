#!/usr/bin/env python3
"""acceptance.py -- drives sage_mcp.py through the official MCP client.

    python tests/acceptance.py [--python /path/to/sageenv/python]

Every request goes through `mcp.client.stdio` + `ClientSession`, i.e. the same
code path a real client uses; the suite tests the wire contract, not a private
harness.  Rule 15 (nothing but JSON-RPC on stdout) is measured directly by
`stdout_hygiene()`: a stray handler print would break the session, and does not.
"""

import asyncio
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "sage_mcp.py")


def _sage_python():
    """Sage's interpreter: $SAGE_PYTHON, else the python beside `sage`.

    The *kernel* runs there; the server may run on a plain Python, which is why
    PYTHON (below) is this process's interpreter and this one only widens PATH.
    """
    explicit = os.environ.get("SAGE_PYTHON")
    if explicit and os.path.exists(explicit):
        return explicit
    sage_bin = shutil.which("sage")
    if sage_bin:
        for name in ("python", "python3"):
            candidate = os.path.join(os.path.dirname(os.path.realpath(sage_bin)), name)
            if os.path.exists(candidate):
                return candidate
    return sys.executable


PYTHON = sys.executable  # the server's interpreter: this one (it imports `mcp`)
SAGE_PYTHON = _sage_python()  # the kernel's: discovered by the server at runtime

RESULTS = []
ERROR_TEXT = []  # isError results whose own message did not survive the SDK
NOISE_MARKER = "stray handler stdout"  # printed by exec under SAGE_MCP_STDOUT_NOISE


def record(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    print("[%s] %s" % (mark, name))
    if detail:
        for line in str(detail).rstrip().splitlines():
            print("       " + line)


def server_params(state_dir, extra_env=None):
    env = os.environ.copy()
    env["SAGE_MCP_STATE"] = state_dir
    env.setdefault("SAGE_CWD", "/tmp")
    # The server discovers Sage itself; PATH is what makes that possible when it
    # runs on a plain Python (two interpreters: PLAN §1.2).
    dirs = [os.path.dirname(PYTHON)]
    if os.path.dirname(SAGE_PYTHON) not in dirs:
        dirs.append(os.path.dirname(SAGE_PYTHON))
    env["PATH"] = os.pathsep.join(dirs + [env.get("PATH", "")])
    env.update(extra_env or {})
    return StdioServerParameters(command=PYTHON, args=[SERVER], env=env, cwd=ROOT)


class Server:
    """One server process, driven synchronously through the official client.

    The suite is synchronous and the MCP client is asyncio-only, so the session
    lives on its own event loop in a daemon thread and every call is submitted
    to it.  `close()` stops that loop with the client's context managers still
    open, which closes the server's stdin -- exactly a client shutting down.
    """

    def __init__(self, state_dir, extra_env=None, errlog=None):
        self.state_dir = state_dir
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._session = None
        self._ready = threading.Event()
        self._closed = False
        self._errlog = errlog or sys.stderr
        self._task = asyncio.run_coroutine_threadsafe(
            self._serve(state_dir, extra_env), self._loop
        )
        if not self._ready.wait(180):
            raise AssertionError("the server did not answer initialize")
        if self._session is None:  # _serve failed before the session existed
            self.close()
            raise AssertionError("the server failed to start: %s" % (self._task.exception(),))

    async def _serve(self, state_dir, extra_env):
        self._stop = asyncio.Event()
        try:
            async with stdio_client(
                server_params(state_dir, extra_env), errlog=self._errlog
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._session = session
                    self._ready.set()
                    await self._stop.wait()
        finally:
            self._ready.set()

    def _result(self, coro, what, timeout=300.0):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise AssertionError("timed out waiting for %s" % what)

    def tools(self):
        listing = self._result(self._session.list_tools(), "tools/list", timeout=60)
        return [
            {
                "name": tool.name,
                "description": tool.description or "",
                "inputSchema": tool.input_schema or {},
            }
            for tool in listing.tools
        ]

    def tool(self, tool_name, **arguments):  # `name` is a tool argument now
        result = self._result(
            self._session.call_tool(tool_name, arguments), "tools/call %s" % tool_name
        )
        text = "".join(
            getattr(block, "text", "")
            for block in result.content or []
            if getattr(block, "type", None) == "text"
        )
        is_error = bool(result.is_error)
        if is_error:
            text = _strip_error_prefix(tool_name, text)
        return text, is_error, result

    def cancel_tool(self, tool_name, arguments, after=0.5):
        """Send a tools/call and cancel it after `after` seconds (C13)."""
        future = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(tool_name, arguments), self._loop
        )
        time.sleep(after)
        was_running = not future.done()
        future.cancel()
        try:
            future.result(10)
        except BaseException:  # CancelledError, or the reply the cancel discarded
            pass
        return was_running

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._loop.call_soon_threadsafe(self._stop.set)
            self._task.result(90)
        except BaseException:  # a shutdown hiccup must not mask the test result
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(20)


def _strip_error_prefix(tool_name, text):
    """Undo the SDK's fixed prefix and keep the message the handler raised.

    `mcp` renders a raised `ToolError` as "Error executing tool <name>: <msg>".
    The prefix is fixed, so it is checked to be exactly that before it is
    dropped; a bare "Error executing tool <name>" means a *crash* (the SDK
    withholds the exception text) and is recorded rather than read as a
    diagnosis.
    """
    fixed = "Error executing tool %s" % tool_name
    if text.startswith(fixed + ": "):
        return text[len(fixed) + 2:]
    ERROR_TEXT.append("%s: %r" % (tool_name, text[:200]))
    return text


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
def make_notebook(path):
    import nbformat
    from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook

    nb = new_notebook()
    nb.metadata["kernelspec"] = {
        "name": "sagemath",
        "display_name": "SageMath",
        "language": "sage",
    }
    nb.metadata["language_info"] = {"name": "sage"}
    nb.cells = [
        new_markdown_cell("# scratch notebook"),
        new_code_cell("print('hello from the notebook')"),
        new_code_cell("2 + 2"),
    ]
    with open(path, "w", encoding="utf-8") as fh:
        nbformat.write(nb, fh)
    return path


def run_file_suite(server, workdir):
    """run_file executes a file the agent wrote itself; nothing is stored."""
    scripts = os.path.join(workdir, "scripts")
    os.makedirs(scripts, exist_ok=True)

    def write(name, body, mode=0o644):
        path = os.path.join(scripts, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(path, mode)
        return path

    hello = write(
        "hello.sh", "#!/bin/bash\necho hello from the file\necho warn >&2\npwd\n", 0o755
    )
    before = (sorted(os.listdir(scripts)), open(hello).read())
    text, is_err, _ = server.tool("run_file", path=hello)
    record(
        "run_file runs an existing file and returns its output inline",
        not is_err
        and text.startswith("exit=0")
        and "hello from the file" in text
        and "--- stderr ---" in text
        and "warn" in text,
        text,
    )
    record(
        "cwd is the file's own directory, and nothing is copied in",
        not is_err
        and ("cwd=" + scripts) in text
        and "\n" + scripts + "\n" in text
        and (sorted(os.listdir(scripts)), open(hello).read()) == before,
        text,
    )

    boom = write("boom.sh", "echo before\nexit 3\n")
    text, is_err, _ = server.tool("run_file", path=boom)
    record(
        "a non-zero exit is an error, with the output still returned",
        is_err and text.startswith("exit=3") and "before" in text,
        text.splitlines()[0] if text else "",
    )

    slow = write("slow.sh", "sleep 30\n")
    t0 = time.time()
    text, is_err, _ = server.tool("run_file", path=slow, timeout_s=1.5)
    elapsed = time.time() - t0
    record(
        "timeout_s kills the whole process group and says so",
        is_err
        and text.startswith("timeout after 1.5s")
        and "process group" in text
        and elapsed < 15,
        "%s (%.1fs)" % (text.splitlines()[0] if text else "", elapsed),
    )

    alg = write("algebra.py", "from sage.all import factor\nprint(factor(12))\n")
    text, is_err, _ = server.tool("run_file", path=alg)
    record(
        "lang=python is Sage's own interpreter (sage.all imports)",
        not is_err and "2^2 * 3" in text,
        text.splitlines()[-1] if text else "",
    )

    vsage = write("power.sage", "print(2^10)\n")
    text, is_err, _ = server.tool("run_file", path=vsage)
    record(
        "a .sage file runs through the `sage` command",
        not is_err and "1024" in text and "lang=sage" in text,
        text.splitlines()[1] if text else "",
    )

    argv = write("argv.sh", 'printf "%s|" "$@"\n')
    text, is_err, _ = server.tool("run_file", path=argv, args=["a b", "c"])
    record(
        "args are passed through verbatim",
        not is_err and "a b|c|" in text,
        text.splitlines()[-1] if text else "",
    )

    reader = write("stdin.sh", "read -r line\necho got=$line\n")
    text, is_err, _ = server.tool("run_file", path=reader, stdin="hello stdin\n")
    record("stdin is fed to the file", not is_err and "got=hello stdin" in text, text)

    big = write("big.sh", "yes x | head -c 300000\n")
    text, is_err, _ = server.tool("run_file", path=big)
    record(
        "huge output is capped inline, with the real size reported",
        not is_err and "more bytes ...]" in text and len(text) < 40000,
        "%d chars back" % (len(text),),
    )

    hi = write("hi", "#!/usr/bin/env bash\necho shebang wins\n", 0o755)
    text, is_err, _ = server.tool("run_file", path=hi)
    record(
        "an extensionless executable runs via its #! line",
        not is_err and "shebang wins" in text and "lang=exec" in text,
        text.splitlines()[1] if text else "",
    )

    plain = write("noext", "just text\n")
    text, is_err, _ = server.tool("run_file", path=plain)
    record(
        "an un-runnable file asks for lang= instead of guessing",
        is_err and "pass lang=" in text,
        text,
    )

    text, is_err, _ = server.tool("run_file", path=os.path.join(scripts, "nope.sh"))
    record("a missing file is a clear error (not a NameError)",
           is_err and "no such file" in text and "NameError" not in text, text)


def notebook_suite(server, workdir):
    nb = make_notebook(os.path.join(workdir, "scratch.ipynb"))
    out, is_err, _ = server.tool("notebook", action="check", path=nb)
    record(
        "notebook check",
        not is_err and out.startswith("OK   ") and "cells=3 (code=2" in out,
        out,
    )
    out, is_err, _ = server.tool("notebook", action="extract", path=nb)
    record(
        "notebook extract",
        not is_err and out.startswith("# extracted from") and "2 + 2" in out,
        out,
    )
    out, is_err, _ = server.tool("notebook", action="run", path=nb, cell_timeout_s=60)
    expected = os.path.join(workdir, "scratch.out.ipynb")
    record(
        "notebook run writes <name>.out.ipynb",
        not is_err
        and out.startswith("wrote ")
        and os.path.exists(expected)
        and "cells with errors: 0" in out,
        out,
    )
    out, is_err, _ = server.tool("notebook", action="clear", path=expected, in_place=True)
    record(
        "notebook clear strips the executed outputs",
        not is_err and "cleared outputs in 2 code cells" in out,
        out,
    )


# --------------------------------------------------------------------------
# the suite
# --------------------------------------------------------------------------
def suite(server, workdir):
    out, err, _ = server.tool("exec", code="1+1")
    record("exec 1+1 -> 2", out.strip() == "2", out)

    server.tool("exec", code="keep_me = 5")
    out, err, _ = server.tool("exec", code="keep_me + 1")
    record("state persists (x=5; x+1 -> 6)", out.strip() == "6", out)

    out, _, _ = server.tool("exec", code="2^10")
    record("preparse on: 2^10 -> 1024", out.strip() == "1024", out)

    out, _, _ = server.tool("exec", code="2^10", preparse=False)
    record("preparse off: 2^10 -> 8", out.strip() == "8", out)

    server.tool("exec", code="y = 1", session="A")
    out, is_err, _ = server.tool("exec", code="y", session="B")
    record("session isolation (NameError in B)", "NameError" in out, out.splitlines()[-1] if out else "")

    out, _, _ = server.tool("exec", code="print('α⊕∩ℤ')")
    record("unicode round trip", out.strip() == "α⊕∩ℤ", repr(out))

    started = time.time()
    out, _, _ = server.tool("exec", code="time.sleep(3)", timeout_s=1)
    took = time.time() - started
    job_id = None
    for token in out.replace("[", " ").replace("]", " ").split():
        if token.startswith("j") and token[1:].isdigit():
            job_id = token
            break
    record(
        "timeout_s=1 on sleep(3) returns a job id",
        job_id is not None and took < 2.5 and "[still running" in out,
        out,
    )
    if job_id:
        deadline = time.time() + 20
        final = ""
        while time.time() < deadline:
            final, _, _ = server.tool("poll", job_id=job_id, since=0)
            if "[job finished in" in final:
                break
            time.sleep(0.2)
        record(
            "poll collects the sleep(3) job (~3s)",
            "[job finished in" in final and any(
                abs(float(part) - 3.0) < 1.2
                for part in _numbers_between(final, "[job finished in ", "s]")
            ),
            final,
        )

    started = time.time()
    out, _, _ = server.tool(
        "exec", code="import time; time.sleep(2); blocking = 7", timeout_s=0
    )
    blocking_id = _first_job_id(out)
    waited, _, _ = server.tool("poll", job_id=blocking_id, since=0, wait_s=30)
    took = time.time() - started
    record(
        "poll(wait_s) blocks until the job finishes (one call)",
        "[job finished in" in waited and 1.5 <= took <= 25.0,
        "%.1fs | %s" % (took, waited.splitlines()[-1] if waited.strip() else ""),
    )

    out, _, _ = server.tool("exec", code="import time; time.sleep(4)", timeout_s=0)
    slow_id = _first_job_id(out)
    t0 = time.time()
    part, _, _ = server.tool("poll", job_id=slow_id, since=0, wait_s=1)
    took = time.time() - t0
    record(
        "poll(wait_s=1) gives up early and stays resumable",
        "[still running" in part and 0.8 <= took <= 3.5,
        "%.1fs | %s" % (took, part.splitlines()[-1] if part.strip() else ""),
    )
    text = part
    deadline = time.time() + 25
    while time.time() < deadline:
        text, _, _ = server.tool("poll", job_id=slow_id, since=0, wait_s=15)
        if "[job finished in" in text:
            break
    record(
        "blocking poll drains the rest",
        "[job finished in" in text,
        text.splitlines()[-1] if text.strip() else "",
    )

    server.tool("exec", code="survivor = 99")
    out, _, _ = server.tool("exec", code="import time; time.sleep(60)", timeout_s=1)
    server.tool("interrupt")
    time.sleep(0.8)
    job_id = _first_job_id(out)
    final, _, _ = server.tool("poll", job_id=job_id, since=0)
    record(
        "interrupt -> KeyboardInterrupt in the job",
        "KeyboardInterrupt" in final,
        final,
    )
    after, _, _ = server.tool("exec", code="survivor")
    record(
        "interrupt keeps the session state",
        after.strip() == "99",
        after,
    )

    out, _, _ = server.tool(
        "exec",
        code="R.<x> = PolynomialRing(QQ); factor(x^15 - 1)",
        timeout_s=120,
    )
    record(
        "factor() runs on the main thread (no SignalError)",
        "Segmentation fault" not in out
        and "SignalError" not in out
        and "(x - 1)" in out
        and "(x^2 + x + 1)" in out,
        out,
    )

    out, _, _ = server.tool("exec", code="1/0")
    st, _, _ = server.tool("status")
    line = [row for row in st.splitlines() if row.startswith("last error")][0]
    record(
        "status indexes the exception, not the Traceback header",
        "ZeroDivisionError" in line and "Traceback" not in line,
        line,
    )

    out, _, _ = server.tool("exec", code="accepted = 1", timeout_s=0)
    record(
        "idle timeout_s=0 does not claim another job is running",
        "another job is running" not in out and "job_id" in out,
        out.splitlines()[-1] if out.strip() else "",
    )

    out, _, _ = server.tool(
        "exec", code="import time; time.sleep(2)", session="behind", timeout_s=0
    )
    first_id = _first_job_id(out)
    time.sleep(0.3)
    queued, _, _ = server.tool("exec", code="1+1", session="behind", timeout_s=0)
    second_id = _first_job_id(queued)
    record(
        "submitting while a job runs says so (queued behind)",
        "another job is running" in queued,
        queued,
    )
    for jid in (first_id, second_id):
        deadline = time.time() + 25
        while jid and time.time() < deadline:
            text, _, _ = server.tool("poll", job_id=jid, since=0)
            if "[job finished in" in text:
                break
            time.sleep(0.2)
    server.tool("drop", sessions=["behind"])

    out, _, _ = server.tool("status")
    record(
        "status: pid + sessions + log path",
        "kernel: running" in out and "pid " in out and "session default:" in out and "log: " in out,
        out,
    )

    out, _, _ = server.tool("reset", session="A")
    record("reset empties the namespace", "session 'A' reset" in out, out)

    server.tool("exec", code="z = 1", session="idle-one")
    out, _, _ = server.tool("drop", sessions=["idle-one"])
    record(
        "drop frees an idle session",
        out.startswith("dropped: idle-one") and "remaining:" in out,
        out,
    )

    # drop must refuse a session with work in flight
    server.tool("exec", code="import time; time.sleep(30)", session="busy-one", timeout_s=0)
    out, _, _ = server.tool("drop", sessions=["busy-one"])
    record(
        "drop refuses a session with a running job",
        "kept 'busy-one': job" in out,
        out,
    )
    out, _, _ = server.tool("status")
    record(
        "status lists the active job",
        "active jobs: j" in out and "(running)" in out,
        [line for line in out.splitlines() if line.startswith("active")][0],
    )
    server.tool("interrupt")

    out, _, _ = server.tool("exec", code="keep_me")
    record("keep_me survived (5) before force-interrupt", out.strip() == "5", out)

    out, is_err, _ = server.tool("interrupt", force=True)
    record(
        "interrupt(force=true) stops the kernel and says so", "kernel stopped" in out, out
    )

    out, _, _ = server.tool("exec", code="1+1")
    record(
        "exec after interrupt(force=true): '[kernel restarted: ...]' + fresh namespace",
        out.startswith("[kernel restarted:") and out.rstrip().endswith("2"),
        out,
    )
    out, _, _ = server.tool("exec", code="keep_me")
    record(
        "the pre-force variable is gone after the kernel was stopped",
        "NameError" in out,
        out.splitlines()[-1] if out else "",
    )

    # rule 8: repr truncation
    out, _, _ = server.tool("exec", code="tuple(range(3000))")
    record(
        "repr truncated at 4000 chars",
        "repr truncated" in out and len(out) < 4200,
        "len=%d" % len(out),
    )

    # rule 7: a syntax error carries the preparsed source
    out, _, _ = server.tool("exec", code="2 +++")
    record(
        "SyntaxError attaches the preparsed source",
        "SyntaxError" in out and "preparsed source" in out,
        out[:400],
    )

    # Two MCP servers (a Pi session and a subagent) share one kernel: the first
    # one to exit must not kill the job the other one is still collecting.
    second = Server(os.path.join(workdir, "state"))
    try:
        out, _, _ = second.tool(
            "exec", code="import time; time.sleep(9); shared = 1", timeout_s=0
        )
        shared_job = _first_job_id(out)
        out, _, _ = server.tool("status")  # also marks this server as a live client
        line = [ln for ln in out.splitlines() if ln.startswith("clients:")]
        record(
            "status counts the live MCP clients",
            line == ["clients: 2 (this server + 1 other)"],
            line or out.splitlines()[0],
        )
        text, is_err, _ = server.tool("interrupt")
        record(
            "interrupt refuses a job submitted by another server",
            is_err and "refused" in text and "not yours" in text,
            text.splitlines()[0] if text.strip() else "",
        )
        second.close()
        time.sleep(1.5)
        text, _, _ = server.tool("poll", job_id=shared_job, since=0, wait_s=40)
        record(
            "a server that exits leaves the other server's job running",
            "[job finished in" in text,
            text.splitlines()[-1] if text.strip() else "",
        )
    finally:
        second.close()

    run_file_suite(server, workdir)
    if "notebook" in [t["name"] for t in server.tools()]:
        notebook_suite(server, workdir)
    else:
        print("[SKIP] notebook (nbformat/nbconvert/nbclient not importable)")

    # tool list
    tools = server.tools()
    names = [t["name"] for t in tools]
    record(
        "tools/list advertises the six kernel tools plus run_file",
        names[:7] == ["exec", "poll", "reset", "drop", "status", "interrupt",
                      "run_file"]
        and set(names[7:]) <= {"notebook"},
        ", ".join(names),
    )
    poll_props = [t for t in tools if t["name"] == "poll"][0]["inputSchema"]["properties"]
    record(
        "poll advertises wait_s as optional",
        "wait_s" in poll_props and "wait_s" not in (
            poll_props and [t for t in tools if t["name"] == "poll"][0]["inputSchema"].get("required", [])
        ),
        ", ".join(sorted(poll_props)),
    )
    descriptions = {t["name"]: t["description"] for t in tools}
    record(
        "interrupt documents its force=true escape hatch",
        "KEEPS" in descriptions["interrupt"]
        and "last resort" in descriptions["interrupt"].lower(),
        "",
    )
    interrupt_schema = [t for t in tools if t["name"] == "interrupt"][0]["inputSchema"]
    record(
        "interrupt takes only an optional force flag",
        list(interrupt_schema["properties"]) == ["force"]
        and interrupt_schema.get("required", []) == [],
        ", ".join(sorted(interrupt_schema["properties"])),
    )


def _numbers_between(text, left, right):
    found = []
    for chunk in text.split(left)[1:]:
        value = chunk.split(right)[0].strip()
        try:
            found.append(float(value))
        except ValueError:
            pass
    return found


def _first_job_id(text):
    for token in text.replace("[", " ").replace("]", " ").replace(",", " ").split():
        token = token.strip("[]():;")
        if token.startswith("j") and token[1:].isdigit():
            return token
    for token in text.split():
        token = token.strip("[]():;,")
        if token.startswith("j") and token[1:].isdigit():
            return token
    return None


# --------------------------------------------------------------------------
# client-level invariants (rules 11, 12, 13, 14)
# --------------------------------------------------------------------------
def client_invariants(workdir):
    sys.path.insert(0, ROOT)
    import sage_client as sc

    # rule 11: a wedged (GIL-holding) kernel is busy, not dead, and is not respawned
    state_dir = os.path.join(workdir, "busy")
    client = sc.Client(state_dir)
    state, _, _ = client.ensure()
    pid = state["pid"]

    # security: the kernel is reachable only through a private AF_UNIX socket
    dir_mode = stat.S_IMODE(os.stat(state_dir).st_mode)
    sock_mode = os.stat(client.sock_path).st_mode
    record(
        "security: state dir is private (0700)",
        dir_mode == 0o700,
        "mode=%03o path=%s" % (dir_mode, state_dir),
    )
    record(
        "security: kernel serves an AF_UNIX socket, no TCP port",
        stat.S_ISSOCK(sock_mode) and state.get("sock") == client.sock_path,
        "state=%r mode=%s" % (state, sock_mode),
    )
    os.chmod(state_dir, 0o755)
    sc.Client(state_dir)  # constructing a client repairs the permissions
    record(
        "security: a loosened state dir is tightened back to 0700",
        stat.S_IMODE(os.stat(state_dir).st_mode) == 0o700,
    )
    client.op(
        "submit",
        wait=5,
        session="default",
        code="import sys; sys.setswitchinterval(600)",
        preparse=True,
    )
    client.op("submit", wait=0, session="default", code="i=0\nwhile i < 10**9: i += 1", preparse=True)
    time.sleep(0.3)
    busy = False
    try:
        client.hello(timeout=1.0)
    except sc.KernelBusy:
        busy = True
    except sc.KernelDown as exc:
        record("rule 11: busy kernel raises KernelBusy", False, str(exc))
        return
    record("rule 11: busy kernel raises KernelBusy (not KernelDown)", busy)
    state2, restarted, reason = client.ensure()
    record(
        "rule 11: KernelBusy never respawns",
        restarted is False and state2["pid"] == pid and reason == "busy",
        "restarted=%r reason=%r pid=%s" % (restarted, reason, state2.get("pid")),
    )
    client.interrupt()
    time.sleep(0.8)
    client.op("submit", wait=10, session="default", code="import sys; sys.setswitchinterval(0.005)", preparse=True)
    record("rule 11: kernel recovered after interrupt", client.status_snapshot()["state"] == "running")
    client.kill()

    # rule 13: a stale socket file is not adopted
    stale_dir = os.path.join(workdir, "stale")
    os.makedirs(stale_dir, exist_ok=True)
    bogus_path = os.path.join(stale_dir, "kernel.sock")
    bogus = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    bogus.bind(bogus_path)  # bound but not listening: connect() is refused
    with open(os.path.join(stale_dir, "kernel.json"), "w") as fh:
        json.dump(
            {
                "pid": os.getpid(),
                "sock": bogus_path,
                "started": time.time() - 5000,
                "version": 1,
            },
            fh,
        )
    stale_client = sc.Client(stale_dir)
    state, restarted, reason = stale_client.ensure()
    record(
        "rule 13: stale socket/state files are replaced, not adopted",
        state["pid"] != os.getpid()
        and state["sock"] == bogus_path
        and "stale" in (reason or ""),
        "reason=%r pid=%s sock=%s (stale pid=%s)"
        % (reason, state.get("pid"), state.get("sock"), os.getpid()),
    )
    stale_client.kill()
    bogus.close()

    # rule 14: a dead launcher is reported at once, with the log tail
    dead_dir = os.path.join(workdir, "dead")
    os.makedirs(dead_dir, exist_ok=True)
    os.environ["SAGE_PYTHON"] = "/bin/false"
    try:
        dead_client = sc.Client(dead_dir)
        started = time.time()
        try:
            dead_client.ensure()
            record("rule 14: dead launcher reported immediately", False, "no exception")
        except sc.KernelDown as exc:
            took = time.time() - started
            record(
                "rule 14: dead launcher reported immediately with the log tail",
                took < 10 and "launcher exited" in str(exc),
                "after %.1fs: %s" % (took, str(exc).splitlines()[0]),
            )
    finally:
        os.environ.pop("SAGE_PYTHON", None)

    # rule 17: kill is honest when the signal cannot be delivered
    kill_dir = os.path.join(workdir, "killfail")
    os.makedirs(kill_dir, exist_ok=True)
    proc = subprocess.Popen(
        [
            PYTHON,
            "-c",
            "import time,sys\n"
            "open(sys.argv[1],'w').write('')\n"
            "time.sleep(30)\n",
            os.path.join(kill_dir, "ignored"),
        ],
    )
    with open(os.path.join(kill_dir, "kernel.json"), "w") as fh:
        json.dump(
            {
                "pid": 999999999,
                "sock": os.path.join(kill_dir, "kernel.sock"),
                "started": time.time(),
                "version": 1,
            },
            fh,
        )
    kill_client = sc.Client(kill_dir)
    result = kill_client.kill()
    record(
        "rule 17: a state file that cannot be verified is not signalled",
        "no kernel is running" in result["text"] and not os.path.exists(kill_client.state_path),
        result["text"],
    )
    proc.kill()
    proc.wait()



# --------------------------------------------------------------------------
# extra invariants: output routing, caps, incremental poll, spawn locking
# --------------------------------------------------------------------------
def extra_invariants(workdir):
    sys.path.insert(0, ROOT)
    import sage_client as sc

    client = sc.Client(os.path.join(workdir, "extra"))
    client.ensure()

    # rule 3 + rule 4: job output is captured per job; the kernel log is clean
    marker = "JOB-MARKER-9f3a"
    client.op("submit", wait=30, session="default", code="print(%r)" % marker, preparse=True)
    log = open(client.log_path, "r", errors="replace").read()
    record(
        "rule 3/4: job output does not leak into kernel.log",
        marker not in log,
        "" if marker not in log else "found the job's print in kernel.log",
    )

    # rule 8: 4 MB job-output cap with a notice
    job = client.op(
        "submit",
        _timeout=120,
        wait=120,
        session="default",
        code="import sys; sys.stdout.write('x' * 5_000_000)",
        preparse=False,
    )["job"]
    size = len(job["stdout"])
    record(
        "rule 8: job output capped at 4 MB with a notice",
        job["truncated"] is True
        and 4_100_000 < size < 4_200_000
        and "output truncated at 4 MB" in job["stdout"][-120:],
        "size=%d truncated=%r" % (size, job["truncated"]),
    )

    # poll: `since` returns only new output
    first = client.op(
        "submit", wait=0, session="default",
        code="import time; print('first'); time.sleep(1.0); print('second')",
        preparse=True,
    )
    jid = first["job_id"]
    time.sleep(0.4)
    part = client.op("poll", job_id=jid, since=0)["job"]
    completed = client.op("wait", _timeout=30, job_id=jid, timeout=30, since=part["offset"])["job"]
    record(
        "poll since=N returns only the new output",
        part["stdout"].strip() == "first"
        and completed["stdout"].strip() == "second"
        and completed["state"] == "done",
        "first=%r second=%r" % (part["stdout"], completed["stdout"]),
    )

    # rule 5: SIGINT outside a job is ignored, the kernel survives
    client.interrupt()
    time.sleep(0.3)
    out = client.op("submit", wait=20, session="default", code="1+1", preparse=True)["job"]
    record(
        "rule 5: SIGINT while idle does not kill the kernel",
        out["repr"] == "2" and client.status_snapshot()["state"] == "running",
        "repr=%r" % (out["repr"],),
    )

    # rule 12: concurrent spawners produce exactly one kernel
    racing_dir = os.path.join(workdir, "racing")
    os.makedirs(racing_dir, exist_ok=True)
    results = {}

    def racer(tag):
        try:
            c = sc.Client(racing_dir)
            st, _, _ = c.ensure()
            results[tag] = st["pid"]
        except Exception as exc:  # noqa: BLE001
            results[tag] = "error: %s" % exc

    threads = [threading.Thread(target=racer, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    pids = set(results.values())
    record(
        "rule 12: concurrent clients end up on one kernel",
        len(pids) == 1 and not str(list(pids)[0]).startswith("error"),
        str(results),
    )
    sc.Client(racing_dir).kill()

    client.kill()


# --------------------------------------------------------------------------
# rule 15, measured: handler stdout cannot reach the wire
# --------------------------------------------------------------------------
def stdout_hygiene(workdir):
    """A stray print inside a tool handler goes to stderr, not to the wire.

    `SAGE_MCP_STDOUT_NOISE=1` makes the exec handler print a marker; the SDK
    claims fd 1 for the wire and points it at stderr while serving.  If that
    were not so, the transport would read a non-JSON line and the session would
    break -- so "the marker is on stderr" *and* "the session still answers"
    are both asserted.
    """
    log_path = os.path.join(workdir, "noise-stderr.log")
    with open(log_path, "w", encoding="utf-8") as errlog:
        server = Server(os.path.join(workdir, "noise"), {"SAGE_MCP_STDOUT_NOISE": "1"}, errlog)
        try:
            first, first_err, _ = server.tool("exec", code="7*6")
            second, second_err, _ = server.tool("exec", code="6*7")
        finally:
            server.close()
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        noise = fh.read()
    record(
        "rule 15: a stray print in a handler reaches stderr",
        NOISE_MARKER in noise,
        "%d bytes on stderr" % len(noise),
    )
    record(
        "rule 15: the wire survives a handler print",
        first.strip() == "42" and second.strip() == "42" and not (first_err or second_err),
        "%r / %r" % (first.strip(), second.strip()),
    )


# --------------------------------------------------------------------------
# §1.5 / C13: a cancelled exec is still collectable
# --------------------------------------------------------------------------
def cancellation_recovery(workdir):
    """Cancel an `exec` request mid-job; status/poll must still recover it.

    The SDK cancels the async task, but a sync handler runs on a worker thread
    that cannot be interrupted, so the job keeps running with its output in the
    kernel while the agent never receives the job id.  `status` therefore names
    the last job submitted per session, and `poll(job_id)` drains it.
    """
    server = Server(os.path.join(workdir, "cancel"))
    try:
        # Warm-up: the first exec pays the kernel spawn (Sage takes seconds), and
        # a job cannot be lost before it was submitted.
        server.tool("exec", code="cancelled_marker = 0")
        cancelled = server.cancel_tool(
            "exec",
            {
                "code": "print('cancel-marker')\nimport time; time.sleep(4)\ncancelled_marker = 11",
                "timeout_s": 30,
            },
            after=0.6,
        )
        record("§1.5: the exec request was cancelled mid-job", cancelled)

        text, _, _ = server.tool("status")
        lines = [row for row in text.splitlines() if row.startswith("last job per session")]
        job_id = None
        if lines and "default=" in lines[0]:
            job_id = lines[0].split("default=", 1)[1].split(",")[0].strip()
        record(
            "§1.5: status names the job whose reply was cancelled",
            bool(job_id) and job_id.startswith("j"),
            lines[0] if lines else text,
        )

        final = ""
        deadline = time.time() + 40
        while job_id and time.time() < deadline:
            final, _, _ = server.tool("poll", job_id=job_id, since=0)
            if "[job finished in" in final:
                break
            time.sleep(0.3)
        record(
            "§1.5: poll recovers the cancelled job's output",
            "cancel-marker" in final and "[job finished in" in final,
            final.splitlines()[-1] if final.strip() else "(nothing)",
        )

        after, _, _ = server.tool("exec", code="cancelled_marker")
        record(
            "§1.5: the cancelled exec left its session state intact",
            after.strip() == "11",
            after.strip(),
        )
    finally:
        server.close()


# --------------------------------------------------------------------------
# phase 2: transport, authkey, adoption, discovery
# --------------------------------------------------------------------------
def transport_invariants(workdir):
    """multiprocessing.connection transport: authkey, refusal, adoption."""
    sys.path.insert(0, ROOT)
    import sage_client as sc
    from multiprocessing.connection import AuthenticationError, Client

    state_dir = os.path.join(workdir, "transport")
    client = sc.Client(state_dir)
    client.ensure()

    # two interpreters: the kernel must be the *discovered* Sage python, not
    # whatever runs this server (it may be a plain Python with only `mcp`).
    try:
        with open(client.log_path, encoding="utf-8", errors="replace") as fh:
            spawn_log = fh.read()
    except OSError:
        spawn_log = ""
    record(
        "transport: the kernel is spawned with Sage's interpreter",
        ("python=%s" % SAGE_PYTHON) in spawn_log,
        "server=%s kernel=%s" % (PYTHON, SAGE_PYTHON),
    )

    mode = stat.S_IMODE(os.stat(client.key_path).st_mode)
    record(
        "transport: the authkey is 32 bytes, 0600, inside the 0700 state dir",
        mode == 0o600
        and os.path.getsize(client.key_path) == sc.KEY_BYTES
        and stat.S_IMODE(os.stat(state_dir).st_mode) == 0o700,
        "%s mode=%03o size=%d" % (client.key_path, mode, os.path.getsize(client.key_path)),
    )

    # a raw client with a foreign authkey is refused by the kernel itself
    refused = ""
    try:
        conn = Client(client.sock_path, authkey=os.urandom(sc.KEY_BYTES))
        conn.send_bytes(b'{"op": "hello"}')
        conn.recv_bytes(4096)
        conn.close()
    except AuthenticationError as exc:
        refused = "AuthenticationError: %s" % exc
    except (OSError, EOFError) as exc:
        refused = "%s: %s" % (type(exc).__name__, exc)
    record("transport: a foreign authkey is refused", bool(refused), refused or "accepted!")
    record(
        "transport: the kernel survives a refused connection",
        client.status_snapshot()["state"] == "running",
    )

    # our own client with the wrong key file: a clear error, not a pickle traceback
    with open(client.key_path, "rb") as fh:
        good = fh.read()
    with open(client.key_path, "wb") as fh:
        fh.write(os.urandom(sc.KEY_BYTES))
    try:
        message = "(no exception)"
        try:
            sc.Client(state_dir).hello(timeout=5.0)
        except sc.KernelError as exc:
            message = str(exc)
    finally:
        with open(client.key_path, "wb") as fh:
            fh.write(good)
    record(
        "transport: a wrong authkey is reported clearly",
        "authkey" in message and "Traceback" not in message,
        message.splitlines()[0] if message else "",
    )
    record(
        "transport: the kernel survives a wrong-authkey client",
        client.op("hello")["ok"] and client.status_snapshot()["state"] == "running",
    )
    client.kill()

    # adoption: a *new MCP server* reaches the kernel the first one started
    adopt_dir = os.path.join(workdir, "adopt")
    first = Server(adopt_dir)
    keeper = Server(adopt_dir)  # a second live client: the kernel must survive
    try:
        first.tool("exec", code="adopted_value = 314")
        first_status, _, _ = first.tool("status")
        pid = _status_pid(first_status)
        keeper.tool("status")
        first.close()  # kernel kept: `keeper` is a live client
        third = Server(adopt_dir)
        try:
            text, _, _ = third.tool("exec", code="adopted_value")
            status, _, _ = third.tool("status")
        finally:
            third.close()
        record(
            "adoption: a restarted server reaches the same kernel",
            text.strip() == "314" and _status_pid(status) == pid and pid is not None,
            "%r | kernel pid %s -> %s" % (text.strip(), pid, _status_pid(status)),
        )
    finally:
        keeper.close()


def _status_pid(status):
    """The pid from a `status` line: "kernel: running, up 1s (pid 720)"."""
    for row in status.splitlines():
        if row.startswith("kernel: running") and "(pid " in row:
            return row.rsplit("(pid ", 1)[1].rstrip(")").strip()
    return None


def _resolve_in(env):
    """Run resolve_python() in a fresh process with `env` (the cache is per process)."""
    code = (
        "import sys; sys.path.insert(0, %r); import sage_client\n"
        "print(sage_client.resolve_python())\n" % ROOT
    )
    done = subprocess.run(
        [PYTHON, "-c", code], env=env, capture_output=True, text=True, timeout=180
    )
    return done.stdout.strip(), (done.stderr or "").strip()


def discovery_invariants(workdir):
    """PLAN §1.2/§9.3: $SAGE_PYTHON -> `sage -c` -> the python beside `sage`.

    Each route is measured in its own process with a controlled PATH, because
    resolve_python() caches its answer (one `sage -c` spawn per server).
    """
    base = os.environ.copy()
    base.pop("SAGE_PYTHON", None)
    expected = os.path.abspath(SAGE_PYTHON)
    real_sage = shutil.which("sage") or os.path.join(os.path.dirname(expected), "sage")

    env = dict(base, SAGE_PYTHON=SAGE_PYTHON)
    found, _ = _resolve_in(env)
    record("discovery: $SAGE_PYTHON is used first", found == expected, found)

    fakebin = os.path.join(workdir, "sage-on-path")
    os.makedirs(fakebin, exist_ok=True)
    os.symlink(real_sage, os.path.join(fakebin, "sage"))
    env = dict(base, PATH=fakebin + os.pathsep + base.get("PATH", ""))
    found, err = _resolve_in(env)
    record(
        "discovery: a `sage` on PATH is asked for its interpreter",
        found == expected,
        found or err,
    )

    broken = os.path.join(workdir, "broken-sage")
    os.makedirs(broken, exist_ok=True)
    shim = os.path.join(broken, "sage")
    with open(shim, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nexit 1\n")
    os.chmod(shim, 0o755)
    os.symlink(SAGE_PYTHON, os.path.join(broken, "python3"))
    env = dict(base, PATH=broken + os.pathsep + base.get("PATH", ""))
    found, err = _resolve_in(env)
    record(
        "discovery: the python beside a broken `sage` is the last route",
        os.path.realpath(found) == os.path.realpath(expected),
        found or err,
    )

    empty = os.path.join(workdir, "no-sage")
    os.makedirs(empty, exist_ok=True)
    env = dict(base, PATH=empty)
    found, _ = _resolve_in(env)
    code = (
        "import sys; sys.path.insert(0, %r); import sage_client\n"
        "try:\n    sage_client.sage_python()\n"
        "except sage_client.KernelError as exc:\n    print(exc)\n" % ROOT
    )
    done = subprocess.run(
        [PYTHON, "-c", code], env=env, capture_output=True, text=True, timeout=180
    )
    text = done.stdout
    record(
        "discovery: no Sage -> None, and an error naming the three routes",
        found == "None"
        and "SAGE_PYTHON" in text
        and "sage -c" in text
        and "beside" in text,
        text.strip().splitlines()[0] if text.strip() else "(no error text)",
    )


# --------------------------------------------------------------------------
def main():
    global PYTHON
    args = sys.argv[1:]
    if args and args[0] == "--python":
        PYTHON = args[1]

    workdir = tempfile.mkdtemp(prefix="sage-mcp-accept-")
    state_dir = os.path.join(workdir, "state")
    print("workdir: %s" % workdir)
    server = Server(state_dir)
    started = time.time()
    try:
        suite(server, workdir)
    finally:
        server.close()
        kernel_gone = not os.path.exists(os.path.join(state_dir, "kernel.json"))
        record(
            "server exit stops the kernel (no orphan)",
            kernel_gone,
            "" if kernel_gone else "kernel.json still present",
        )

    client_invariants(workdir)
    extra_invariants(workdir)
    transport_invariants(workdir)
    discovery_invariants(workdir)
    cancellation_recovery(workdir)
    stdout_hygiene(workdir)

    record(
        "errors reach the model with the tool's own message",
        not ERROR_TEXT,
        "\n".join(ERROR_TEXT[:5]),
    )

    failed = [name for name, ok, _ in RESULTS if not ok]
    print("\n%d checks, %d failed, %.1fs" % (len(RESULTS), len(failed), time.time() - started))
    if failed:
        print("FAILED:")
        for name in failed:
            print("  - %s" % name)
        return 1
    print("ALL PASS")
    if not os.environ.get("SAGE_MCP_KEEP_TMP"):
        shutil.rmtree(workdir, ignore_errors=True)
    else:
        print("kept %s" % workdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
