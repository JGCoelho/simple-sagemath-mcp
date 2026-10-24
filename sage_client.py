#!/usr/bin/env python3
"""sage_client.py -- client side of sage-mcp: find, spawn, talk to the kernel.

Rules encoded here (each one was a bug):

* A **busy** kernel is not a dead kernel: if the socket accepts a connection but
  the handshake/reply times out, the kernel is alive and merely holding the GIL
  inside a computation.  Raise :class:`KernelBusy` and NEVER respawn.
* The socket lives in a directory we own, mode 0700: ``/tmp`` is sticky, not
  protective -- another user could create ``/tmp/sage-mcp-<uid>`` first.
* Framing is ``multiprocessing.connection`` (``Client`` + ``send_bytes`` /
  ``recv_bytes``), authenticated with a 32-byte authkey kept in a 0600 file in
  that directory -- never on a command line.
* Spawning is serialised with ``O_CREAT|O_EXCL`` on ``spawn.lock``, and only a
  state file written after this spawn began is trusted.  A dead launcher is
  reported at once, with the log tail; a stale state file is never adopted.
"""

import json
import os
import shutil
import stat
import subprocess
import threading
import time
import uuid
from multiprocessing.connection import AuthenticationError, Client as ConnectClient

MAX_RESPONSE = 64 * 1024 * 1024  # every reply read is capped
SPAWN_TIMEOUT = 180.0  # how long we wait for a launcher
SPAWN_LOCK = "spawn.lock"
STATE_NAME = "kernel.json"
SOCK_NAME = "kernel.sock"  # AF_UNIX: no TCP port, nothing another user can dial
RUNNING_NAME = "running.json"  # written by the kernel: the job executing now
LOG_NAME = "kernel.log"
KEY_NAME = "kernel.key"  # the kernel's authkey: 32 random bytes, mode 0600
KEY_BYTES = 32
DIR_MODE = 0o700
SUN_PATH_MAX = 100  # Linux sun_path is 108 bytes including the trailing NUL
HELLO_TIMEOUT = 5.0
OP_TIMEOUT = 30.0
KERNEL_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sage_kernel.py")


class KernelError(Exception):
    """Base class for client-side kernel trouble."""


class KernelDown(KernelError):
    """No kernel: connection refused/gone, launcher died, spawn failed."""


class KernelBusy(KernelError):
    """Alive but not answering: a computation holds the GIL. Never respawn."""


def default_state_dir():
    override = os.environ.get("SAGE_MCP_STATE")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join("/tmp", "sage-mcp-%d" % os.getuid())


def private_dir(path):
    """Create the state dir as 0700 and refuse to use one that is not ours.

    A kernel in a directory another user controls is local code execution as
    us: they read the socket path and the authkey.  The sticky bit on /tmp does
    not stop them creating the name first, so ownership is verified.
    """
    try:
        os.makedirs(path, mode=DIR_MODE, exist_ok=True)
        st = os.lstat(path)
    except OSError as exc:
        raise KernelError("cannot use the state directory %s: %s" % (path, exc))
    if not stat.S_ISDIR(st.st_mode):
        raise KernelError(
            "the state path %s is not a directory (a symlink or a file); refusing "
            "to put a kernel there" % (path,)
        )
    if st.st_uid != os.getuid():
        raise KernelError(
            "the state directory %s belongs to uid %d, not to us (uid %d); refusing "
            "to run a kernel in a directory another user controls"
            % (path, st.st_uid, os.getuid())
        )
    if stat.S_IMODE(st.st_mode) != DIR_MODE:
        try:
            os.chmod(path, DIR_MODE)
        except OSError as exc:
            raise KernelError(
                "the state directory %s is %03o and cannot be made private (%s)"
                % (path, stat.S_IMODE(st.st_mode), exc)
            )


def check_sock_path(path):
    """AF_UNIX paths have a hard byte limit; fail with advice, not errno 90."""
    length = len(os.fsencode(path))
    if length > SUN_PATH_MAX:
        raise KernelError(
            "the kernel socket path is %d bytes, over the %d-byte AF_UNIX limit:\n%s\n"
            "Set SAGE_MCP_STATE to a shorter directory."
            % (length, SUN_PATH_MAX, path)
        )


def _sage_ish(path):
    """True when a `sage` executable sits next to this interpreter.

    The sage environment's bin dir must hold both: sage there is what makes
    `import sage.all` work at all (Singular and friends live in the same dir).
    """
    return os.access(os.path.join(os.path.dirname(path), "sage"), os.X_OK)

DISCOVERY_HELP = (
    "no interpreter that can 'import sage.all' was found.  Tried, in order:\n"
    "  1. $SAGE_PYTHON -- not set, or not an existing file\n"
    "  2. `sage -c 'import sys;print(sys.executable)'` -- no `sage` on PATH, or it failed\n"
    "  3. the python beside `sage` on PATH -- no `sage` on PATH, or no python beside it\n"
    "Point SAGE_PYTHON at Sage's own interpreter, e.g.\n"
    "  SAGE_PYTHON=$(sage -c 'print(__import__(\"sys\").executable)')"
)

_PYTHON = []  # cache of the *discovered* answer: `sage -c` is a process spawn


def discovery_report():
    """What is known about Sage's interpreter *without* starting anything.

    `status` is meant to be cheap and must never block on a `sage -c` spawn, so
    this reads $SAGE_PYTHON and PATH only, plus whatever a previous real
    resolution cached.  The `detail` field is the sentence a user needs when
    `exec` cannot start a kernel -- above all the case of a pip/uvx install on a
    machine with no SageMath at all, where nothing else would say so.
    """
    explicit = os.environ.get("SAGE_PYTHON") or ""
    sage_bin = shutil.which("sage")
    report = {
        "interpreter": None,
        "source": None,
        "sage_on_path": bool(sage_bin),
        "detail": None,
    }
    if explicit:
        if os.path.exists(explicit):
            report.update(interpreter=os.path.abspath(explicit), source="SAGE_PYTHON")
            return report
        report["detail"] = "$SAGE_PYTHON is set to %r, which does not exist" % explicit
        return report
    if _PYTHON and _PYTHON[0]:
        report.update(interpreter=_PYTHON[0], source="discovered (sage -c)")
        return report
    if not sage_bin:
        report["detail"] = "$SAGE_PYTHON is not set and there is no `sage` on PATH"
        return report
    report["detail"] = (
        "`sage` is at %s but its interpreter has not been probed yet (the first "
        "exec or run_file does that)" % sage_bin
    )
    return report


def discovery_error():
    """The failure a user sees when no Sage interpreter can be found."""
    report = discovery_report()
    if not report["sage_on_path"] and not os.environ.get("SAGE_PYTHON"):
        # The first line is what a one-line renderer shows (the checker logs only
        # that), so the install hint lives there -- after the diagnosis, which
        # stays the same phrase every client and doc already knows.
        return DISCOVERY_HELP.replace(
            "no interpreter that can 'import sage.all' was found.",
            "SageMath does not look installed on this machine: no interpreter that can "
            "'import sage.all' was found.  pip and uvx install this MCP server, not "
            "Sage -- the kernel is Sage's own interpreter, a separate installation.",
            1,
        )
    if report["detail"]:
        return report["detail"] + "\n" + DISCOVERY_HELP
    return DISCOVERY_HELP


def resolve_python():
    """Sage's interpreter for the kernel (and run_file/notebook children).

    The only place that knows where Sage is: $SAGE_PYTHON, then
    `sage -c 'import sys;print(sys.executable)'`, then the python beside `sage`
    on PATH.  Returns None when none works -- a client must still be
    constructible then, or even `status` could not explain the problem.
    $SAGE_PYTHON is re-read every call (a test may set it); only `sage -c`,
    which spawns a process, is cached.
    """
    explicit = os.environ.get("SAGE_PYTHON")
    if explicit and os.path.exists(explicit):
        return os.path.abspath(explicit)
    if _PYTHON:
        return _PYTHON[0]
    sage_bin = shutil.which("sage")
    if sage_bin:
        try:
            done = subprocess.run(
                [sage_bin, "-c", "import sys;print(sys.executable)"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=120,
            )
            candidate = done.stdout.strip().splitlines()[-1] if done.returncode == 0 else ""
            if candidate and os.path.exists(candidate) and _sage_ish(candidate):
                _PYTHON.append(os.path.abspath(candidate))
                return _PYTHON[0]
        except (OSError, subprocess.SubprocessError, IndexError):
            pass
        # prefix layouts (conda, source builds) call it `python`; distro
        # packages ship `python3` only, next to the same `sage`
        beside = os.path.dirname(os.path.realpath(sage_bin))
        for name in ("python", "python3"):
            candidate = os.path.join(beside, name)
            if os.path.exists(candidate) and _sage_ish(candidate):
                _PYTHON.append(os.path.abspath(candidate))
                return _PYTHON[0]
    _PYTHON.append(None)
    return None


def sage_python():
    """Sage's interpreter, or a KernelError naming the three discovery routes."""
    python = resolve_python()
    if not python:
        raise KernelError(discovery_error())
    return python


def load_or_create_key(state_dir):
    """The kernel's authkey: 32 random bytes, mode 0600, in the 0700 state dir.

    A file rather than argv/env (both are readable from `ps` and /proc), and
    *reused* across kernel generations so adoption keeps working.  Written
    atomically so a concurrent reader never sees a half-written key.
    """
    path = os.path.join(state_dir, KEY_NAME)
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        if len(data) >= KEY_BYTES:
            return data
    except OSError:
        pass
    data = os.urandom(KEY_BYTES)
    tmp = path + ".tmp.%d" % os.getpid()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return data


def _alive(pid):
    """True only for a *live* process: a zombie is a dead process to us."""
    try:
        pid = int(pid)
    except (ValueError, TypeError, OverflowError):
        return False
    if not pid:
        return False
    try:
        with open("/proc/%d/stat" % pid, "r") as fh:
            state = fh.read().rsplit(")", 1)[-1].split()
        if state and state[0] in ("Z", "X", "x"):
            return False
        return True
    except OSError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def tail_log(path, lines=12):
    try:
        with open(path, "r", errors="replace") as fh:
            data = fh.read()[-200000:]
    except OSError:
        return "(no kernel.log)"
    rows = [row for row in data.splitlines() if row.strip()]
    return "\n".join(rows[-lines:]) if rows else "(kernel.log is empty)"


def _is_our_kernel(pid, state_path):
    """Guard against pid reuse: a stale state file must not point at a stranger."""
    try:
        with open("/proc/%d/cmdline" % int(pid), "rb") as fh:
            cmd = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return False
    # The full path, not its basename: two state dirs both pass
    # `--state-file .../kernel.json`, and a basename match would confuse them.
    return "sage_kernel.py" in cmd and state_path in cmd


def _stuck_text(pid, why):
    """Rule 17: a kernel we could not stop keeps its state files -- say so."""
    return (
        "could not stop the kernel: %s.\n"
        "The state files were left in place on purpose: pid %s may still be alive, and "
        "deleting them would orphan it and let the next call spawn a second kernel.\n"
        "Stop it yourself with: kill -9 %s" % (why, pid, pid)
    )


class Client:
    """One process-wide handle on the kernel; safe to share across threads."""

    def __init__(self, state_dir=None):
        self.state_dir = os.path.abspath(state_dir or default_state_dir())
        private_dir(self.state_dir)
        self.state_path = os.path.join(self.state_dir, STATE_NAME)
        self.sock_path = os.path.join(self.state_dir, SOCK_NAME)
        check_sock_path(self.sock_path)
        self.lock_path = os.path.join(self.state_dir, SPAWN_LOCK)
        self.log_path = os.path.join(self.state_dir, LOG_NAME)
        self.key_path = os.path.join(self.state_dir, KEY_NAME)
        self.python = resolve_python()  # None: Sage was not found (see DISCOVERY_HELP)
        self.cwd = os.environ.get("SAGE_CWD") or "/tmp"
        self.proc = None  # our own launcher, if we ever spawned one
        self._spawn_lock = threading.Lock()
        # Identifies this MCP server to the kernel: several servers may share one
        # kernel, and each one must be able to tell whether it is the last user.
        self.client_id = uuid.uuid4().hex
        self._ever_ready = False  # did this server hand out a kernel already?
        self._killed_pid = None  # pid stopped by kill(), for the restart notice
        self.spawns = 0  # kernels this server launched (observability)
        self.restarts = 0  # kernels replaced after this server had handed one out

    # -- state file -------------------------------------------------------
    def read_state(self):
        try:
            with open(self.state_path, "r") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or "pid" not in data:
            return None
        return data

    def _write_state(self, payload):
        tmp = self.state_path + ".tmp.%d" % os.getpid()
        with open(tmp, "w") as fh:
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.state_path)

    def _unlink_state(self):
        for path in (
            self.state_path,
            os.path.join(self.state_dir, RUNNING_NAME),
            self.sock_path,
        ):
            try:
                os.unlink(path)
            except OSError:
                pass

    # -- transport --------------------------------------------------------
    def _authkey(self):
        """The authkey the running kernel expects, or None when it is missing."""
        try:
            with open(self.key_path, "rb") as fh:
                data = fh.read()
        except OSError:
            return None
        return data if len(data) >= KEY_BYTES else None

    def _connect(self, budget):
        """Connect + authenticate; KernelBusy when the kernel is alive but wedged.

        `multiprocessing.connection.Client` runs the challenge/response handshake
        with blocking reads, and a busy kernel holds the GIL -- its accept thread
        cannot answer -- so the handshake runs in a helper thread bounded by
        `budget`.  Busy is then a clean KernelBusy, never a hang.
        """
        key = self._authkey()
        if key is None:
            raise KernelDown(
                "the kernel authkey %s is missing or too short; it is created next "
                "to kernel.json when a kernel is spawned -- stop the running kernel "
                "with interrupt(force=true) and retry" % (self.key_path,)
            )
        box = {}
        abandoned = threading.Event()

        def attempt():
            try:
                conn = ConnectClient(self.sock_path, authkey=key)
            except BaseException as exc:  # noqa: BLE001 - reported to the caller
                box["error"] = exc
                return
            if abandoned.is_set():
                conn.close()
                return
            box["conn"] = conn

        helper = threading.Thread(target=attempt, daemon=True, name="kernel-connect")
        helper.start()
        helper.join(budget)
        if helper.is_alive():
            abandoned.set()
            raise KernelBusy(
                "the kernel accepted the connection but did not answer within "
                "%.1fs (a computation is probably holding the GIL; it is alive, "
                "not dead)" % budget
            )
        exc = box.get("error")
        if exc is not None:
            if isinstance(exc, AuthenticationError):
                raise KernelDown(
                    "the kernel at %s refused the authkey from %s (%s): the running "
                    "kernel was started with a different key, or the file was "
                    "replaced.  Stop it with interrupt(force=true) and retry."
                    % (self.sock_path, self.key_path, exc)
                )
            raise KernelDown(
                "cannot connect to the kernel socket %s: %s" % (self.sock_path, exc)
            )
        return box["conn"]

    def _roundtrip_state(self, payload, timeout):
        if isinstance(payload, dict):
            payload.setdefault("client", self.client_id)
        conn = self._connect(timeout)
        try:
            try:
                conn.send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            except OSError as exc:
                raise KernelDown("lost the connection to the kernel: %s" % (exc,))
            if not conn.poll(timeout):
                raise KernelBusy(
                    "the kernel accepted the connection but did not answer within "
                    "%.1fs (a computation is probably holding the GIL; it is alive, "
                    "not dead)" % timeout
                )
            try:
                raw = conn.recv_bytes(MAX_RESPONSE)
            except EOFError:
                raise KernelBusy(
                    "kernel accepted the connection but closed it without answering"
                )
            except OSError as exc:
                raise KernelDown("error while reading from the kernel: %s" % (exc,))
        finally:
            try:
                conn.close()
            except OSError:
                pass
        if raw is None:
            raise KernelError(
                "kernel reply exceeded the 64 MB client cap; reduce the output "
                "(e.g. truncate reprs) and retry"
            )
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise KernelDown("kernel sent a non-JSON reply: %r" % (raw[:200],))

    # -- lifecycle --------------------------------------------------------
    def hello(self, state=None, timeout=HELLO_TIMEOUT):
        state = state or self.read_state()
        if state is None:
            raise KernelDown("no kernel state file at %s" % self.state_path)
        reply = self._roundtrip_state({"op": "hello"}, timeout)
        if not reply.get("ok"):
            raise KernelDown("hello failed: %s" % (reply.get("error"),))
        return reply

    def op(self, name, _timeout=OP_TIMEOUT, **params):
        """Send an op; ``_timeout`` is the socket budget, params go to the kernel."""
        if self.read_state() is None:
            raise KernelDown("no kernel state file at %s" % self.state_path)
        payload = {"op": name}
        payload.update(params)
        reply = self._roundtrip_state(payload, _timeout)
        if not reply.get("ok"):
            raise KernelError(str(reply.get("error") or ("%s failed" % name)))
        return reply

    def ensure(self):
        """Return ``(state, restarted, reason)``; spawn only when truly needed.

        Raises :class:`KernelBusy` when the kernel is alive but wedged (never a
        respawn), :class:`KernelDown` when it cannot be reached at all.
        """
        was_ready = self._ever_ready
        state = self.read_state()
        if state is not None:
            pid = state.get("pid")
            if not self._still_there(pid):
                reason = "the previous kernel (pid %s) is gone" % (pid,)
            elif not _is_our_kernel(pid, self.state_path):
                reason = (
                    "the state file pointed at pid %s, which is not our Sage kernel "
                    "(stale file)" % (pid,)
                )
            else:
                try:
                    self.hello(state)
                    self._ever_ready = True
                    return state, False, None
                except KernelBusy:
                    # Rule 11: busy != dead. Hand the state back untouched.
                    return state, False, "busy"
                except KernelDown as exc:
                    if _alive(pid):
                        raise KernelDown(
                            "kernel pid %s is alive but not accepting connections "
                            "(%s); refusing to start a second kernel — use "
                            "interrupt(force=true)" % (pid, exc)
                        )
                    reason = "the previous kernel (pid %s) stopped answering" % (pid,)
        else:
            killed = self._killed_pid
            self._killed_pid = None
            if killed:
                reason = "the previous kernel (pid %s) was force-stopped with interrupt(force=true)" % (killed,)
            else:
                reason = "no kernel was running"
        state = self._spawn(reason)
        if state.pop("_adopted", False):
            reason = (
                "the state file was missing; the live kernel (pid %s) was adopted"
                % (state.get("pid"),)
            )
        if was_ready:
            self.restarts += 1
        self._ever_ready = True
        return state, was_ready, reason

    def _spawn(self, reason):
        with self._spawn_lock:
            # Re-check: another thread may have spawned while we waited.  Only a
            # state we can *verify* as our kernel may be adopted.
            state = self._verified_state()
            return state if state is not None else self._spawn_locked(reason)

    def _verified_state(self, timeout=2.0):
        """Our verified live kernel's state (even if wedged), or None."""
        state = self.read_state()
        if (
            state is None
            or not self._still_there(state.get("pid"))
            or not _is_our_kernel(state.get("pid"), self.state_path)
        ):
            return None
        try:
            self.hello(state, timeout=timeout)
            return state
        except KernelBusy:
            return state  # alive, merely holding the GIL: adopt, never respawn
        except KernelDown:
            return None

    def _acquire_spawn_lock(self):
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                try:
                    age = time.time() - os.path.getmtime(self.lock_path)
                except OSError:
                    continue
                if age > SPAWN_TIMEOUT:  # reclaim only when stale
                    try:
                        os.unlink(self.lock_path)
                    except OSError:
                        pass
                    continue
                return None  # somebody else is spawning; wait for their kernel
            else:
                os.write(fd, ("%d %f\n" % (os.getpid(), time.time())).encode())
                return fd

    def _adopt_lost_state(self):
        """Adopt a live kernel whose state file vanished instead of spawning a twin.

        The socket is the truth: `hello` returns everything the state file held,
        so the missing file can be rebuilt.  A busy kernel cannot answer, and
        its pid cannot be read from the socket -- then report rather than guess.
        """
        if not os.path.exists(self.sock_path):
            return None
        try:
            reply = self._roundtrip_state({"op": "hello"}, HELLO_TIMEOUT)
        except KernelBusy:
            raise KernelDown(
                "a kernel is already listening on %s but is busy and its state file "
                "is missing, so it cannot be adopted yet; wait for it to go idle, or "
                "stop it with interrupt(force=true)" % self.sock_path
            )
        except KernelDown:
            return None  # stale socket name left by a dead kernel
        pid = reply.get("pid")
        if not pid or not _is_our_kernel(pid, self.state_path):
            # Something is listening but we cannot verify it as ours.  Do not
            # unlink its socket and do not spawn a twin: say so.
            raise KernelDown(
                "a kernel is listening on %s (pid %r) but its command line does "
                "not match this state file; refusing to adopt or replace it"
                % (self.sock_path, pid)
            )
        state = {
            "pid": int(pid),
            "started": reply.get("started") or time.time(),
            "sock": self.sock_path,
            "log": reply.get("log") or self.log_path,
            "version": 1,
        }
        self._write_state(state)
        state["_adopted"] = True
        return state

    def _spawn_locked(self, reason):
        if not self.python:
            raise KernelError(discovery_error())
        mark = time.time()
        deadline = mark + SPAWN_TIMEOUT
        fd = self._acquire_spawn_lock()
        if fd is None:
            state = self._wait_for_foreign_kernel(deadline)
            return state
        try:
            adopted = self._adopt_lost_state()
            if adopted is not None:
                return adopted
            self._unlink_state()  # never adopt a stale state file
            load_or_create_key(self.state_dir)  # before the kernel boots and reads it
            env = os.environ.copy()
            env["PATH"] = os.path.dirname(self.python) + os.pathsep + env.get("PATH", "")
            env.setdefault("SAGE_MCP_STATE", self.state_dir)
            log = open(self.log_path, "ab", buffering=0)
            try:
                log.write(
                    ("%s spawn: %s (python=%s)\n"
                     % (time.strftime("%Y-%m-%d %H:%M:%S"), reason, self.python)).encode()
                )
                os.makedirs(os.path.join(self.state_dir, "tmp"), exist_ok=True)
                self.proc = subprocess.Popen(
                    [
                        self.python,
                        KERNEL_SCRIPT,
                        "--state-file",
                        self.state_path,
                        "--log-file",
                        self.log_path,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    cwd=self.cwd if os.path.isdir(self.cwd) else "/tmp",
                    env=env,
                    start_new_session=True,
                )
                self.spawns += 1
            finally:
                log.close()
            state = self._wait_for_kernel(
                "the Sage kernel did not come up", mark + SPAWN_TIMEOUT, mark, self.proc
            )
            return state
        finally:
            os.close(fd)
            try:
                os.unlink(self.lock_path)
            except OSError:
                pass

    def _wait_for_kernel(self, what, deadline, mark=None, proc=None):
        """Wait for a kernel we can verify (and hello); else KernelDown `what`."""
        while time.time() < deadline:
            if proc is not None:
                code = proc.poll()
                if code is not None:  # a dead launcher is reported at once
                    raise KernelDown(
                        "the Sage kernel launcher exited with code %s after %.1fs\n"
                        "--- %s (last 12 lines) ---\n%s"
                        % (code, time.time() - mark, self.log_path, tail_log(self.log_path))
                    )
            state = self.read_state()
            if (
                state is not None
                and self._still_there(state.get("pid"))
                and _is_our_kernel(state.get("pid"), self.state_path)
                and (
                    proc is None  # another client's kernel: any verified one will do
                    or (
                        float(state.get("started") or 0) >= mark - 5  # a fresh state file
                        and int(state.get("pid") or -1) == proc.pid
                    )
                )
            ):
                try:
                    self.hello(state, timeout=HELLO_TIMEOUT)
                    return state
                except KernelBusy:
                    return state
                except KernelDown:
                    pass
            time.sleep(0.05)
        raise KernelDown(
            "%s within %.0fs\n--- %s (last 12 lines) ---\n%s"
            % (what, SPAWN_TIMEOUT, self.log_path, tail_log(self.log_path))
        )

    def _wait_for_foreign_kernel(self, deadline):
        """Another client holds the spawn lock: adopt the kernel it is starting."""
        return self._wait_for_kernel(
            "another client is starting the kernel (lock: %s) and it did not appear"
            % (self.lock_path,),
            deadline,
        )

    # -- control ----------------------------------------------------------
    def running_job(self, wait=0.6):
        """The job executing right now, as published by the kernel (None if idle).

        Read from a file, not over the socket: the running job may be holding the
        GIL, which is exactly when a client wants to interrupt it.
        """
        path = os.path.join(self.state_dir, RUNNING_NAME)
        deadline = time.time() + max(0.0, wait)
        while True:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    info = json.load(fh)
                if isinstance(info, dict) and info.get("job"):
                    return info
            except (OSError, ValueError):
                pass
            if time.time() >= deadline:
                return None
            time.sleep(0.05)

    def interrupt(self):
        """SIGINT the running job -- but only if it belongs to this client.

        Returns ``(pid, message, refused)``: ``pid`` is None when no signal was
        sent, and ``refused`` says whether that was because the running job
        belongs to another MCP server (a different Pi session or subagent).
        """
        state = self.read_state()
        if state is None:
            return None, "no kernel is running (nothing to interrupt)", False
        pid = int(state["pid"])
        if not self._still_there(pid):
            return None, (
                "no kernel is running (pid %s is gone; the state file at %s is stale)"
                % (pid, self.state_path)
            ), False
        if not _is_our_kernel(pid, self.state_path):
            return None, (
                "no kernel is running (the state file points at pid %s, which is not "
                "our Sage kernel — pid reuse; the stale file was left for `status`)"
                % (pid,)
            ), False
        info = self.running_job()
        if info is None or int(info.get("pid") or 0) != pid:
            return None, (
                "nothing to interrupt: the kernel is idle (no job is running; no "
                "signal was sent)"
            ), False
        owner = info.get("owner")
        if owner is not None and owner != self.client_id:
            return None, (
                "refused: job %s (session %r) is running and was submitted by another "
                "MCP client, so SIGINT was not sent — that job is not yours and "
                "interrupting it would destroy someone else's work. Poll it instead."
                % (info.get("job"), info.get("session"))
            ), True
        os.kill(pid, 2)  # SIGINT: delivered to the main thread, inside the job
        return pid, None, False

    def kill(self):
        """Stop the kernel.  Honest: on failure the state files stay put."""
        state = self.read_state()
        if state is None:
            return status_result(
                ok=True,
                text="no kernel is running (no state file at %s)" % self.state_path,
            )
        pid = int(state["pid"])
        if self.proc is not None and self.proc.pid == pid:
            self.proc.poll()  # reap if it already died
        if not self._still_there(pid):
            self._unlink_state()
            return status_result(
                ok=True,
                text="no kernel is running (pid %s was already gone; stale state file removed)"
                % (pid,),
            )
        if not _is_our_kernel(pid, self.state_path):
            self._unlink_state()
            return status_result(
                ok=True,
                text="no kernel is running (the state file pointed at pid %s, which is "
                "not our Sage kernel — pid reuse; stale state file removed)" % (pid,),
            )
        # Ask nicely first; it may be wedged, so this is best-effort only.
        try:
            self._roundtrip_state({"op": "shutdown"}, 1.5)
        except KernelError:
            pass
        if self._wait_gone(pid, 3.0):
            return self._stopped(pid)
        for sig, budget in ((15, 5.0), (9, 5.0)):  # SIGTERM, then SIGKILL
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                return self._stopped(pid)
            except OSError as exc:
                return status_result(
                    ok=False, text=_stuck_text(pid, "signal %d failed: %s" % (sig, exc))
                )
            if self._wait_gone(pid, budget):
                return self._stopped(pid)
        return status_result(
            ok=False, text=_stuck_text(pid, "SIGTERM and SIGKILL did not stop it")
        )

    def stop_if_last(self, timeout=3.0):
        """Stop the kernel only if no other MCP server still uses it (on exit)."""
        try:
            info = self.op("clients", _timeout=timeout)
        except (KernelDown, KernelBusy, KernelError, OSError):
            return False
        if int(info.get("others") or 0) > 0:
            try:  # stop counting as a client, so the last one out can stop it
                self.op("bye", _timeout=timeout)
            except (KernelDown, KernelBusy, KernelError, OSError):
                pass
            return False
        self.kill()
        return True

    def _stopped(self, pid):
        self._killed_pid = pid
        self._unlink_state()
        return status_result(
            ok=True,
            text="kernel stopped (pid %s); the next exec starts a fresh one — "
            "all sessions and variables are gone" % (pid,),
        )

    def _wait_gone(self, pid, budget):
        end = time.time() + budget
        while time.time() < end:
            if not self._still_there(pid):
                return True
            time.sleep(0.05)
        return not self._still_there(pid)

    def _still_there(self, pid):
        """Alive?  Also reaps our own launcher so it cannot linger as a zombie."""
        if self.proc is not None and self.proc.pid == pid:
            return self.proc.poll() is None
        return _alive(pid)

    def status_snapshot(self, timeout=5.0):
        """Best-effort status: never raises for a missing/wedged kernel."""
        state = self.read_state()
        log = self.log_path
        if state is None:
            snapshot = {"state": "absent", "log": log, "path": self.state_path}
            if self._killed_pid:
                snapshot["note"] = "the previous kernel (pid %s) was force-stopped" % (
                    self._killed_pid,
                )
            return snapshot
        pid = int(state["pid"])
        if not self._still_there(pid):
            return {"state": "dead", "pid": pid, "log": log, "state_file": state}
        if not _is_our_kernel(pid, self.state_path):
            return {"state": "dead", "pid": pid, "log": log,
                    "error": "stale state file (pid reuse)"}
        try:
            reply = self._roundtrip_state({"op": "status"}, timeout)
        except KernelBusy:
            return {"state": "busy", "pid": pid, "started": state.get("started"), "log": log}
        except KernelError as exc:
            return {"state": "unreachable", "pid": pid, "error": str(exc), "log": log}
        reply["state"] = "running"
        return reply


def status_result(ok, text):
    return {"ok": ok, "text": text}

_SINGLETON = None
_SINGLETON_LOCK = threading.Lock()


def get_client():
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = Client()
        return _SINGLETON


def format_uptime(seconds):
    seconds = float(seconds or 0)
    if seconds < 90:
        return "%.0fs" % seconds
    if seconds < 5400:
        return "%.1fm" % (seconds / 60.0)
    return "%.1fh" % (seconds / 3600.0)
