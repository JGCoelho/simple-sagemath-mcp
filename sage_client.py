#!/usr/bin/env python3
"""sage_client.py -- client side of sage-mcp: find, spawn, talk to the kernel.

Non-obvious rules encoded here (each one was a bug):

* A **busy** kernel is not a dead kernel.  If the TCP port accepts a
  connection but ``hello``/the op times out, the kernel is alive and merely
  holding the GIL inside a computation: raise :class:`KernelBusy` and NEVER
  respawn (that would orphan a live kernel and start a second one on a new
  port).
* Spawning is serialised with ``O_CREAT|O_EXCL`` on ``spawn.lock``; a second
  client waits for the first one's kernel.  The lock is reclaimed only when its
  mtime is older than the wait budget.
* Only a port file written *after* this spawn began is trusted
  (``started >= spawn_mark - 5`` plus a pid match).
* A dead launcher is reported immediately (``proc.poll()``) together with the
  last ~12 lines of ``kernel.log``, not as a generic timeout.
* Every response read is capped at 64 MB.
"""

import errno
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid

MAX_RESPONSE = 64 * 1024 * 1024  # rule 19
SPAWN_TIMEOUT = 180.0  # rule 14: how long we wait for a launcher
SPAWN_LOCK = "spawn.lock"
STATE_NAME = "kernel.json"
RUNNING_NAME = "running.json"  # written by the kernel: the job executing now
LOG_NAME = "kernel.log"
HELLO_TIMEOUT = 5.0
OP_TIMEOUT = 30.0
CONNECT_TIMEOUT = 3.0
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


def _sage_ish(path):
    """True when a `sage` executable sits next to this interpreter.

    The sage environment's bin dir must hold both: sage there is what makes
    `import sage.all` work at all (Singular and friends live in the same dir).
    """
    return os.access(os.path.join(os.path.dirname(path), "sage"), os.X_OK)


def resolve_python():
    """Interpreter that can `import sage.all`, or the one we are running on.

    Order: $SAGE_PYTHON, then our own interpreter, then the python beside the
    `sage` found on PATH.  Nothing machine-specific is baked in: an install that
    is not the sage environment fails loudly at spawn time (the launcher's log
    names the missing import), and $SAGE_PYTHON fixes it.
    """
    explicit = os.environ.get("SAGE_PYTHON")
    if explicit and os.path.exists(explicit):
        return os.path.abspath(explicit)
    candidates = [sys.executable]
    sage_bin = shutil.which("sage")
    if sage_bin:
        # prefix layouts (conda, source builds) call it `python`; distro
        # packages ship `python3` only, next to the same `sage`
        beside = os.path.dirname(os.path.realpath(sage_bin))
        candidates.append(os.path.join(beside, "python"))
        candidates.append(os.path.join(beside, "python3"))
    for candidate in candidates:
        if candidate and os.path.exists(candidate) and _sage_ish(candidate):
            return os.path.abspath(candidate)
    return sys.executable


def _alive(pid):
    """True only for a *live* process: a zombie is a dead process to us."""
    if not pid:
        return False
    try:
        pid = int(pid)
    except (ValueError, TypeError, OverflowError):
        return False
    try:
        with open("/proc/%d/stat" % pid, "r") as fh:
            data = fh.read()
        fields = data.rsplit(")", 1)[-1].split()
        if fields and fields[0] in ("Z", "X", "x"):
            return False
        return True
    except FileNotFoundError:
        return False
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
    """Guard against pid reuse: a stale port file must not point at a stranger."""
    try:
        with open("/proc/%d/cmdline" % int(pid), "rb") as fh:
            cmd = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return False
    return "sage_kernel.py" in cmd and os.path.basename(state_path) in cmd


class Client:
    """One process-wide handle on the kernel; safe to share across threads."""

    def __init__(self, state_dir=None):
        self.state_dir = os.path.abspath(state_dir or default_state_dir())
        os.makedirs(self.state_dir, exist_ok=True)
        self.state_path = os.path.join(self.state_dir, STATE_NAME)
        self.lock_path = os.path.join(self.state_dir, SPAWN_LOCK)
        self.log_path = os.path.join(self.state_dir, LOG_NAME)
        self.python = resolve_python()
        self.cwd = os.environ.get("SAGE_CWD") or "/tmp"
        self.proc = None  # our own launcher, if we ever spawned one
        self._spawn_lock = threading.Lock()
        # Identifies this MCP server to the kernel: several servers may share one
        # kernel, and each one must be able to tell whether it is the last user.
        self.client_id = uuid.uuid4().hex
        self._ever_ready = False  # did this server hand out a kernel already?
        self._killed_pid = None  # pid stopped by kill(), for the restart notice

    # -- state file -------------------------------------------------------
    def read_state(self):
        try:
            with open(self.state_path, "r") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or "port" not in data or "pid" not in data:
            return None
        return data

    def _unlink_state(self):
        for path in (self.state_path, os.path.join(self.state_dir, RUNNING_NAME)):
            try:
                os.unlink(path)
            except OSError:
                pass

    # -- transport --------------------------------------------------------
    def _roundtrip(self, payload, timeout):
        state = self.read_state()
        if state is None:
            raise KernelDown("no kernel state file at %s" % self.state_path)
        return self._roundtrip_state(state, payload, timeout)

    def _roundtrip_state(self, state, payload, timeout):
        if isinstance(payload, dict):
            payload.setdefault("client", self.client_id)
        try:
            sock = socket.create_connection(
                ("127.0.0.1", int(state["port"])), timeout=CONNECT_TIMEOUT
            )
        except OSError as exc:
            raise KernelDown(
                "cannot connect to kernel pid %s on port %s: %s"
                % (state.get("pid"), state.get("port"), exc)
            )
        try:
            sock.settimeout(timeout)
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
            try:
                sock.sendall(data)
            except OSError as exc:
                raise KernelDown("lost the connection to the kernel: %s" % (exc,))
            line = self._recv_line(sock, timeout)
            if line is None:
                raise KernelBusy(
                    "kernel accepted the connection but closed it without answering"
                )
        finally:
            try:
                sock.close()
            except OSError:
                pass
        try:
            reply = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            raise KernelDown("kernel sent a non-JSON reply: %r" % (line[:200],))
        return reply

    def _recv_line(self, sock, timeout):
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise KernelBusy(
                    "the kernel accepted the connection but did not answer within "
                    "%.1fs (a computation is probably holding the GIL; it is alive, "
                    "not dead)" % timeout
                )
            try:
                sock.settimeout(remaining)
                chunk = sock.recv(65536)
            except socket.timeout:
                raise KernelBusy(
                    "the kernel accepted the connection but did not answer within "
                    "%.1fs (a computation is probably holding the GIL; it is alive, "
                    "not dead)" % timeout
                )
            except OSError as exc:
                raise KernelDown("error while reading from the kernel: %s" % (exc,))
            if not chunk:
                return None
            buf += chunk
            if len(buf) > MAX_RESPONSE:
                raise KernelError(
                    "kernel reply exceeded the 64 MB client cap (got %d bytes); "
                    "reduce the output (e.g. truncate reprs) and retry" % len(buf)
                )
            idx = buf.find(b"\n")
            if idx >= 0:
                return bytes(buf[:idx])

    # -- lifecycle --------------------------------------------------------
    def hello(self, state=None, timeout=HELLO_TIMEOUT):
        state = state or self.read_state()
        if state is None:
            raise KernelDown("no kernel state file at %s" % self.state_path)
        reply = self._roundtrip_state(state, {"op": "hello"}, timeout)
        if not reply.get("ok"):
            raise KernelDown("hello failed: %s" % (reply.get("error"),))
        return reply

    def op(self, name, _timeout=OP_TIMEOUT, **params):
        """Send an op; ``_timeout`` is the socket budget, params go to the kernel."""
        payload = {"op": name}
        payload.update(params)
        reply = self._roundtrip(payload, _timeout)
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
                            "(%s); refusing to start a second kernel — use `kill`"
                            % (pid, exc)
                        )
                    reason = "the previous kernel (pid %s) stopped answering" % (pid,)
            if self.proc is not None and self.proc.poll() is None and self.proc.pid == pid:
                raise KernelDown("our own kernel (pid %s) is alive but unresponsive" % pid)
        else:
            killed = self._killed_pid
            self._killed_pid = None
            if killed:
                reason = "the previous kernel (pid %s) was force-stopped with interrupt(force=true)" % (killed,)
            else:
                reason = "no kernel was running"
        state = self._spawn(reason)
        self._ever_ready = True
        return state, was_ready, reason

    def _spawn(self, reason):
        with self._spawn_lock:
            # Re-check: another thread may have spawned while we waited.  Only a
            # state we can *verify* as our kernel may be adopted (rule 13).
            state = self.read_state()
            if (
                state is not None
                and self._still_there(state.get("pid"))
                and _is_our_kernel(state.get("pid"), self.state_path)
            ):
                try:
                    self.hello(state, timeout=2.0)
                    return state
                except KernelBusy:
                    return state
                except KernelDown:
                    pass
            return self._spawn_locked(reason)

    def _acquire_spawn_lock(self, deadline):
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                try:
                    age = time.time() - os.path.getmtime(self.lock_path)
                except OSError:
                    continue
                if age > SPAWN_TIMEOUT:  # rule 12: reclaim only when stale
                    try:
                        os.unlink(self.lock_path)
                    except OSError:
                        pass
                    continue
                return None  # somebody else is spawning; wait for their kernel
            else:
                os.write(fd, ("%d %f\n" % (os.getpid(), time.time())).encode())
                return fd
            if time.time() > deadline:
                raise KernelDown(
                    "timed out waiting for the spawn lock at %s" % self.lock_path
                )

    def _spawn_locked(self, reason):
        mark = time.time()
        deadline = mark + SPAWN_TIMEOUT
        fd = self._acquire_spawn_lock(deadline)
        if fd is None:
            state = self._wait_for_foreign_kernel(deadline)
            return state
        try:
            self._unlink_state()  # rule 13: never adopt a stale port file
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
            finally:
                log.close()
            state = self._wait_for_our_kernel(mark, self.proc)
            self._recent_restart = reason
            return state
        finally:
            os.close(fd)
            try:
                os.unlink(self.lock_path)
            except OSError:
                pass

    def _wait_for_our_kernel(self, mark, proc):
        deadline = time.time() + SPAWN_TIMEOUT
        while time.time() < deadline:
            rc = proc.poll()
            if rc is not None:  # rule 14: report a dead launcher immediately
                raise KernelDown(
                    "the Sage kernel launcher exited with code %s after %.1fs\n"
                    "--- %s (last 12 lines) ---\n%s"
                    % (rc, time.time() - mark, self.log_path, tail_log(self.log_path))
                )
            state = self.read_state()
            if (
                state is not None
                and float(state.get("started") or 0) >= mark - 5  # rule 13
                and int(state.get("pid") or -1) == proc.pid
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
            "the Sage kernel did not come up within %.0fs\n"
            "--- %s (last 12 lines) ---\n%s"
            % (SPAWN_TIMEOUT, self.log_path, tail_log(self.log_path))
        )

    def _wait_for_foreign_kernel(self, deadline):
        """Another client holds the spawn lock: adopt the kernel it is starting."""
        while time.time() < deadline:
            state = self.read_state()
            if (
                state is not None
                and self._still_there(state.get("pid"))
                and _is_our_kernel(state.get("pid"), self.state_path)
            ):
                try:
                    self.hello(state, timeout=2.0)
                    return state
                except KernelBusy:
                    return state
                except KernelDown:
                    pass
            time.sleep(0.1)
        raise KernelDown(
            "another client is starting the kernel and it did not appear within "
            "%.0fs (lock: %s)\n--- %s (last 12 lines) ---\n%s"
            % (SPAWN_TIMEOUT, self.lock_path, self.log_path, tail_log(self.log_path))
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
            self._roundtrip_state(state, {"op": "shutdown"}, 1.5)
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
                # Rule 17: keep the state files; they point at a live process.
                return status_result(
                    ok=False,
                    text="could not stop the kernel: signal %d to pid %s failed (%s).\n"
                    "The state files were left in place on purpose: pid %s may still be "
                    "alive, and deleting them would orphan it and let the next call "
                    "spawn a second kernel.\nStop it yourself with: kill -9 %s"
                    % (sig, pid, exc, pid, pid),
                )
            if self._wait_gone(pid, budget):
                return self._stopped(pid)
        return status_result(
            ok=False,
            text="could not stop the kernel: pid %s is still alive after SIGTERM and "
            "SIGKILL.\nThe state files were left in place on purpose (deleting them "
            "would orphan a live kernel and let the next call spawn a second one).\n"
            "Stop it yourself with: kill -9 %s" % (pid, pid),
        )

    def stop_if_last(self, timeout=3.0):
        """Stop the kernel only if no other MCP server is still using it.

        Called when this server exits. Returns True when the kernel was stopped,
        False when it was left running for somebody else (or was unreachable).
        """
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
        if state is None:
            snapshot = {"state": "absent", "log": self.log_path, "path": self.state_path}
            if self._killed_pid:
                snapshot["note"] = (
                    "the previous kernel (pid %s) was force-stopped"
                    % (self._killed_pid,)
                )
            return snapshot
        pid = int(state["pid"])
        if not self._still_there(pid):
            return {"state": "dead", "pid": pid, "log": self.log_path, "state_file": state}
        if not _is_our_kernel(pid, self.state_path):
            return {
                "state": "dead",
                "pid": pid,
                "log": self.log_path,
                "error": "stale state file (pid reuse)",
            }
        try:
            reply = self._roundtrip_state(state, {"op": "status"}, timeout)
        except KernelBusy:
            return {
                "state": "busy",
                "pid": pid,
                "started": state.get("started"),
                "log": self.log_path,
            }
        except KernelError as exc:
            return {
                "state": "unreachable",
                "pid": pid,
                "error": str(exc),
                "log": self.log_path,
            }
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
