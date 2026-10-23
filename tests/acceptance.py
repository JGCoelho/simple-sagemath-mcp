#!/usr/bin/env python3
"""acceptance.py -- drives sage_mcp.py over stdio and runs the spec's §9 suite.

    python tests/acceptance.py [--python /path/to/sageenv/python]

Every line the server writes to stdout is parsed as JSON-RPC; a single
non-JSON line fails the whole run (rule 15).
"""

import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "sage_mcp.py")
def _sage_python():
    """An interpreter that can import sage.all: $SAGE_PYTHON, else the python
    beside `sage` on PATH, else this one."""
    explicit = os.environ.get("SAGE_PYTHON")
    if explicit and os.path.exists(explicit):
        return explicit
    sage_bin = shutil.which("sage")
    if sage_bin:
        candidate = os.path.join(os.path.dirname(sage_bin), "python")
        if os.path.exists(candidate):
            return candidate
    return sys.executable


PYTHON = _sage_python()

RESULTS = []
BAD_OUTPUT = []


def record(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    print("[%s] %s" % (mark, name))
    if detail:
        for line in str(detail).rstrip().splitlines():
            print("       " + line)


class Server:
    def __init__(self, state_dir):
        env = os.environ.copy()
        env["SAGE_MCP_STATE"] = state_dir
        env["PATH"] = os.path.dirname(PYTHON) + os.pathsep + env.get("PATH", "")
        self.proc = subprocess.Popen(
            [PYTHON, SERVER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=ROOT,
        )
        self.next_id = 0
        self.lock = threading.Lock()
        self.waiters = {}
        self.stderr_lines = []
        self.reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader.start()
        self.err_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self.err_reader.start()
        self.initialize()

    def _read_stdout(self):
        for raw in self.proc.stdout:
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except ValueError:
                BAD_OUTPUT.append(text)
                continue
            if not isinstance(msg, dict) or "jsonrpc" not in msg:
                BAD_OUTPUT.append(text)
                continue
            msg_id = msg.get("id")
            with self.lock:
                waiter = self.waiters.pop(msg_id, None)
            if waiter is not None:
                waiter.put(msg)

    def _read_stderr(self):
        for raw in self.proc.stderr:
            self.stderr_lines.append(raw.decode("utf-8", "replace").rstrip())

    def call(self, method, params=None, timeout=120.0, notify=False):
        with self.lock:
            self.next_id += 1
            msg_id = self.next_id
            waiter = queue.Queue()
            if not notify:
                self.waiters[msg_id] = waiter
        payload = {"jsonrpc": "2.0", "method": method}
        if not notify:
            payload["id"] = msg_id
        if params is not None:
            payload["params"] = params
        self.proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        if notify:
            return None
        try:
            reply = waiter.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError("timed out waiting for %s" % method)
        if "error" in reply:
            raise AssertionError("%s failed: %s" % (method, reply["error"]))
        return reply["result"]

    def initialize(self):
        result = self.call(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "acceptance", "version": "1"},
            },
            timeout=30,
        )
        self.call("notifications/initialized", None, notify=True)
        return result

    def tool(self, tool_name, **arguments):  # `name` is a tool argument now
        reply = self.call(
            "tools/call", {"name": tool_name, "arguments": arguments}, timeout=300
        )
        text = "".join(
            block.get("text", "")
            for block in reply.get("content") or []
            if block.get("type") == "text"
        )
        return text, bool(reply.get("isError")), reply

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


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
    record("keep_me survived (5) before kill", out.strip() == "5", out)

    out, is_err, _ = server.tool("kill")
    record("kill reports the stopped kernel", "kernel stopped" in out, out)

    out, _, _ = server.tool("exec", code="1+1")
    record(
        "exec after kill: '[kernel restarted: ...]' prefix + fresh namespace",
        out.startswith("[kernel restarted:") and out.rstrip().endswith("2"),
        out,
    )
    out, _, _ = server.tool("exec", code="keep_me")
    record(
        "the pre-kill variable is gone after kill",
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
    if "notebook" in [t["name"] for t in server.call("tools/list")["tools"]]:
        notebook_suite(server, workdir)
    else:
        print("[SKIP] notebook (nbformat/nbconvert/nbclient not importable)")

    # tool list
    tools = server.call("tools/list")["tools"]
    names = [t["name"] for t in tools]
    record(
        "tools/list advertises the seven kernel tools plus run_file",
        names[:8] == ["exec", "poll", "reset", "drop", "status", "interrupt",
                      "kill", "run_file"]
        and set(names[8:]) <= {"notebook"},
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
        "interrupt vs kill documented",
        "KEEPS" in descriptions["interrupt"] and "Last resort" in descriptions["kill"],
        "",
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

    # rule 13: a stale port file is not adopted
    stale_dir = os.path.join(workdir, "stale")
    os.makedirs(stale_dir, exist_ok=True)
    bogus = socket.socket()
    bogus.bind(("127.0.0.1", 0))
    bogus.listen(1)
    with open(os.path.join(stale_dir, "kernel.json"), "w") as fh:
        json.dump(
            {
                "pid": os.getpid(),
                "port": bogus.getsockname()[1],
                "started": time.time() - 5000,
                "version": 1,
            },
            fh,
        )
    stale_client = sc.Client(stale_dir)
    state, restarted, reason = stale_client.ensure()
    record(
        "rule 13: stale port file is replaced, not adopted",
        state["pid"] != os.getpid()
        and state["port"] != bogus.getsockname()[1]
        and "stale" in (reason or ""),
        "reason=%r pid=%s port=%s (stale pid=%s port=%s)"
        % (reason, state.get("pid"), state.get("port"), os.getpid(), bogus.getsockname()[1]),
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
        json.dump({"pid": 999999999, "port": 1, "started": time.time(), "version": 1}, fh)
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

    record(
        "rule 15: stdout carried only JSON-RPC",
        not BAD_OUTPUT,
        "\n".join(BAD_OUTPUT[:5]),
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
