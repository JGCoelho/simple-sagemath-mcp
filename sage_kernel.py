#!/usr/bin/env python3
"""sage_kernel.py -- long-lived Sage kernel process for sage-mcp.

Design rules that must not be broken (each was paid for by a bug):

* User code runs on the **main thread**, one job at a time.  Sage's cysignals
  layer calls ``sig_on()``, which is main-thread-only; running ``factor()`` on a
  worker thread raises ``SignalError: Segmentation fault``.  Connection threads
  only enqueue jobs and read state.
* ``timeout`` is not a computation limit: the kernel never kills a job, it
  only hands back a snapshot while the job keeps running.
* ``sys.stdout`` / ``sys.stderr`` are replaced by routers that append to the
  *currently running job's* buffer and fall back to the real stream when idle.
  The kernel's own log goes straight to the original stderr (-> kernel.log) and
  never through the router.
* ``SIGINT`` cannot kill the kernel: an interrupt outside a job is ignored; an
  interrupt inside a job becomes that job's ``KeyboardInterrupt``.
* No job may kill the kernel: ``except BaseException`` around execution,
  traceback into ``job.error``, ``job.done.set()`` in a ``finally``.
"""

import argparse
import ast
import itertools
import json
import os
import queue
import signal
import socket
import stat
import sys
import threading
import time
import traceback
from multiprocessing.connection import AuthenticationError, Listener

# The authkey helpers are shared with the client: one implementation for both
# sides of the wire (a hand-started kernel creates the key too).
from sage_client import load_or_create_key

OUTPUT_CAP = 4 * 1024 * 1024  # chars per job, ~4 MB; hard cap, with notice
REPR_CAP = 4000  # chars of repr(); unbounded reprs have killed agent contexts
MAX_REQUEST = 8 * 1024 * 1024  # refuse absurd request messages

STARTED = time.time()

# --------------------------------------------------------------------------
# global state
# --------------------------------------------------------------------------
_ORIG_OUT = sys.stdout
_ORIG_ERR = sys.stderr
_LOG = _ORIG_ERR  # rule 4: kernel log = original stderr (-> kernel.log)
_LOG_LOCK = threading.Lock()

_JOBS = {}  # job_id -> Job
_JOBS_LOCK = threading.RLock()
_QUEUE = queue.Queue()
_JobIds = itertools.count(1)

_SESSIONS = {}  # name -> namespace dict
_SESSIONS_LOCK = threading.RLock()
_BASE = {}  # shared base namespace built from `from sage.all import *`

_CURRENT = None  # Job currently executing (only the main thread sets this)
_CURRENT_LOCK = threading.Lock()
_LAST_ERROR = None  # (job_id, session, first line)
_SHUTDOWN = threading.Event()

STATE_FILE = None
RUNNING_FILE = "running.json"  # lives next to the state file
SOCK_NAME = "kernel.sock"
SOCK_PATH = None
DIR_MODE = 0o700
SUN_PATH_MAX = 100  # Linux sun_path is 108 bytes including the trailing NUL
LOG_PATH = None
SAGE_VERSION = "?"


def _log(msg):
    """Kernel diagnostics go to the original stderr, never through the router."""
    try:
        with _LOG_LOCK:
            _LOG.write(str(msg).rstrip("\n") + "\n")
            _LOG.flush()
    except Exception:
        pass


def _private_dir(path):
    """Create our state directory as 0700 and refuse one we do not own.

    The socket is only as private as the directory holding it: on a shared
    machine /tmp lets another user create /tmp/sage-mcp-<uid> first, and a
    kernel there would hand them a code-execution channel.  The client checks
    this too; the kernel checks it again because it can be started by hand.
    """
    try:
        os.makedirs(path, mode=DIR_MODE, exist_ok=True)
        st = os.lstat(path)
    except OSError as exc:
        raise SystemExit("sage-mcp kernel: unusable state directory %s: %s" % (path, exc))
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise SystemExit(
            "sage-mcp kernel: refusing state directory %s (not a directory we own)" % path
        )
    if stat.S_IMODE(st.st_mode) != DIR_MODE:
        try:
            os.chmod(path, DIR_MODE)
        except OSError as exc:
            raise SystemExit(
                "sage-mcp kernel: cannot make state directory %s private: %s" % (path, exc)
            )


def _check_sock_path(path):
    if len(os.fsencode(path)) > SUN_PATH_MAX:
        raise SystemExit(
            "sage-mcp kernel: socket path is %d bytes, over the %d-byte AF_UNIX "
            "limit: %s (set SAGE_MCP_STATE to a shorter directory)"
            % (len(os.fsencode(path)), SUN_PATH_MAX, path)
        )


def _remove_socket():
    if SOCK_PATH:
        try:
            os.unlink(SOCK_PATH)
        except OSError:
            pass


def _bind_server(path, authkey):
    """Bind the AF_UNIX listener; clear a name only when it is provably dead."""
    if os.path.exists(path):
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(1.0)
        try:
            probe.connect(path)
        except (ConnectionRefusedError, FileNotFoundError):
            try:
                os.unlink(path)
            except OSError:
                pass
        except OSError as exc:
            # A timeout here means SOMETHING accepted the connection (a busy
            # kernel's listen backlog). Never unlink what we cannot prove dead.
            raise SystemExit(
                "sage-mcp kernel: %s already exists and could not be proven dead "
                "(%s); refusing to start a second kernel on it" % (path, exc)
            )
        else:
            raise SystemExit(
                "sage-mcp kernel: another kernel is already listening on %s" % path
            )
        finally:
            probe.close()
    # `accept()` runs the challenge/response handshake, so a connection without
    # the authkey never reaches an op handler.
    listener = Listener(path, backlog=128, authkey=authkey)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return listener


# --------------------------------------------------------------------------
# per-job output routing (rule 3 + rule 8)
# --------------------------------------------------------------------------
class Job:
    __slots__ = (
        "id", "session", "code", "preparse", "submitted", "started", "finished",
        "state", "parts", "nbytes", "truncated", "error", "repr", "done", "lock",
        "cancel", "blocked", "owner",
    )

    def __init__(self, jid, session, code, preparse, owner=None):
        self.id = jid
        self.session = session
        self.code = code
        self.preparse = preparse
        self.submitted = time.time()
        self.started = None
        self.finished = None
        self.state = "queued"
        self.parts = []
        self.nbytes = 0
        self.truncated = False
        self.error = None
        self.repr = None
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.cancel = False
        self.blocked = False  # was something already running/queued at submit?
        self.owner = owner  # client id of the MCP server that submitted it

    def append(self, text):
        if not text:
            return
        with self.lock:
            if self.truncated:
                return
            if self.nbytes + len(text) > OUTPUT_CAP:
                room = max(0, OUTPUT_CAP - self.nbytes)
                if room:
                    self.parts.append(text[:room])
                    self.nbytes += room
                self.parts.append(
                    "\n[output truncated at 4 MB — further output dropped]\n"
                )
                self.truncated = True
                return
            self.parts.append(text)
            self.nbytes += len(text)

    def snapshot(self, since=0):
        with self.lock:
            text = "".join(self.parts)
            truncated = self.truncated
        total = len(text)
        since = max(0, min(int(since or 0), total))
        return text[since:], total, truncated

    def info(self, since=0):
        text, offset, truncated = self.snapshot(since)
        now = time.time()
        ref = self.started or self.submitted
        return {
            "id": self.id,
            "session": self.session,
            "state": self.state,
            "blocked": self.blocked,
            "stdout": text,
            "offset": offset,
            "truncated": truncated,
            "error": self.error,
            "repr": self.repr,
            "elapsed_s": round(now - ref, 3),
            "duration_s": (
                round(self.finished - self.started, 3)
                if self.finished and self.started
                else None
            ),
        }


class _Router:
    """A stdout/stderr stand-in that writes into the running job's buffer."""

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, name, fallback):
        self.name = name
        self._fallback = fallback

    def write(self, data):
        if not isinstance(data, str):
            data = str(data)
        if not data:
            return 0
        job = _CURRENT
        if job is None:
            try:
                self._fallback.write(data)
                self._fallback.flush()
            except Exception:
                pass
        else:
            job.append(data)
        return len(data)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        if _CURRENT is None:
            try:
                self._fallback.flush()
            except Exception:
                pass

    def isatty(self):
        return False

    def fileno(self):  # nothing may dup a job's stream
        raise OSError("job output is not a file descriptor")

    def close(self):
        pass

    @property
    def closed(self):
        return False


# --------------------------------------------------------------------------
# time.sleep patch (rule 9)
# --------------------------------------------------------------------------
def _patch_sleep():
    """The Sage preparser rewrites ``time.sleep(0.4)`` as
    ``time.sleep(RealNumber('0.4'))``; ``__index__`` rounds that to 0, so the
    sleep would silently return instantly.  Accept anything float-able."""
    import time as _time

    original = _time.sleep

    def sleep(seconds=0):
        try:
            return original(float(seconds))
        except (TypeError, ValueError):
            return original(seconds)

    _time.sleep = sleep


# --------------------------------------------------------------------------
# sessions (rule 10)
# --------------------------------------------------------------------------
def _ns_size(ns):
    return sum(1 for k in ns if not k.startswith("__"))


def get_namespace(name):
    with _SESSIONS_LOCK:
        ns = _SESSIONS.get(name)
        if ns is None:
            ns = dict(_BASE)
            ns.setdefault("__name__", "__main__")
            _SESSIONS[name] = ns
        return ns


def active_job_for_session(name):
    with _JOBS_LOCK:
        for job in _JOBS.values():
            if job.session == name and job.state in ("queued", "running"):
                return job.id
    return None


# --------------------------------------------------------------------------
# execution (rules 1, 2, 5, 6, 7, 8)
# --------------------------------------------------------------------------
def _prepare_source(job):
    src = job.code
    if job.preparse:
        from sage.repl.preparse import preparse

        src = preparse(src, True)
    if src.startswith("\ufeff"):  # preparse keeps a BOM; ast.parse cannot
        src = src[1:]
    return src.replace("\r\n", "\n").replace("\r", "\n")


def _syntax_report(exc, src):
    """A SyntaxError must carry the *preparsed* source: that is the only way to
    debug a bad rewrite."""
    lines = ["SyntaxError: %s" % (exc.msg,)]
    if exc.lineno:
        where = "line %d" % exc.lineno
        if exc.offset:
            where += ", column %d" % exc.offset
        lines[0] += " (%s)" % where
    if exc.text:
        lines.append("    " + exc.text.rstrip("\n"))
        if exc.offset:
            lines.append("    " + " " * max(0, exc.offset - 1) + "^")
    if len(src) > REPR_CAP:
        body = src[:REPR_CAP] + "\n... [preparsed source truncated, %d chars]" % len(src)
    else:
        body = src
    lines.append("--- preparsed source ---")
    lines.append(body)
    lines.append("--- end preparsed source ---")
    return "\n".join(lines)


def _format_error(exc):
    if isinstance(exc, KeyboardInterrupt):
        return (
            "KeyboardInterrupt: interrupted by SIGINT "
            "(the session and its variables are preserved)"
        )
    if isinstance(exc, SystemExit):
        return "SystemExit: %s (the kernel keeps running)" % (exc.code,)
    tb = exc.__traceback__
    frames = traceback.format_tb(tb)
    kept = [f for f in frames if os.path.basename(__file__) not in f]
    head = ""
    if kept:
        head = "Traceback (most recent call last):\n" + "".join(kept)
    tail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    text = (head + tail).strip()
    return text or "%s: %s" % (type(exc).__name__, exc)


def _write_running(job):
    """Publish the running job (id + owner) to a file next to the state file.

    A client must be able to answer "is the running job mine?" *before* sending
    SIGINT, and it cannot ask us: the job may be holding the GIL at the time.
    """
    if not STATE_FILE:
        return
    path = os.path.join(os.path.dirname(STATE_FILE), RUNNING_FILE)
    payload = {
        "job": job.id,
        "owner": job.owner,
        "session": job.session,
        "started": job.started,
        "pid": os.getpid(),
    }
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def _clear_running():
    if not STATE_FILE:
        return
    try:
        os.unlink(os.path.join(os.path.dirname(STATE_FILE), RUNNING_FILE))
    except OSError:
        pass


def _run_job(job):
    global _CURRENT, _LAST_ERROR
    job.state = "running"
    job.started = time.time()
    with _CURRENT_LOCK:
        _CURRENT = job
    _write_running(job)
    try:
        ns = get_namespace(job.session)
        src = _prepare_source(job)
        try:
            tree = ast.parse(src, mode="exec")
        except SyntaxError as exc:
            job.error = _syntax_report(exc, src)
            return
        body = list(tree.body)
        tail = None
        if body and isinstance(body[-1], ast.Expr):
            tail = body.pop()
        if body:
            exec(compile(ast.Module(body=body, type_ignores=[]), "<sage-mcp>", "exec"), ns)
        if tail is not None:
            value = eval(compile(ast.Expression(body=tail.value), "<sage-mcp>", "eval"), ns)
            if value is not None:
                text = repr(value)
                if len(text) > REPR_CAP:
                    text = text[:REPR_CAP] + (
                        "... [repr truncated: %d chars total]" % len(text)
                    )
                job.repr = text
    except BaseException as exc:  # rule 6: nothing escapes
        job.error = _format_error(exc)
    finally:
        _clear_running()
        job.finished = time.time()
        job.state = "done"
        with _CURRENT_LOCK:
            _CURRENT = None  # route later output to the real stream again
        if job.error:
            _LAST_ERROR = (job.id, job.session, _error_line(job.error))
        job.done.set()


def _error_line(text, limit=300):
    """The line a reader wants in `status`: the exception, not its header.

    `poll` still returns the whole traceback; `status` is only an index.
    """
    for line in reversed((text or "").strip().splitlines()):
        stripped = line.strip()
        if stripped and (set(stripped) - set("^~ ")):  # skip blank and caret rows
            return stripped[:limit] + (" ..." if len(stripped) > limit else "")
    return "?"


def _executor_main():
    """Main thread: take one job at a time off the queue and run it."""
    while not _SHUTDOWN.is_set():
        try:
            jid = _QUEUE.get(timeout=0.5)
        except queue.Empty:
            continue
        except KeyboardInterrupt:  # rule 5: idle interrupt is not fatal
            _log("SIGINT outside a job: ignored")
            continue
        job = _JOBS.get(jid)
        if job is None or job.cancel:
            continue
        try:
            _run_job(job)
        except KeyboardInterrupt:
            # The interrupt landed in the sliver between _run_job's handler and
            # its finally; that finally has already released the job.
            _log("SIGINT outside a job: ignored")
        except BaseException:
            _log("internal executor error:\n" + traceback.format_exc())
            try:
                job.state = "done"
                job.done.set()
            except Exception:
                pass
        finally:
            _prune_jobs()


def _prune_jobs(keep=200):
    """Forget old finished jobs; nothing else is ever evicted."""
    with _JOBS_LOCK:
        finished = [j for j in _JOBS.values() if j.state == "done"]
        if len(finished) <= keep:
            return
        finished.sort(key=lambda j: j.submitted)
        for job in finished[: len(finished) - keep]:
            _JOBS.pop(job.id, None)


# --------------------------------------------------------------------------
# operations (called from connection threads)
# --------------------------------------------------------------------------
def op_submit(req):
    session = req.get("session") or "default"
    code = req.get("code")
    if not isinstance(code, str):
        return {"ok": False, "error": "code must be a string"}
    preparse = bool(req.get("preparse", True))
    job = Job("j%d" % next(_JobIds), str(session), code, preparse, req.get("client"))
    with _CURRENT_LOCK:
        current = _CURRENT
    with _JOBS_LOCK:
        job.blocked = current is not None or any(
            other.state in ("queued", "running") for other in _JOBS.values()
        )
        _JOBS[job.id] = job
    _log("submit %s session=%s preparse=%s %dB" % (job.id, session, preparse, len(code)))
    wait = float(req.get("wait") or 0)
    response = {"ok": True, "job_id": job.id, "job": job.info(int(req.get("since") or 0))}
    if wait > 0:
        _QUEUE.put(job.id)
        job.done.wait(wait)
        return {"ok": True, "job_id": job.id, "job": job.info(int(req.get("since") or 0))}
    # Enqueue only *after* the reply is on the wire: a job that grabs the GIL
    # immediately (a tight loop) would otherwise starve the reply and the client
    # would lose the job id.
    return response, lambda: _QUEUE.put(job.id)


def _lookup(req):
    jid = req.get("job_id")
    with _JOBS_LOCK:
        job = _JOBS.get(jid)
    return job


def op_poll(req):
    job = _lookup(req)
    if job is None:
        return {"ok": False, "error": "unknown job %r (the kernel may have restarted)" % (req.get("job_id"),)}
    return {"ok": True, "job_id": job.id, "job": job.info(int(req.get("since") or 0))}


def op_wait(req):
    job = _lookup(req)
    if job is None:
        return {"ok": False, "error": "unknown job %r (the kernel may have restarted)" % (req.get("job_id"),)}
    timeout = float(req.get("timeout") or 0)
    if timeout > 0:
        job.done.wait(timeout)
    return {
        "ok": True,
        "job_id": job.id,
        "job": job.info(int(req.get("since") or 0)),
        "finished": job.done.is_set(),
    }


def op_reset(req):
    name = str(req.get("session") or "default")
    ns = get_namespace(name)  # never evicts the entry, just empties it
    ns.clear()
    ns.update({k: v for k, v in _BASE.items()})
    ns.setdefault("__name__", "__main__")
    return {"ok": True, "session": name, "names": _ns_size(ns)}


def op_drop(req):
    names = req.get("sessions") or []
    everything = bool(req.get("all"))
    dropped, kept, missing = [], {}, []
    with _SESSIONS_LOCK:
        targets = list(_SESSIONS) if everything else [str(n) for n in names]
        for name in targets:
            if name not in _SESSIONS:
                missing.append(name)
                continue
            jid = active_job_for_session(name)
            if jid is not None:  # rule 10: refuse a session with work in flight
                kept[name] = jid
                continue
            _SESSIONS.pop(name, None)
            dropped.append(name)
        remaining = sorted(_SESSIONS)
    return {
        "ok": True,
        "dropped": dropped,
        "kept": kept,
        "missing": missing,
        "remaining": remaining,
    }


def op_status(req):
    with _SESSIONS_LOCK:
        sessions = {name: _ns_size(ns) for name, ns in _SESSIONS.items()}
    with _JOBS_LOCK:
        queued = [j.id for j in _JOBS.values() if j.state == "queued"]
        running = [j.id for j in _JOBS.values() if j.state == "running"]
        last = None
        if _LAST_ERROR:
            last = {"job": _LAST_ERROR[0], "session": _LAST_ERROR[1], "error": _LAST_ERROR[2]}
    return {
        "ok": True,
        "pid": os.getpid(),
        "sock": SOCK_PATH,
        "started": STARTED,
        "uptime_s": round(time.time() - STARTED, 1),
        "sessions": sessions,
        "jobs": {"queued": queued, "running": running},
        "clients": {"live": len(_live_clients()), "ttl_s": CLIENT_TTL},
        "last_error": last,
        "log": LOG_PATH,
        "sage_version": SAGE_VERSION,
        "python": sys.version.split()[0],
    }


def op_interrupt(req):
    pid = os.getpid()
    cid = req.get("client")
    with _JOBS_LOCK:
        running = None
        for other in _JOBS.values():
            if other.state == "running":
                running = other
    if running is None:
        _log("interrupt requested while idle; no signal sent")
        return {"ok": True, "pid": pid, "running": None, "idle": True}
    if running.owner is not None and running.owner != cid:
        _log("interrupt refused: %s belongs to another client" % running.id)
        return {
            "ok": False,
            "error": "refused: job %s (session %r) belongs to another MCP client"
            % (running.id, running.session),
        }
    _log("interrupt requested; sending SIGINT to self (pid %d)" % pid)
    os.kill(pid, signal.SIGINT)  # delivered to the main thread, inside the job
    return {"ok": True, "pid": pid, "running": running.id}


def op_shutdown(req):
    _log("shutdown requested")
    threading.Thread(target=_shutdown_soon, daemon=True).start()
    return {"ok": True, "pid": os.getpid()}


def _shutdown_soon():
    _SHUTDOWN.set()
    time.sleep(0.2)
    _remove_socket()
    os._exit(0)


def op_clients(req):
    """Who else is using this kernel? Used by a server that is about to exit."""
    live = _live_clients()
    cid = req.get("client")
    others = [c for c in live if c != cid]
    return {"ok": True, "clients": len(live), "others": len(others), "ttl_s": CLIENT_TTL}


def op_bye(req):
    """Deregister a server that is exiting, so it stops counting as a client."""
    cid = req.get("client")
    if isinstance(cid, str) and cid:
        with _CLIENTS_LOCK:
            _CLIENTS.pop(cid, None)
    return {"ok": True, "deregistered": bool(cid)}


_DISPATCH = {
    "hello": lambda req: {
        "ok": True,
        "pid": os.getpid(),
        "sock": SOCK_PATH,
        "started": STARTED,
        "log": LOG_PATH,
        "sage_version": SAGE_VERSION,
        "python": sys.version.split()[0],
    },
    "ping": lambda req: {"ok": True},
    "submit": op_submit,
    "poll": op_poll,
    "wait": op_wait,
    "reset": op_reset,
    "drop": op_drop,
    "status": op_status,
    "interrupt": op_interrupt,
    "shutdown": op_shutdown,
    "clients": op_clients,
    "bye": op_bye,
}


CLIENT_TTL = 60.0  # seconds a client id stays "live" after its last request
_CLIENTS = {}  # client id -> time of its last request
_CLIENTS_LOCK = threading.Lock()


def _live_clients():
    """Recently active MCP servers (prunes ids that have gone quiet)."""
    now = time.monotonic()
    with _CLIENTS_LOCK:
        for gone in [c for c, seen in _CLIENTS.items() if now - seen > CLIENT_TTL]:
            _CLIENTS.pop(gone, None)
        return dict(_CLIENTS)


def _touch_client(req):
    cid = req.get("client")
    if isinstance(cid, str) and cid:
        with _CLIENTS_LOCK:
            _CLIENTS[cid] = time.monotonic()


class _Handler:
    """One connection: authenticate (in accept), read one request, write one reply.

    No read timeout: a `wait` op may legitimately block for a long time, and the
    thread ends as soon as the peer closes or the reply is written.
    """

    def __init__(self, conn):
        self.conn = conn

    def _send(self, payload):
        try:
            self.conn.send_bytes(
                json.dumps(payload, ensure_ascii=False).encode("utf-8")
            )
        except Exception:
            pass

    def handle(self):
        try:
            raw = self.conn.recv_bytes(MAX_REQUEST)
        except Exception as exc:
            _log("read error: %r" % (exc,))
            return
        if raw is None:
            self._send({"ok": False, "error": "request larger than %d bytes" % MAX_REQUEST})
            return
        try:
            req = json.loads(raw.decode("utf-8", "replace"))
            if not isinstance(req, dict):
                raise ValueError("request must be a JSON object")
        except Exception as exc:
            self._send({"ok": False, "error": "bad request: %s" % (exc,)})
            return
        _touch_client(req)
        op = req.get("op")
        fn = _DISPATCH.get(op)
        post = None
        try:
            result = fn(req) if fn else {"ok": False, "error": "unknown op %r" % (op,)}
            if isinstance(result, tuple):  # (reply, post_reply_callback)
                result, post = result
            resp = result
        except Exception as exc:
            _log("op %r failed:\n%s" % (op, traceback.format_exc()))
            resp = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        self._send(resp)
        if post is not None:
            try:
                post()
            except Exception:
                _log("post-reply hook failed:\n" + traceback.format_exc())


def _serve_conn(conn):
    try:
        _Handler(conn).handle()
    except Exception:
        _log("connection error:\n" + traceback.format_exc())
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _accept_loop(listener):
    """One daemon thread per connection; ops block only in their own thread."""
    while True:
        try:
            conn = listener.accept()
        except AuthenticationError as exc:
            _log("refused a connection: %s" % (exc,))
            continue
        except OSError:
            return  # the listener was closed: shutting down
        threading.Thread(
            target=_serve_conn, args=(conn,), name="conn", daemon=True
        ).start()


# --------------------------------------------------------------------------
# startup
# --------------------------------------------------------------------------
def _write_state(path):
    tmp = path + ".tmp.%d" % os.getpid()
    payload = {
        "pid": os.getpid(),
        "sock": SOCK_PATH,
        "started": STARTED,
        "log": LOG_PATH,
        "version": 1,
    }
    with open(tmp, "w") as fh:
        json.dump(payload, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def main():
    global _LOG, STATE_FILE, LOG_PATH, SAGE_VERSION, SOCK_PATH
    ap = argparse.ArgumentParser(description="sage-mcp kernel")
    ap.add_argument("--state-file", required=True, help="where to publish {pid,sock}")
    ap.add_argument("--log-file", default=None, help="redirect kernel stdout/stderr here")
    args = ap.parse_args()

    STATE_FILE = os.path.abspath(args.state_file)
    SOCK_PATH = os.path.join(os.path.dirname(STATE_FILE), SOCK_NAME)
    _private_dir(os.path.dirname(STATE_FILE))
    _check_sock_path(SOCK_PATH)
    if args.log_file:
        LOG_PATH = os.path.abspath(args.log_file)
        fd = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        os.close(fd)
        sys.stdout = os.fdopen(1, "w", buffering=1)
        sys.stderr = os.fdopen(2, "w", buffering=1)
    else:
        LOG_PATH = None

    _log("=== sage-mcp kernel booting (pid %d) %s ===" % (os.getpid(), time.strftime("%Y-%m-%d %H:%M:%S")))

    # Log first, then take over the streams: from here on, kernel diagnostics go
    # through _log() (original stderr) and user code output goes through routers.
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout = _Router("stdout", real_out)
    sys.stderr = _Router("stderr", real_err)

    _patch_sleep()

    # SIGINT keeps its default meaning (KeyboardInterrupt on the main thread);
    # SIGTERM exits promptly even mid-job, which is what `kill` relies on.
    def _on_term(*_args):
        _remove_socket()
        os._exit(0)

    signal.signal(signal.SIGTERM, _on_term)

    try:
        exec("from sage.all import *", _BASE)
        import sage.version as _sv

        SAGE_VERSION = _sv.version
    except BaseException:
        _log("fatal: could not import sage.all:\n" + traceback.format_exc())
        raise

    # sage.all does not export `time`, but a Sage REPL has it, the preparser
    # rewrites time.sleep(0.4) for it, and the sleep patch lives on it.
    _BASE.setdefault("time", time)

    authkey = load_or_create_key(os.path.dirname(STATE_FILE))
    listener = _bind_server(SOCK_PATH, authkey)
    _write_state(STATE_FILE)
    _log(
        "listening on unix:%s (authkey), state=%s, sage=%s"
        % (SOCK_PATH, STATE_FILE, SAGE_VERSION)
    )

    thread = threading.Thread(target=_accept_loop, args=(listener,), name="conn", daemon=True)
    thread.start()

    try:
        _executor_main()
    finally:
        try:
            listener.close()  # unlinks the AF_UNIX path
        except Exception:
            pass
        _remove_socket()
        _log("kernel exiting")


if __name__ == "__main__":
    main()
