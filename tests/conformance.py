#!/usr/bin/env python3
"""conformance.py -- the MCP conformance matrix of PLAN §6, C01-C24.

    python tests/conformance.py                  # all 24 cases
    python tests/conformance.py --only C13,C15   # a subset, by case id
    python tests/conformance.py --list
    python tests/conformance.py --quiet          # only failures and the summary

One row per case (`C01 lifecycle PASS ...`) with the evidence that decided it;
the exit code is 0 only when every requested case passed.  A case that cannot be
*set up* (C13 needs a tools/call in flight, C15 needs two servers sharing one
kernel) fails with the reason: a conformance run that skipped its hard cases
would prove nothing.

Every request goes through the official client (`mcp.client.stdio` +
`ClientSession`), exactly as a user's client does.  The exceptions are the two
malformed inputs the client API cannot produce (C24: a non-JSON line and an
unknown method), which speak the same stdio transport directly; and the couple
of checks that ask the client session for what the handshake negotiated.

The suite owns its state: every server gets a fresh `SAGE_MCP_STATE` under one
temp dir, so the caller's `~/.local/state/sage-mcp` kernel is never touched.
"""

import argparse
import asyncio
import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import acceptance as A  # noqa: E402  (the synchronous facade over the official client)
import snapshot_tools as SNAP  # noqa: E402

from mcp import ClientSession, types  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402
from mcp.types.version import LATEST_HANDSHAKE_VERSION  # noqa: E402

URI = "sage://kernel/status"
TOOLS = [  # the frozen registration order (PLAN §2), not alphabetical
    "exec", "poll", "reset", "drop", "status", "interrupt", "run_file", "notebook",
]


# ---------------------------------------------------------------- harness ---
class Ctx:
    """The servers one conformance run owns, and where their state lives."""

    def __init__(self, workdir):
        self.workdir = workdir
        self._servers = []
        self._shared = None

    def dir(self, tag):
        path = os.path.join(self.workdir, tag)
        os.makedirs(path, exist_ok=True)
        return path

    def server(self, tag, extra_env=None):
        server = A.Server(self.dir(tag), extra_env)
        self._servers.append(server)
        return server

    def server_dir(self, tag, state_dir):
        """A second server on a state dir somebody else already owns (C15)."""
        server = A.Server(state_dir, None)
        self._servers.append(server)
        return server

    def shared(self):
        if self._shared is None:
            self._shared = self.server("shared")
        return self._shared

    def close(self):
        for server in reversed(self._servers):
            try:  # stop the kernel first: a closed server must not orphan one
                server.tool("interrupt", force=True)
            except BaseException:
                pass
            try:
                server.close()
            except BaseException:
                pass
        self._servers = []


def doc(server):
    """The status resource, parsed (the kernel is the source of truth)."""
    return json.loads(server.resource(URI)[1])


def calls(server):
    """Finished jobs so far; the server reports None until a kernel exists."""
    return doc(server)["jobs"].get("run") or 0


def wait_finished(server, job_id, deadline=40.0, since=0):
    end = time.time() + deadline
    text = ""
    while job_id and time.time() < end:
        text = server.tool("poll", job_id=job_id, since=since, wait_s=5)[0]
        if "[job finished in" in text:
            return text
        time.sleep(0.2)
    return text


def _job_id(text):
    found = A._first_job_id(text)
    if not found:
        raise AssertionError("no job id in %r" % text[:200])
    return found


def init_with_protocol(state_dir, version):
    """`initialize` with an explicit protocolVersion, through the official client."""

    async def run():
        async with stdio_client(A.server_params(state_dir), errlog=sys.stderr) as (read, write):
            async with ClientSession(read, write) as session:
                params = types.InitializeRequestParams(
                    protocol_version=version,
                    capabilities=types.ClientCapabilities(),
                    client_info=types.Implementation(name="conformance", version="1.0"),
                )
                result = await session.send_request(
                    types.InitializeRequest(params=params), types.InitializeResult
                )
                session.adopt(result)
                await session.send_notification(types.InitializedNotification())
                listing = await session.list_tools()
                return result, len(listing.tools)

    return asyncio.run(run())


def _raw_replies(state_dir, lines, last_id=7, wait=30.0):
    """Write raw JSON-RPC lines to the server's stdin; return the reply lines.

    Only C24 needs this: the official client refuses to send a non-JSON line, and
    a "parse error" can only be observed by producing one.  Everything else here
    is the official client.  The server is A.server_spec()'s (a registration when
    `--check` drives the subset), not this file's own copy.
    """
    command, args, env, cwd = A.server_spec()
    env = dict(env, SAGE_MCP_STATE=state_dir)
    proc = subprocess.Popen(
        [command] + args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
        cwd=cwd,
    )
    replies = []
    try:
        for line in lines:
            proc.stdin.write(line + "\n")
        proc.stdin.flush()
        end = time.time() + wait
        seen_last = False
        while time.time() < end and not seen_last:
            ready, _, _ = select.select([proc.stdout], [], [], 0.2)
            if ready:
                got = proc.stdout.readline()
                if got:
                    replies.append(got.rstrip())
                    seen_last = '"id":%d' % last_id in got.replace(" ", "")
            if proc.poll() is not None:
                break
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
    return replies


def _cancel_and_collect(server, session):
    """C13: cancel a blocking tools/call, then find and poll the job it left.

    The warm-up is a precondition of the *harness*, not of the product: on a cold
    server the first exec pays the kernel spawn (Sage takes seconds), so a cancel
    0.6 s in would land before the job was ever submitted and there would be
    nothing to recover.  It runs in its own session, so `last job per session`
    still names only the job this case submitted.  (`--only C13` on a fresh
    kernel is how this was found: it failed with a message that blamed `status`.)
    """
    server.tool("exec", code="warm = 1", session="C13-warm")
    arguments = {
        "code": "print('c13-marker')\nimport time\ntime.sleep(4)\nc13_state = 2",
        "session": session,
        "timeout_s": 120,
    }
    if not server.cancel_tool("exec", arguments, after=0.6):
        raise AssertionError(
            "harness precondition not met: the request finished before the cancel, "
            "so cancellation was never exercised (not a verdict on the server)"
        )
    match = re.search(r"\b%s=(j\d+)" % re.escape(session), server.tool("status")[0])
    if not match:
        raise AssertionError(
            "harness precondition not met: after a warm kernel the cancelled "
            "tools/call left no job in status, so the request never reached the "
            "kernel and there is nothing to collect (not a verdict on the server)"
        )
    return match.group(1), wait_finished(server, match.group(1), deadline=30)


# --------------------------------------------------------------- the cases --
def c01(ctx):
    """initialize with protocol 2024-11-05: accepted, serverInfo, tools usable."""
    result, count = init_with_protocol(ctx.dir("C01"), "2024-11-05")
    ok = (
        result.protocol_version == "2024-11-05"
        and result.server_info.name == "sage"
        and count == len(TOOLS)
    )
    return ok, "asked 2024-11-05 -> %r, serverInfo=%r, %d tools" % (
        result.protocol_version,
        result.server_info.name,
        count,
    )


def c02(ctx):
    """initialize with the newest version the SDK knows."""
    info = ctx.shared().initialize_result()
    ok = (
        info is not None
        and info.protocol_version == LATEST_HANDSHAKE_VERSION
        and info.server_info.name == "sage"
    )
    return ok, "negotiated %r (SDK newest %r), serverInfo=%r" % (
        info.protocol_version,
        LATEST_HANDSHAKE_VERSION,
        info.server_info.name,
    )


def c03(ctx):
    """ping after the handshake."""
    result = ctx.shared().ping()
    return result is not None, "empty result (%s)" % type(result).__name__


def c04(ctx):
    """initialized, then tools/list: the eight frozen tools, no `kill`."""
    names = [tool["name"] for tool in ctx.shared().tools()]
    ok = names == TOOLS and "kill" not in names
    return ok, ", ".join(names)


def c05(ctx):
    """inputSchema of every tool equals the snapshot, through the client."""
    with open(SNAP.SNAPSHOT, encoding="utf-8") as fh:
        frozen = json.load(fh)["tools"]
    session = ctx.shared()
    listing = session._result(session._session.list_tools(), "tools/list", 60)
    problems = SNAP.compare(frozen, SNAP.normalise(listing.tools))
    return not problems, "; ".join(problems) or "%d tools identical to the snapshot" % len(frozen)


def c06(ctx):
    """unknown argument to exec: the SDK drops it; no protocol damage, one job."""
    server = ctx.shared()
    before = calls(server)
    text, is_error, _ = server.tool("exec", code="c06 = 1 + 1\nc06", not_a_parameter=1)
    ok = (not is_error) and text.strip() == "2" and calls(server) == before + 1
    return ok, "unknown field ignored by mcp (no JSON-RPC error); result=%r, jobs +1" % (
        text.strip()[:40],
    )


def c07(ctx):
    """wrong argument type: a validation error, and no job submitted."""
    server = ctx.shared()
    before = calls(server)
    text, is_error, _ = server.tool("exec", code="1 + 1", timeout_s="soon")
    ok = is_error and "validation error" in text and calls(server) == before
    return ok, "isError=%s, jobs unchanged, %r" % (
        is_error,
        " ".join(text.split())[:100],
    )


def c08(ctx):
    """exec 2+2."""
    text, is_error, _ = ctx.shared().tool("exec", code="2 + 2")
    return (not is_error) and text.strip() == "4", "result=%r" % text.strip()


def c09(ctx):
    """a syntax error reports the preparsed source and leaves the session intact."""
    server = ctx.shared()
    server.tool("exec", code="c09 = 41", session="C09")
    bad, is_error, _ = server.tool("exec", code="1 +* 2", session="C09")
    kept, kept_error, _ = server.tool("exec", code="c09", session="C09")
    ok = (
        not is_error
        and "SyntaxError" in bad
        and "preparsed source" in bad
        and not kept_error
        and kept.strip() == "41"
    )
    return ok, "%r / variable survives: %r" % (bad.splitlines()[-1][:60], kept.strip())


def c10(ctx):
    """exec timeout_s -> job id; poll incremental; a queued submit says so."""
    server = ctx.shared()
    text, is_error, _ = server.tool(
        "exec",
        code="import time\ntime.sleep(2)\nc10 = 'done'\nc10",
        session="C10",
        timeout_s=0,
    )
    job_id = _job_id(text)
    queued = server.tool("exec", code="1 + 1", session="C10b", timeout_s=0)[0]
    first = server.tool("poll", job_id=job_id)[0]
    rest = wait_finished(server, job_id, since=len(first))
    ok = (
        not is_error
        and "job_id" in text
        and "[queued: another job is running]" in queued
        and "[still running" in first
        and "'done'" in first + rest
        and "[job finished in" in rest
    )
    return ok, "queued while busy; incremental poll: %r + %r" % (
        first.strip()[-40:],
        rest.strip()[:60],
    )


def c11(ctx):
    """poll of an unknown job id is a tool error naming the id."""
    text, is_error, _ = ctx.shared().tool("poll", job_id="jnope")
    return is_error and "jnope" in text and "unknown job" in text, text.splitlines()[0][:120]


def c12(ctx):
    """interrupt finishes the job as KeyboardInterrupt; variables stay."""
    server = ctx.shared()
    server.tool("exec", code="c12 = 'before'", session="C12")
    text, _, _ = server.tool(
        "exec", code="import time\ntime.sleep(5)", session="C12", timeout_s=0
    )
    job_id = _job_id(text)
    time.sleep(0.8)
    said, is_error, _ = server.tool("interrupt")
    body = wait_finished(server, job_id, deadline=25)
    kept, kept_error, _ = server.tool("exec", code="c12", session="C12")
    ok = (
        not is_error
        and "KeyboardInterrupt" in body
        and not kept_error
        and kept.strip() == "'before'"
    )
    return ok, "interrupt=%r, job: %r, c12=%r" % (
        said.strip()[:40],
        "KeyboardInterrupt" in body,
        kept.strip(),
    )


def c13(ctx):
    """a cancelled tools/call leaves a job that status names and poll recovers."""
    job_id, body = _cancel_and_collect(ctx.shared(), "c13")
    ok = "c13-marker" in body and "[job finished in" in body
    return ok, "cancel mid-job -> %s in status, poll recovered %r" % (
        job_id,
        body.strip()[:60],
    )


def c14(ctx):
    """cancelled notification for a request that already finished: no-op."""
    server = ctx.shared()
    server.tool("exec", code="c14 = 1", session="C14")
    before = calls(server)
    server.notify_cancelled("C14-finished-call", "the reply already arrived")
    after = calls(server)
    count = len(server.tools())
    ok = before == after and count == len(TOOLS)
    return ok, "notification accepted, jobs %d -> %d, %d tools still listed" % (
        before,
        after,
        count,
    )


def c15(ctx):
    """two servers, one kernel; B may not interrupt A's job."""
    # Both state dirs are the same directory on purpose: the second server adopts
    # the first one's kernel.  If they ever end up with two kernels, this case
    # fails loudly -- a "pass" would be about nothing.
    shared_dir = ctx.dir("C15-state")
    server_a = ctx.server_dir("C15-a", shared_dir)
    server_b = ctx.server_dir("C15-b", shared_dir)
    server_a.tool("exec", code="c15 = 1")  # a kernel exists only once a job ran
    pid_a = doc(server_a)["kernel"]["pid"]
    pid_b = doc(server_b)["kernel"]["pid"]
    if not pid_a or pid_a != pid_b:
        raise AssertionError(
            "harness could not put two servers on one kernel (pids %r and %r)"
            % (pid_a, pid_b)
        )
    text, _, _ = server_a.tool(
        "exec", code="import time\ntime.sleep(3.5)", session="C15", timeout_s=0
    )
    job_id = _job_id(text)
    time.sleep(0.8)
    refusal, refused, _ = server_b.tool("interrupt")
    body = wait_finished(server_a, job_id, deadline=25)
    ok = (
        refused
        and "another MCP client" in refusal
        and "KeyboardInterrupt" not in body
        and "[job finished in" in body
    )
    return ok, "one kernel (pid %s): B refused (%r), A's job finished normally" % (
        pid_a,
        "another MCP client" in refusal,
    )


def c16(ctx):
    """interrupt(force=true) stops the kernel; the next exec says it restarted."""
    server = ctx.server("C16")
    server.tool("exec", code="c16 = 1")
    said, is_error, _ = server.tool("interrupt", force=True)
    gone, gone_error, _ = server.tool("exec", code="'c16' in globals()")
    ok = (
        not is_error
        and "kernel stopped" in said
        and "[kernel restarted:" in gone
        and gone.strip().splitlines()[-1] == "False"
    )
    return ok, "%r -> next exec %r" % (
        said.strip().splitlines()[0][:50],
        gone.strip().splitlines()[0][:60],
    )


def c17(ctx):
    """a stray print in a handler must not touch the wire (rule 15)."""
    server = ctx.server("C17", {"SAGE_MCP_STDOUT_NOISE": "1"})
    first, first_error, _ = server.tool("exec", code="7 * 6")
    second, second_error, _ = server.tool("exec", code="6 * 7")
    ok = (
        not (first_error or second_error)
        and first.strip() == "42"
        and second.strip() == "42"
        and len(server.tools()) == len(TOOLS)
    )
    return ok, "two calls with noise: %r / %r, session alive" % (
        first.strip(),
        second.strip(),
    )


def c18(ctx):
    """300 kB passes whole; above 4 MB the job is truncated with a notice."""
    server = ctx.shared()
    small = server.tool(
        "exec", code="import sys; sys.stdout.write('x' * 300000)", session="C18a",
        timeout_s=0,
    )[0]
    small_body = wait_finished(server, _job_id(small), deadline=25)
    big = server.tool(
        "exec", code="import sys; sys.stdout.write('y' * 4300000)", session="C18b",
        timeout_s=0,
    )[0]
    big_body = wait_finished(server, _job_id(big), deadline=30)
    after, after_error, _ = server.tool("exec", code="'c18 alive'")
    ok = (
        "truncated" not in small_body
        and len(small_body) >= 300000
        and "[output truncated at 4 MB" in big_body
        and len(big_body) < 4_300_000
        and not after_error
        and after.strip() == "'c18 alive'"
    )
    return ok, "300 kB intact, 4.3 MB capped at %d chars, session framing intact" % len(
        big_body
    )


def c19(ctx):
    """a lone surrogate in the output does not break the reply frame."""
    server = ctx.shared()
    text, is_error, _ = server.tool(
        "exec",
        code="import os, sys; sys.stdout.write(os.fsdecode(b'\\xff\\xfe'))",
        session="C19",
    )
    after, after_error, _ = server.tool("exec", code="1 + 1", session="C19")
    ok = (
        not (is_error or after_error)
        and ("?" in text or "\ufffd" in text)
        and after.strip() == "2"
    )
    return ok, "reply survived (%d chars), next call=%r" % (len(text), after.strip())


def c20(ctx):
    """resources/list and resources/read: application/json, §5 keys."""
    server = ctx.shared()
    listed = server.resources()
    mime, text = server.resource(URI)
    body = json.loads(text)
    ok = (
        any(entry["uri"] == URI and entry["mime_type"] == "application/json" for entry in listed)
        and mime == "application/json"
        and sorted(body) == sorted(
            ("server", "kernel", "sessions", "jobs", "last_error", "limits", "log")
        )
        and {"pid", "state", "restarts", "counters_since"} <= set(body["kernel"])
        and {"run", "failed", "interrupted", "queued", "last_job"} <= set(body["jobs"])
        and set(body["limits"]) == {"cpu", "as", "fsize", "nproc", "source"}
    )
    return ok, "%d resource(s), %d top-level keys, limits=%s" % (
        len(listed),
        len(body),
        json.dumps(body["limits"]),
    )


def c21(ctx):
    """reading the resource while a job runs: state busy, tail present, then done."""
    server = ctx.shared()
    text, _, _ = server.tool(
        "exec", code="import time\nprint('c21-running')\ntime.sleep(3.5)", session="C21",
        timeout_s=0,
    )
    job_id = _job_id(text)
    time.sleep(0.7)
    during = doc(server)
    body = wait_finished(server, job_id, deadline=25)
    ok = (
        during["kernel"]["state"] == "busy"
        and during["jobs"].get("last_job") == job_id
        and during["log"]["tail"]
        and "c21-running" in body
    )
    return ok, "during: state=%r last_job=%s tail=%d lines; job finished" % (
        during["kernel"]["state"],
        during["jobs"].get("last_job"),
        len(during["log"]["tail"]),
    )


def c22(ctx):
    """GAP crash-restart markers trip the guard once; the session survives.

    The markers must come from `sage/interfaces/gap.py` -- that is the guard's
    evidence (the writer), not the text and not "GAP is imported".
    """
    server = ctx.shared()
    _, gap = A.gap_stand_in(ctx.dir("C22"), "gapfake")
    server.tool("exec", code="c22 = 41", session="C22")
    trips = doc(server)["jobs"]["gap_trips"]
    echoed, echoed_error, _ = server.tool(
        "exec",
        code="import sys\nfor _ in range(4):\n    print('Gap crashed', file=sys.stderr)",
        session="C22",
    )
    text, is_error, _ = server.tool(
        "exec", code=gap + "for _ in range(2):\n    gap_fake.eval('x')", session="C22"
    )
    kept, kept_error, _ = server.tool("exec", code="c22", session="C22")
    ok = (
        not (is_error or echoed_error)
        and "[gap-guard]" not in echoed
        and text.startswith("[gap-guard] interrupted after 3 'Gap crashed' markers")
        and "KeyboardInterrupt" in text
        and doc(server)["jobs"]["gap_trips"] == trips + 1
        and not kept_error
        and kept.strip() == "41"
    )
    return ok, "trips %d -> %d, echoed markers ignored, session keeps c22=%r" % (
        trips,
        doc(server)["jobs"]["gap_trips"],
        kept.strip(),
    )


def c23(ctx):
    """SAGE_MCP_LIMITS: as, fsize, cpu, nproc and core fail the job, not the kernel."""
    notes = []

    server = ctx.server("C23-as", {"SAGE_MCP_LIMITS": "as=8G"})
    text, _, _ = server.tool("exec", code="big = bytearray(10 * 1024**3)")
    alive, alive_error, _ = server.tool("exec", code="1 + 1")
    if not ("MemoryError" in text and not alive_error and alive.strip() == "2"):
        return False, "as=8G: %r then %r" % (text.splitlines()[-1][:60], alive.strip())
    notes.append("as=8G -> MemoryError, kernel alive")

    server = ctx.server("C23-fsize", {"SAGE_MCP_LIMITS": "fsize=1M"})
    target = os.path.join(ctx.dir("C23-fsize"), "big.bin")
    text, _, _ = server.tool(
        "exec",
        code="fh = open(%r, 'wb')\nfor _ in range(4):\n    fh.write(b'x' * 512 * 1024)\nfh.close()"
        % target,
    )
    alive, alive_error, _ = server.tool("exec", code="2 + 2")
    if not ("RLIMIT_FSIZE" in text and not alive_error and alive.strip() == "4"):
        return False, "fsize=1M: %r" % text.splitlines()[-1][:80]
    notes.append("fsize=1M -> SIGXFSZ job error, kernel alive")

    server = ctx.server("C23-cpu", {"SAGE_MCP_LIMITS": "cpu=2"})
    started = time.time()
    text, _, _ = server.tool(
        "exec",
        code="import time\nstart = time.time()\nwhile time.time() - start < 30:\n    pass",
        timeout_s=60,
    )
    elapsed = time.time() - started
    status = server.tool("status")[0]
    if not (
        "RLIMIT_CPU" in text
        and 1.5 <= elapsed <= 12
        and status.splitlines()[0].startswith("kernel: running")
    ):
        return False, "cpu=2: %.1fs, %r, %r" % (
            elapsed,
            text.splitlines()[-1][:60],
            status.splitlines()[0][:60],
        )
    notes.append("cpu=2 -> tripped in %.1fs, kernel running" % elapsed)

    server = ctx.server("C23-nproc", {"SAGE_MCP_LIMITS": "nproc=8"})
    server.tool("exec", code="np_probe = 0")
    text = server.tool("exec", code=_NPROC_CODE, timeout_s=0)[0]
    fork_body = wait_finished(server, _job_id(text), deadline=30)
    time.sleep(1.5)
    alive, alive_error, _ = server.tool("exec", code="np_probe + 5")
    if not (
        "margin=8" in fork_body
        and "EAGAIN at attempt" in fork_body
        and not alive_error
        and alive.strip() == "5"
    ):
        return False, "nproc=8: %r" % fork_body.replace("\n", " | ")[:120]
    notes.append("nproc=8 -> EAGAIN, kernel alive")

    server = ctx.server("C23-core", {"SAGE_MCP_LIMITS": "as=8G,fsize=256M"})
    work = os.path.join(ctx.dir("C23-core"), "work")
    os.makedirs(work, exist_ok=True)
    script = os.path.join(work, "abort_child.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write("import os\nos.abort()\n")
    text, is_error, _ = server.tool("run_file", path=script)
    cores = [name for name in os.listdir(work) if name.startswith("core")]
    if not (is_error and "exit=-6" in text and not cores):
        return False, "core: %r cores=%s" % (text.splitlines()[0][:60], cores)
    notes.append("RLIMIT_CORE=0 -> os.abort() leaves no core")
    return True, "; ".join(notes)


def c24(ctx):
    """malformed input and an unknown method, then a 1 MB argument."""
    request = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "initialize",
            "params": {
                "protocolVersion": LATEST_HANDSHAKE_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "conformance-raw", "version": "1.0"},
            },
        }
    )
    replies = _raw_replies(
        ctx.dir("C24"),
        ["{this is not json}", '{"jsonrpc": "2.0", "id": 5, "method": "nope"}', request],
    )
    parsed = []
    for line in replies:
        try:
            parsed.append(json.loads(line))
        except ValueError:
            pass
    unknown = [item for item in parsed if item.get("id") == 5]
    init = [item for item in parsed if item.get("id") == 7]
    bad_line_answered = any(item.get("id") is None for item in parsed)
    # mcp 2.3.0 drops a line it cannot parse (no -32700 reply) and keeps serving:
    # what must hold is "no crash and the next request is answered".
    session_ok = len(init) == 1 and init[0].get("result", {}).get("serverInfo", {}).get("name") == "sage"
    method_ok = bool(unknown) and unknown[0].get("error", {}).get("code") == -32601
    server = ctx.shared()
    big, is_error, _ = server.tool(
        "exec", code="# " + "z" * 1_000_000 + "\nc24 = 1\nc24"
    )
    ok = (
        session_ok
        and method_ok
        and not bad_line_answered  # the SDK drops it silently; nothing is faked
        and not is_error
        and big.strip() == "1"
    )
    return ok, "malformed line dropped, next initialize answered, unknown method -32601, 1 MB arg ok"


_NPROC_CODE = """
import os, resource, time
import limits as kernel_limits


def settle():
    # Sage's import leaves a thread pool alive for a moment; one fork makes it
    # exit, so the per-UID count (and therefore the margin) is stable.
    for _ in range(3):
        before = kernel_limits.uid_tasks()
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
        if kernel_limits.uid_tasks() == before:
            return


settle()
kernel_limits.arm_nproc({"nproc": 8})  # the helper the kernel itself uses
soft, _hard = resource.getrlimit(resource.RLIMIT_NPROC)
used = kernel_limits.uid_tasks()
print("armed soft=%d tasks=%d margin=%d" % (soft, used, soft - used))
kids, note = [], ""
for i in range(20):
    try:
        child = os.fork()
    except OSError as exc:
        note = "EAGAIN at attempt %d: %s" % (i + 1, exc.strerror or exc)
        break
    if child == 0:
        time.sleep(0.5)
        os._exit(0)
    kids.append(child)
else:
    note = "no EAGAIN after 20 attempts"
for child in kids:
    try:
        os.waitpid(child, 0)
    except OSError:
        pass
print("kids=%d %s" % (len(kids), note))
"""


CASES = [
    ("C01", "lifecycle", "initialize with protocol 2024-11-05", c01),
    ("C02", "lifecycle", "initialize with the newest handshake version", c02),
    ("C03", "lifecycle", "ping after the handshake", c03),
    ("C04", "lifecycle", "initialized, then tools/list", c04),
    ("C05", "schema", "inputSchema equals the snapshot", c05),
    ("C06", "arguments", "unknown argument to exec", c06),
    ("C07", "arguments", "wrong argument type", c07),
    ("C08", "exec", "exec 2+2", c08),
    ("C09", "exec", "syntax error, session intact", c09),
    ("C10", "exec", "timeout_s -> job id, incremental poll", c10),
    ("C11", "poll", "poll of an unknown job id", c11),
    ("C12", "interrupt", "interrupt -> KeyboardInterrupt, variables kept", c12),
    ("C13", "cancel", "cancelled tools/call is recoverable", c13),
    ("C14", "cancel", "cancelled notification for a finished call", c14),
    ("C15", "ownership", "two servers, one kernel; cross interrupt refused", c15),
    ("C16", "restart", "interrupt(force=true) and the restart notice", c16),
    ("C17", "stdout", "a stray print stays out of the wire", c17),
    ("C18", "output", "300 kB whole, 4 MB capped", c18),
    ("C19", "output", "lone surrogate does not break the frame", c19),
    ("C20", "resources", "resources/list and resources/read", c20),
    ("C21", "resources", "read while a job runs", c21),
    ("C22", "gap", "GAP crash-restart guard", c22),
    ("C23", "limits", "SAGE_MCP_LIMITS trips the job, not the kernel", c23),
    ("C24", "transport", "malformed input, unknown method, 1 MB argument", c24),
]


def main(argv=None):
    ap = argparse.ArgumentParser(description="the PLAN §6 conformance matrix")
    ap.add_argument("--only", default="", help="comma-separated case ids (e.g. C13,C15)")
    ap.add_argument("--list", action="store_true", help="print the case table")
    ap.add_argument("--quiet", action="store_true", help="only failures and the summary")
    ap.add_argument("--keep", action="store_true", help="keep the temporary state dir")
    args = ap.parse_args(argv)

    if args.list:
        for case_id, area, title, _ in CASES:
            print("%s  %-10s %s" % (case_id, area, title))
        return 0

    wanted = [part.strip().upper() for part in args.only.split(",") if part.strip()]
    unknown = [case_id for case_id in wanted if case_id not in [c[0] for c in CASES]]
    if unknown:
        print("unknown case id(s): %s" % ", ".join(unknown), file=sys.stderr)
        return 2
    selected = [case for case in CASES if not wanted or case[0] in wanted]

    workdir = tempfile.mkdtemp(prefix="sage-mcp-conformance-")
    ctx = Ctx(workdir)
    failures = 0
    print(
        "conformance: %d case(s) via the official client, state=%s"
        % (len(selected), workdir)
    )
    try:
        for case_id, area, title, case in selected:
            started = time.time()
            try:
                ok, evidence = case(ctx)
            except BaseException as exc:  # a case must never abort the run
                ok, evidence = False, "%s: %s" % (type(exc).__name__, exc)
            elapsed = time.time() - started
            if ok:
                if not args.quiet:
                    print("  %s  %-10s PASS %5.1fs  %s" % (case_id, area, elapsed, evidence))
            else:
                failures += 1
                print("  %s  %-10s FAIL %5.1fs  %s" % (case_id, area, elapsed, evidence))
    finally:
        ctx.close()
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            print("state kept in %s" % workdir)

    total = len(selected)
    print(
        "%d/%d cases passed%s"
        % (total - failures, total, "" if not failures else "; %d FAILED" % failures)
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
