#!/usr/bin/env python3
"""sage_kernel.py -- long-lived Sage kernel process for sage-mcp.

Rules that must not be broken (each was paid for by a bug):

* User code runs on the **main thread**, one job at a time: Sage's cysignals
  layer calls ``sig_on()``, which is main-thread-only (``factor()`` on a worker
  thread raises ``SignalError``).  Connection threads only enqueue and read.
* ``timeout`` is not a computation limit: the kernel never kills a job, it only
  hands back a snapshot while the job keeps running.
* ``sys.stdout``/``sys.stderr`` are routers that append to the *running job's*
  buffer and fall back to the real stream when idle.  Kernel diagnostics go
  straight to the original stderr (-> kernel.log), never through the router.
* ``SIGINT`` cannot kill the kernel: outside a job it is ignored, inside a job
  it becomes that job's ``KeyboardInterrupt``.
* No job may kill the kernel: ``except BaseException`` around execution,
  traceback into ``job.error``, ``job.done.set()`` in a ``finally``.
"""

import argparse
import ast
import itertools
import json
import os
import queue
import re
import signal
import socket
import sys
import threading
import time
import traceback
from multiprocessing.connection import AuthenticationError, Listener

import limits as limits_mod
from sage_client import check_sock_path, load_or_create_key, private_dir

OUTPUT_CAP = 4 * 1024 * 1024  # chars per job, ~4 MB; hard cap, with notice
REPR_CAP = 4000  # chars of repr(); unbounded reprs have killed agent contexts
MAX_REQUEST = 8 * 1024 * 1024  # refuse absurd request messages
STARTED = time.time()


def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class ResourceLimitError(BaseException):
    """A configured rlimit tripped: the *job* fails, the kernel survives.

    BaseException on purpose: an ordinary ``except Exception`` in user code must
    not turn "this job is too big" into a SIGKILL for the whole kernel (the hard
    limit is the backstop if it is caught anyway).
    """


LIMITS = {}  # SAGE_MCP_LIMITS as configured for this kernel (status reports it)
_LIMIT_TRIPPED = False  # an idle trip is logged once, not every second


def _on_limit(signum, frame):
    """Convert SIGXCPU/SIGXFSZ into a job error instead of a dead kernel.

    Outside a job there is nothing to raise into: note it once (SIGXCPU repeats
    every second while the process is over its soft limit) and keep serving.
    """
    global _LIMIT_TRIPPED
    if signum == signal.SIGXCPU:
        what = "RLIMIT_CPU (cpu=%s) exhausted: the process used its whole CPU budget" % (
            limits_mod.human("cpu", LIMITS),
        )
    else:
        what = "RLIMIT_FSIZE (fsize=%s): a write went over the file-size limit" % (
            limits_mod.human("fsize", LIMITS),
        )
    what += (
        "; the job stopped, the kernel and its sessions are intact -- for more "
        "room stop the kernel with interrupt(force=true) and restart it with a "
        "larger SAGE_MCP_LIMITS"
    )
    job = _CURRENT
    if job is None:
        if not _LIMIT_TRIPPED:
            _LIMIT_TRIPPED = True
            _log("resource limit outside a job: %s" % what, rotate=False)
        return
    job.note = "[limits] " + what
    # One raise per job: SIGXFSZ/SIGXCPU are delivered again while the job is
    # unwinding, and a second raise would replace the error already recorded.
    if job.limit_guarded:
        return
    job.limit_guarded = True
    raise ResourceLimitError(what)


# A GAP crash-restart loop (`structure_description()` on hostile input forks a
# process per crash) is not caught by any timeout, because the cell never ends.
# The guard counts the markers GAP writes and interrupts the job.  Sage prints
# them with `print()` from `sage/interfaces/gap.py` ("crashed or quit executing"
# / "Restarting Gap and trying again"), so the evidence is the *writer*:
# `_is_gap_interface(origin)` (a frame in that file) on either stream, or -- for
# a job that is really driving the interface -- any stderr once a gap.py frame
# has written something.  "GAP is loaded" is NOT evidence: `from sage.all import
# *` imports `sage.interfaces.gap` in every kernel, so the old `sys.modules` gate
# was true for every job and a user echoing the marker tripped the guard.
GAP_CRASH = re.compile(r"(Gap crashed|Restarting Gap)")
GAP_LIMIT = _int_env("SAGE_MCP_GAP_LIMIT", 3)  # 0 disables the guard


def _is_gap_interface(origin):
    """True when the writing frame is Sage's own GAP interface source file."""
    return bool(origin) and "sage/interfaces" in origin and os.path.basename(origin) == "gap.py"


class Stats:
    """Kernel-owned counters (the kernel is the only process that knows).

    They reset with the kernel, so `since` is exported as `counters_since`: these
    are never lifetime totals.
    """

    def __init__(self):
        self.since = time.time()
        self.run = 0  # jobs that finished
        self.failed = 0  # finished with an error that was not an interrupt
        self.interrupted = 0  # finished as KeyboardInterrupt
        self.gap_trips = 0
        self.last_job = None
        self.last_error = None  # {"error", "job", "session", "when"}

    def submitted(self, job_id):
        self.last_job = job_id

    def finished(self, job):
        self.run += 1
        if not job.error:
            return
        if job.error.startswith("KeyboardInterrupt"):
            self.interrupted += 1
        else:
            self.failed += 1
        self.last_error = {
            "error": _error_line(job.error),
            "job": job.id,
            "session": job.session,
            "when": time.time(),
        }


STATS = Stats()

# global state
_LOG = sys.stderr  # the kernel log is the original stderr (-> kernel.log)
# Reentrant: a limit signal (SIGXCPU/SIGXFSZ) can arrive while this thread is
# inside _log, and its handler logs too.
_LOG_LOCK = threading.RLock()

_JOBS = {}  # job_id -> Job
_JOBS_LOCK = threading.RLock()
_QUEUE = queue.Queue()
_JobIds = itertools.count(1)

_SESSIONS = {}  # name -> namespace dict
_SESSIONS_LOCK = threading.RLock()
_BASE = {}  # shared base namespace built from `from sage.all import *`

_CURRENT = None  # Job currently executing (only the main thread sets this)
_CURRENT_LOCK = threading.Lock()
_SHUTDOWN = threading.Event()

STATE_FILE = None
RUNNING_FILE = "running.json"  # lives next to the state file
SOCK_NAME = "kernel.sock"
SOCK_PATH = None
LOG_PATH = None
SAGE_VERSION = "?"
CLIENT_TTL = 60.0  # seconds a client id stays "live" after its last request
_CLIENTS = {}  # client id -> time of its last request
_CLIENTS_LOCK = threading.Lock()


LOG_MAX = _int_env("SAGE_MCP_LOG_MAX", 8 * 1024 * 1024)  # bytes; 0 disables


def _rotate_log():
    """kernel.log -> kernel.log.1 once it reaches LOG_MAX (8 MB by default).

    RLIMIT_FSIZE would otherwise let a chatty kernel earn SIGXFSZ on its own log.
    The log is fd 1/2 (dup2), so the rotation rebinds those descriptors and every
    writer -- the routers' fallback and _LOG -- follows.
    """
    if not LOG_MAX or not LOG_PATH:
        return
    try:
        if os.path.getsize(LOG_PATH) < LOG_MAX:
            return
        os.replace(LOG_PATH, LOG_PATH + ".1")
        fd = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        os.close(fd)
    except OSError:
        pass


def _log(msg, rotate=True):
    """Kernel diagnostics go to the original stderr, never through the router.

    `rotate=False` is for callers inside a signal handler: renaming the log and
    rebinding fd 1/2 from a handler can interleave with a writer mid-write.
    """
    try:
        with _LOG_LOCK:
            if rotate:
                _rotate_log()
            _LOG.write(str(msg).rstrip("\n") + "\n")
            _LOG.flush()
    except BaseException:  # a limit signal raised in here must not kill us
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
            # A timeout means SOMETHING accepted (a busy kernel's backlog):
            # never unlink what we cannot prove dead.
            raise SystemExit(
                "sage-mcp kernel: %s exists and could not be proven dead (%s); "
                "refusing to start a second kernel on it" % (path, exc)
            )
        else:
            raise SystemExit(
                "sage-mcp kernel: another kernel is already listening on %s" % path
            )
        finally:
            probe.close()
    # accept() runs the challenge/response handshake, so a connection without
    # the authkey never reaches an op handler.
    listener = Listener(path, backlog=128, authkey=authkey)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return listener

# per-job output routing and caps


class Job:
    __slots__ = (
        "id", "session", "code", "preparse", "submitted", "started", "finished",
        "state", "parts", "nbytes", "truncated", "error", "repr", "done", "lock",
        "cancel", "blocked", "owner", "gap_crashes", "gap_guarded", "gap_touched",
        "note", "limit_guarded",
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
        self.gap_crashes = 0  # GAP crash markers seen, per job
        self.gap_guarded = False  # did this job already get its one SIGINT?
        self.gap_touched = False  # did a gap.py frame write anything at all?
        self.note = None  # prepended to the job's result text
        self.limit_guarded = False  # did a rlimit already raise into this job?

    def append(self, text, stream="stdout", origin=""):
        if not text:
            return
        with self.lock:
            if not self.truncated:
                room = OUTPUT_CAP - self.nbytes
                if len(text) > room:
                    self.parts.append(text[: max(0, room)])
                    self.nbytes += max(0, room)
                    self.parts.append(
                        "\n[output truncated at 4 MB — further output dropped]\n"
                    )
                    self.truncated = True
                else:
                    self.parts.append(text)
                    self.nbytes += len(text)
        # Markers are counted even once the cap has dropped the text: a chatty
        # job must not be able to escape the guard by filling its output first.
        if stream == "stderr" or _is_gap_interface(origin):
            self._gap_guard(text, stream, origin)

    def _gap_guard(self, text, stream, origin):
        """One SIGINT per job when GAP's own interface writes crash markers.

        The evidence is the writer (see GAP_CRASH above): a gap.py frame, or a
        stderr write from a job that has already written through gap.py.  A job
        that merely has `sage.interfaces.gap` imported -- which `from sage.all
        import *` does for every job -- cannot trip it, and neither can a user
        echoing the marker.  The signal is the same self-SIGINT that `interrupt`
        uses; what it *did* is decided when the job ends (`_finish_gap_note`),
        because the job may catch the KeyboardInterrupt and run on.
        """
        if not GAP_LIMIT:
            return
        from_gap = _is_gap_interface(origin)
        if from_gap:
            self.gap_touched = True  # this job really is driving the interface
        if not (from_gap or (stream == "stderr" and self.gap_touched)):
            return
        if self.gap_guarded:
            return  # one signal per job, however many markers follow
        self.gap_crashes += len(GAP_CRASH.findall(text))
        if self.gap_crashes < GAP_LIMIT:
            return
        self.gap_guarded = True
        STATS.gap_trips += 1
        _log(
            "gap guard: %d crash markers in job %s -> SIGINT"
            % (self.gap_crashes, self.id)
        )
        os.kill(os.getpid(), signal.SIGINT)  # lands on the executor (main) thread

    def snapshot(self, since=0):
        with self.lock:
            text = "".join(self.parts)
            truncated = self.truncated
        total = len(text)
        since = max(0, min(int(since or 0), total))
        return text[since:], total, truncated

    def info(self, since=0):
        text, offset, truncated = self.snapshot(since)
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
            "note": self.note,
            "elapsed_s": round(time.time() - ref, 3),
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

    @staticmethod
    def origin():
        """The file that called print()/write(): GAP's markers come from gap.py."""
        try:
            return sys._getframe(2).f_code.co_filename
        except (ValueError, AttributeError):
            return ""

    def write(self, data):
        data = data if isinstance(data, str) else str(data)
        if not data:
            return 0
        job = _CURRENT
        if job is not None:
            job.append(data, self.name, self.origin())
        else:
            try:
                self._fallback.write(data)
                self._fallback.flush()
            except Exception:
                pass
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

# time.sleep patch


def _patch_sleep():
    """The preparser rewrites ``time.sleep(0.4)`` as ``sleep(RealNumber('0.4'))``;
    ``__index__`` rounds that to 0, so the sleep would return instantly."""
    import time as _time

    original = _time.sleep

    def sleep(seconds=0):
        try:
            return original(float(seconds))
        except (TypeError, ValueError):
            return original(seconds)

    _time.sleep = sleep

# sessions


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

# execution (rules 1, 2, 5, 6, 7, 8)


def _prepare_source(job):
    src = job.code
    if job.preparse:
        from sage.repl.preparse import preparse

        src = preparse(src, True)
    if src.startswith("\ufeff"):  # preparse keeps a BOM; ast.parse cannot
        src = src[1:]
    return src.replace("\r\n", "\n").replace("\r", "\n")


def _syntax_report(exc, src):
    """A SyntaxError must carry the *preparsed* source: the only way to debug a
    bad rewrite."""
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
    return "\n".join(
        lines + ["--- preparsed source ---", body, "--- end preparsed source ---"]
    )


def _format_error(exc):
    if isinstance(exc, KeyboardInterrupt):
        return (
            "KeyboardInterrupt: interrupted by SIGINT "
            "(the session and its variables are preserved)"
        )
    if isinstance(exc, SystemExit):
        return "SystemExit: %s (the kernel keeps running)" % (exc.code,)
    kept = [
        frame
        for frame in traceback.format_tb(exc.__traceback__)
        if os.path.basename(__file__) not in frame
    ]
    head = "Traceback (most recent call last):\n" + "".join(kept) if kept else ""
    tail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    return (head + tail).strip() or "%s: %s" % (type(exc).__name__, exc)


def _running_path():
    return os.path.join(os.path.dirname(STATE_FILE), RUNNING_FILE) if STATE_FILE else None


def _write_running(job):
    """Publish the running job (id + owner) next to the state file.

    A client must be able to answer "is the running job mine?" *before* sending
    SIGINT, and it cannot ask us: the job may be holding the GIL at the time.
    """
    path = _running_path()
    if path is None:
        return
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(
                {
                    "job": job.id,
                    "owner": job.owner,
                    "session": job.session,
                    "started": job.started,
                    "pid": os.getpid(),
                },
                fh,
            )
        os.replace(tmp, path)
    except OSError:
        pass


def _clear_running():
    path = _running_path()
    if path is None:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def _run_job(job):
    global _CURRENT
    try:  # per job, because the per-UID task count drifts (limits.py)
        limits_mod.arm_nproc(LIMITS)
    except limits_mod.LimitError as exc:
        _log("could not arm nproc: %s" % exc)
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
        tail = body.pop() if body and isinstance(body[-1], ast.Expr) else None
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
    except BaseException as exc:  # nothing escapes
        job.limit_guarded = True  # a late limit signal must not clobber the error
        job.error = _format_error(exc)
    finally:
        _clear_running()
        job.finished = time.time()
        job.state = "done"
        with _CURRENT_LOCK:
            _CURRENT = None  # route later output to the real stream again
        _finish_gap_note(job)
        STATS.finished(job)
        job.done.set()


def _finish_gap_note(job):
    """Say what the guard's SIGINT actually did, once the job has ended.

    The signal is asynchronous: the job may swallow the KeyboardInterrupt and
    finish normally, so the note cannot be written at trip time -- it would
    claim an interruption that never happened.  The "interrupted" counter needs
    no fixing here: `Stats.finished` counts a KeyboardInterrupt error, and a job
    that continued has no error.
    """
    if not job.gap_guarded:
        return
    if job.error and job.error.startswith("KeyboardInterrupt"):
        job.note = (
            "[gap-guard] interrupted after %d 'Gap crashed' markers; "
            "structure_description() forks a process per crash on this input — "
            "try a different invariant/representation." % job.gap_crashes
        )
        return
    job.note = (
        "[gap-guard] %d 'Gap crashed' markers; SIGINT was sent but the job "
        "continued (it caught KeyboardInterrupt), so nothing was stopped — "
        "if this is structure_description(), stop the job yourself with "
        "interrupt or the crash loop will not end." % job.gap_crashes
    )


def _error_line(text, limit=300):
    """The line a reader wants in `status`: the exception, not its header."""
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
        except KeyboardInterrupt:  # an idle interrupt is not fatal
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
        except ResourceLimitError as exc:
            # Same sliver, for a limit: the job is already released.
            _log("resource limit outside a job: %s" % exc)
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

# operations (called from connection threads)


def op_hello(req):
    return {
        "ok": True,
        "pid": os.getpid(),
        "sock": SOCK_PATH,
        "started": STARTED,
        "log": LOG_PATH,
        "sage_version": SAGE_VERSION,
        "python": sys.version.split()[0],
    }


def op_ping(req):
    return {"ok": True}


def op_submit(req):
    session = str(req.get("session") or "default")
    preparse = bool(req.get("preparse", True))
    job = Job("j%d" % next(_JobIds), session, req.get("code"), preparse, req.get("client"))
    with _CURRENT_LOCK:
        current = _CURRENT
    with _JOBS_LOCK:
        job.blocked = current is not None or any(
            other.state in ("queued", "running") for other in _JOBS.values()
        )
        _JOBS[job.id] = job
    STATS.submitted(job.id)
    _log("submit %s session=%s preparse=%s %dB" % (job.id, session, preparse, len(job.code or "")))
    wait = float(req.get("wait") or 0)
    since = int(req.get("since") or 0)
    if wait > 0:
        _QUEUE.put(job.id)
        job.done.wait(wait)
        return {"ok": True, "job_id": job.id, "job": job.info(since)}
    # Enqueue only *after* the reply is on the wire: a job that grabs the GIL
    # immediately (a tight loop) would otherwise starve the reply and the client
    # would lose the job id.
    return {"ok": True, "job_id": job.id, "job": job.info(since)}, lambda: _QUEUE.put(job.id)


def _lookup(req):
    with _JOBS_LOCK:
        return _JOBS.get(req.get("job_id"))


def op_read(req):
    """`poll` (no wait) and `wait` (blocks up to timeout) share one reader."""
    job = _lookup(req)
    if job is None:
        return {
            "ok": False,
            "error": "unknown job %r (the kernel may have restarted)" % (req.get("job_id"),),
        }
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
    ns.update(_BASE)
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
            if jid is not None:  # refuse a session with work in flight
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
    return {
        "ok": True,
        "pid": os.getpid(),
        "sock": SOCK_PATH,
        "started": STARTED,
        "uptime_s": round(time.time() - STARTED, 1),
        "sessions": sessions,
        "jobs": {
            "queued": queued,
            "running": running,
            "run": STATS.run,
            "failed": STATS.failed,
            "interrupted": STATS.interrupted,
            "last_job": STATS.last_job,
        },
        "clients": {"live": len(_live_clients()), "ttl_s": CLIENT_TTL},
        "last_error": STATS.last_error,
        "log": LOG_PATH,
        "sage_version": SAGE_VERSION,
        "python": sys.version.split()[0],
        "gap_trips": STATS.gap_trips,
        "counters_since": STATS.since,
        "limits": limits_mod.as_json(LIMITS, "kernel"),
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
    """Who else is using this kernel?  A server asks before it exits."""
    live = _live_clients()
    others = [c for c in live if c != req.get("client")]
    return {"ok": True, "clients": len(live), "others": len(others), "ttl_s": CLIENT_TTL}


def op_bye(req):
    """Deregister a server that is exiting, so it stops counting as a client."""
    cid = req.get("client")
    if isinstance(cid, str) and cid:
        with _CLIENTS_LOCK:
            _CLIENTS.pop(cid, None)
    return {"ok": True, "deregistered": bool(cid)}

OPS = {
    "hello": op_hello,
    "ping": op_ping,
    "submit": op_submit,
    "poll": op_read,
    "wait": op_read,
    "reset": op_reset,
    "drop": op_drop,
    "status": op_status,
    "interrupt": op_interrupt,
    "shutdown": op_shutdown,
    "clients": op_clients,
    "bye": op_bye,
}


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
    """One connection: read one request, write one reply.

    No read timeout: a `wait` op may block for a long time, and the thread ends
    as soon as the peer closes or the reply is written.
    """

    def __init__(self, conn):
        self.conn = conn

    def _send(self, payload):
        try:
            # errors="replace": a job may print a lone surrogate (os.fsdecode of
            # non-UTF-8 bytes); the reply must still be a valid frame (C19).
            self.conn.send_bytes(
                json.dumps(payload, ensure_ascii=False).encode("utf-8", "replace")
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
        fn = OPS.get(op)
        post = None
        try:
            resp = fn(req) if fn else {"ok": False, "error": "unknown op %r" % (op,)}
            if isinstance(resp, tuple):  # (reply, post_reply_callback)
                resp, post = resp
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
        try:
            threading.Thread(
                target=_serve_conn, args=(conn,), name="conn", daemon=True
            ).start()
        except RuntimeError as exc:  # e.g. RLIMIT_NPROC: a job must not blind us
            _log("could not start a connection thread: %s" % exc)
            try:
                conn.close()
            except OSError:
                pass

# startup


def _remove_socket():
    if SOCK_PATH:
        try:
            os.unlink(SOCK_PATH)
        except OSError:
            pass


def _write_state(path):
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w") as fh:
        json.dump(
            {"pid": os.getpid(), "sock": SOCK_PATH, "started": STARTED,
             "log": LOG_PATH, "version": 1},
            fh,
        )
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def main():
    global _LOG, STATE_FILE, LOG_PATH, SAGE_VERSION, SOCK_PATH, LIMITS
    ap = argparse.ArgumentParser(description="sage-mcp kernel")
    ap.add_argument("--state-file", required=True, help="where to publish {pid,sock}")
    ap.add_argument("--log-file", default=None, help="redirect stdout/stderr here")
    args = ap.parse_args()

    STATE_FILE = os.path.abspath(args.state_file)
    SOCK_PATH = os.path.join(os.path.dirname(STATE_FILE), SOCK_NAME)
    try:  # the same checks the client makes: a hand-started kernel gets them too
        private_dir(os.path.dirname(STATE_FILE))
        check_sock_path(SOCK_PATH)
    except Exception as exc:
        raise SystemExit("sage-mcp kernel: %s" % (exc,))
    if args.log_file:
        LOG_PATH = os.path.abspath(args.log_file)
        fd = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        os.close(fd)
        sys.stdout = os.fdopen(1, "w", buffering=1)
        sys.stderr = os.fdopen(2, "w", buffering=1)
    _log("=== sage-mcp kernel booting (pid %d) %s ==="
         % (os.getpid(), time.strftime("%Y-%m-%d %H:%M:%S")))

    # Limits before the import: `import sage.all` peaks near 4 GB of address
    # space, so RLIMIT_AS must already hold.  `cpu` and `nproc` are applied
    # *after* the import: RLIMIT_CPU counts from exec (Sage's import costs
    # seconds) and RLIMIT_NPROC counts tasks, threads included (the import
    # starts threads, so a small margin would break it).
    try:
        LIMITS = limits_mod.parse()
        limits_mod.no_core()  # always, whatever else is configured
        limits_mod.apply(LIMITS, kinds=("as", "fsize"))
    except limits_mod.LimitError as exc:
        raise SystemExit("sage-mcp kernel: SAGE_MCP_LIMITS: %s" % exc)
    # A limit must fail the job, never the kernel: these handlers raise into the
    # running job (SIGXCPU/SIGXFSZ default action would kill the kernel).
    signal.signal(signal.SIGXCPU, _on_limit)
    signal.signal(signal.SIGXFSZ, _on_limit)
    _log("limits: %s" % limits_mod.describe(LIMITS))

    # Log first, then take over the streams: from here on kernel diagnostics go
    # through _log() (original stderr) and user output through the routers.
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout = _Router("stdout", real_out)
    sys.stderr = _Router("stderr", real_err)
    _patch_sleep()

    def _on_term(*_args):  # SIGTERM must exit promptly, even mid-job
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
    if LIMITS.get("cpu"):
        try:
            applied = limits_mod.apply(LIMITS, kinds=("cpu",))
        except limits_mod.LimitError as exc:
            raise SystemExit("sage-mcp kernel: SAGE_MCP_LIMITS: %s" % exc)
        _log("limits applied after the import: cpu=%s" % applied.get("cpu"))
    if LIMITS.get("nproc"):
        _log("limits: nproc=%s is armed before each job (a margin over the UID's "
             "task count)" % limits_mod.human("nproc", LIMITS))

    authkey = load_or_create_key(os.path.dirname(STATE_FILE))
    listener = _bind_server(SOCK_PATH, authkey)
    _write_state(STATE_FILE)
    _log("listening on unix:%s (authkey), state=%s, sage=%s"
         % (SOCK_PATH, STATE_FILE, SAGE_VERSION))
    try:
        threading.Thread(
            target=_accept_loop, args=(listener,), name="conn", daemon=True
        ).start()
    except RuntimeError as exc:  # almost always an nproc limit below Sage's needs
        raise SystemExit(
            "sage-mcp kernel: cannot start the accept thread (%s).  Sage's import "
            "already created %d threads; SAGE_MCP_LIMITS nproc=%s is per-UID, so it "
            "must cover every task you run, not just this kernel."
            % (exc, threading.active_count(), limits_mod.human("nproc", LIMITS))
        )

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
