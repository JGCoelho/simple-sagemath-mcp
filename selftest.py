"""selftest.py -- prove that a registered `sage` server really works.

A registration is a pair of paths in someone's config, and the usual way it goes
wrong is silent: the client is pointed at a python that cannot `import sage.all`,
so every `exec` comes back as an error mentioning Singular, which reads as "the
server is broken".  This checks the pair end to end -- spawn the configured
command, initialize, list tools, run `2+2` -- against a throwaway state dir, so
the caller's live kernel is never touched.

    python selftest.py /path/to/python /path/to/sage_mcp.py
    python selftest.py --config ~/.pi/agent/mcp.json      # entry named "sage"
    python -m selftest --config ...                        # same thing

Exit status is 0 only when every check passed.
"""

import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time

TIMEOUT = 90.0
TOOL_HEAD = ("initialize", "serverInfo")
TOOLS = ("exec", "poll", "reset", "drop", "status", "interrupt", "kill", "run_file")


class Failed(Exception):
    pass


class Server:
    def __init__(self, command, args, state_dir, cwd="/tmp"):
        self.state_dir = state_dir
        env = os.environ.copy()
        env["SAGE_MCP_STATE"] = state_dir
        env["SAGE_CWD"] = cwd
        env["PATH"] = os.path.dirname(command) + os.pathsep + env.get("PATH", "")
        self.proc = subprocess.Popen(
            [command] + list(args),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
            bufsize=0,
        )
        self._next_id = 1

    def send(self, payload):
        data = (json.dumps(payload) + "\n").encode("utf-8")
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def read(self, deadline):
        """One JSON-RPC object, or raise Failed on EOF/deadline."""
        buf = b""
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise Failed("no reply from the server (is it hanging?)")
            ready, _, _ = select.select([self.proc.stdout], [], [], remaining)
            if not ready:
                raise Failed("no reply from the server (is it hanging?)")
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                err = b""
                if self.proc.stderr is not None:
                    err = self.proc.stderr.read() or b""
                raise Failed(
                    "the server exited before replying:\n%s"
                    % (err.decode("utf-8", "replace").strip()[-1500:] or "(no stderr)")
                )
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                return json.loads(line.decode("utf-8", "replace"))

    def call(self, method, params=None, timeout=TIMEOUT):
        msg_id = self._next_id
        self._next_id += 1
        self.send(
            {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
        )
        deadline = time.time() + timeout
        while True:
            reply = self.read(deadline)
            if reply.get("id") == msg_id:
                return reply

    def notify(self, method, params=None):
        self.send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except (OSError, AttributeError):
                pass


def _text(reply):
    result = reply.get("result") or {}
    parts = [
        block.get("text", "")
        for block in result.get("content") or []
        if isinstance(block, dict)
    ]
    return "\n".join(parts), bool(result.get("isError"))


def _log_tail(state_dir, lines=6):
    """The end of kernel.log: the launcher's own words about a failed spawn."""
    path = os.path.join(state_dir, "kernel.log")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            tail = fh.read()[-4000:].rstrip().splitlines()
    except OSError:
        return ""
    return "\n".join(tail[-lines:]) if tail else ""


def check(command, args, timeout=TIMEOUT):
    """Run the checks; return a list of (name, ok, detail)."""
    results = []

    def record(name, ok, detail=""):
        results.append((name, bool(ok), detail))

    state_dir = tempfile.mkdtemp(prefix="sage-selftest-")
    server = None
    try:
        try:
            server = Server(command, args, state_dir)
        except OSError as exc:
            record("spawn %s" % command, False, str(exc))
            return results
        record("spawn %s" % " ".join([command] + list(args)), True)

        reply = server.call("initialize", timeout=timeout)
        info = (reply.get("result") or {}).get("serverInfo") or {}
        record("initialize", "error" not in reply, json.dumps(info))

        server.notify("notifications/initialized")
        reply = server.call("tools/list", timeout=timeout)
        names = [
            tool.get("name")
            for tool in (reply.get("result") or {}).get("tools") or []
        ]
        record("tools/list", bool(names), ", ".join(names))
        missing = [name for name in TOOLS if name not in names]
        record("the seven kernel tools are advertised", not missing, ", ".join(missing))

        reply = server.call(
            "tools/call", {"name": "exec", "arguments": {"code": "print(2+2)"}}
        )
        text, is_error = _text(reply)
        detail = (text or "(no text)").splitlines()[0] if text else "(empty)"
        if is_error or "4" not in text:
            tail = _log_tail(state_dir)
            if tail:
                detail += "\nkernel.log:\n" + tail
        record("exec runs Sage code", not is_error and "4" in text, detail)

        reply = server.call("tools/call", {"name": "status", "arguments": {}})
        text, is_error = _text(reply)
        record("status", not is_error and "kernel: running" in text, text.splitlines()[0])

        reply = server.call("tools/call", {"name": "kill", "arguments": {}})
        text, is_error = _text(reply)
        record("kill", not is_error, text.splitlines()[0] if text else "")
        return results
    except Failed as exc:
        record("the server answered the handshake", False, str(exc))
        return results
    except (OSError, ValueError, KeyError) as exc:
        record("the server answered the handshake", False, "%s: %s" % (type(exc).__name__, exc))
        return results
    finally:
        if server is not None:
            server.close()
        shutil.rmtree(state_dir, ignore_errors=True)


def entry_from_config(path, name="sage"):
    """(command, args) for `name` in an mcp.json-style file."""
    with open(path) as fh:
        data = json.load(fh)
    servers = data.get("mcpServers") or data.get("servers") or {}
    entry = servers.get(name)
    if not isinstance(entry, dict):
        raise SystemExit("error: no %r entry in %s" % (name, path))
    command = entry.get("command")
    if not isinstance(command, str) or not command:
        raise SystemExit("error: entry %r has no command" % name)
    args = [str(arg) for arg in entry.get("args") or []]
    return command, args


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    command = None
    args = []
    timeout = TIMEOUT
    while argv:
        flag = argv.pop(0)
        if flag in ("-h", "--help"):
            print(__doc__.strip())
            return 0
        if flag == "--config":
            command, args = entry_from_config(argv.pop(0))
        elif flag == "--name":
            name = argv.pop(0)
            command, args = entry_from_config(argv.pop(0), name)
        elif flag == "--timeout":
            timeout = float(argv.pop(0))
        elif flag.startswith("-"):
            raise SystemExit("error: unknown option %s" % flag)
        elif command is None:
            command = flag
            if argv and not argv[0].startswith("-"):
                args = [argv.pop(0)]
        else:
            args.append(flag)
    if command is None:
        print(__doc__.strip())
        return 2

    results = check(command, args, timeout=timeout)
    for name, ok, detail in results:
        print("[%s] %s" % ("PASS" if ok else "FAIL", name))
        if detail:
            for line in str(detail).rstrip().splitlines():
                print("       " + line)
    failed = [name for name, ok, _ in results if not ok]
    print()
    print("%d checks, %d failed" % (len(results), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
