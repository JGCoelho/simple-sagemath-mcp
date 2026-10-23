"""runfile.py -- the `run_file` tool: execute a file that already exists.

The agent writes scripts with its own tools (bash, an editor, a heredoc); this
module just runs one of them and hands the output back inline.  Nothing is
copied, stored or indexed -- the file you named is the file that ran.

The interpreter is the point: `python` is the Sage interpreter itself (so
`from sage.all import *` works) and `lang="sage"` shells out to the `sage`
command next to it, which the agent's shell may not have on its PATH.
"""

import os
import shutil
import signal
import subprocess
import tempfile
import time

import sage_client

DEFAULT_TIMEOUT = 120.0
MAX_TIMEOUT = 3600.0
KILL_GRACE = 2.0
INLINE_LIMIT = 20000  # characters of each stream returned to the model

FROM_EXTENSION = {".sh": "bash", ".bash": "bash", ".py": "python", ".sage": "sage"}
LANGUAGES = ("bash", "python", "sage", "exec")


class RunError(Exception):
    pass


def sage_python():
    """Sage's own interpreter -- the one that can `import sage.all`.

    Not `sys.executable`: the MCP server may run on a plain Python with only
    `mcp` installed (PLAN §1.2), while these children need Sage.
    """
    try:
        return sage_client.sage_python()
    except sage_client.KernelError as exc:
        raise RunError(str(exc))


def sage_binary():
    candidate = os.path.join(os.path.dirname(sage_python()), "sage")
    if os.access(candidate, os.X_OK):
        return candidate
    return shutil.which("sage")


def _shebang(path):
    try:
        with open(path, "rb") as fh:
            first = fh.readline(200)
    except OSError:
        return False
    return first.startswith(b"#!")


def resolve_lang(lang, path):
    lang = (lang or "auto").strip().lower()
    if lang != "auto":
        if lang not in LANGUAGES:
            raise RunError(
                "unknown lang %r (use bash, python, sage, exec or auto)" % (lang,)
            )
        return lang
    guess = FROM_EXTENSION.get(os.path.splitext(path)[1].lower())
    if guess:
        return guess
    if os.access(path, os.X_OK) and _shebang(path):
        return "exec"  # honour the file's own #!
    raise RunError(
        "%s: cannot tell how to run this file; pass lang=bash|python|sage|exec"
        % (os.path.basename(path),)
    )


def _command(lang, path):
    if lang == "bash":
        return [shutil.which("bash") or "/bin/bash", path]
    if lang == "python":
        return [sage_python(), path]
    if lang == "sage":
        binary = sage_binary()
        if not binary:
            raise RunError(
                "lang='sage' needs the `sage` command next to this Python (%s); "
                "use lang='python' and `from sage.all import *` instead"
                % (os.path.dirname(sage_python()),)
            )
        return [binary, path]
    return [path]  # exec: the file's own shebang decides


def _child_env():
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["NO_COLOR"] = "1"
    env.setdefault("TERM", "dumb")
    # Sage's CLI shells out to Singular and friends, which live beside it.
    python = sage_client.resolve_python()  # None: Sage missing, e.g. for lang=bash
    env["PATH"] = os.pathsep.join(
        p
        for p in (
            env.get("PATH", "/usr/bin:/bin"),
            os.path.dirname(python) if python else "",
        )
        if p
    )
    return env


def _read_capped(fh):
    fh.seek(0)
    data = fh.read(INLINE_LIMIT + 1).decode("utf-8", "replace")
    if len(data) > INLINE_LIMIT:
        size = os.fstat(fh.fileno()).st_size
        data = data[:INLINE_LIMIT] + "\n[... %d more bytes ...]" % (size - INLINE_LIMIT)
    return data


def run(path, lang="auto", args=None, timeout_s=None, stdin=None, cwd=None):
    """Run `path` once and return its output as text (raises on failure)."""
    if not path:
        raise RunError("run_file needs path=<file to run>")
    source = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.isfile(source):
        raise RunError("no such file: %s" % (source,))
    lang = resolve_lang(lang, source)
    try:
        timeout = float(DEFAULT_TIMEOUT if timeout_s is None else timeout_s)
    except (TypeError, ValueError):
        raise RunError("timeout_s must be a number of seconds")
    timeout = min(max(timeout, 0.2), MAX_TIMEOUT)
    argv_extra = [str(a) for a in (args or [])]
    workdir = os.path.abspath(os.path.expanduser(str(cwd))) if cwd else os.path.dirname(source)
    if not os.path.isdir(workdir):
        raise RunError("no such directory: %s" % (workdir,))

    command = _command(lang, source) + argv_extra
    stdin_data = None if stdin is None or str(stdin) == "" else str(stdin).encode()
    out_fh = tempfile.TemporaryFile()
    err_fh = tempfile.TemporaryFile()
    started = time.time()
    timed_out = False
    try:
        try:
            proc = subprocess.Popen(
                command,
                cwd=workdir,
                env=_child_env(),
                stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
                stdout=out_fh,
                stderr=err_fh,
                start_new_session=True,  # one process group per run: the timeout kills all of it
            )
        except OSError as exc:
            raise RunError("could not start %s: %s" % (command[0], exc))
        try:
            proc.communicate(stdin_data, timeout=timeout)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except OSError:
                    pass
                try:
                    exit_code = proc.wait(timeout=KILL_GRACE)
                    break
                except subprocess.TimeoutExpired:
                    continue
            else:  # pragma: no cover - SIGKILL is final
                exit_code = None
        duration = time.time() - started
        stdout = _read_capped(out_fh)
        stderr = _read_capped(err_fh)
    finally:
        out_fh.close()
        err_fh.close()

    head = "%s  %s  %.2fs  %s" % (
        "timeout after %gs" % (timeout,) if timed_out else "exit=%s" % (exit_code,),
        os.path.basename(source),
        duration,
        " ".join([os.path.basename(command[0])] + [os.path.basename(source)] + argv_extra),
    )
    parts = [head, "cwd=%s  lang=%s" % (workdir, lang), "--- stdout ---", stdout]
    if stderr.strip():
        parts += ["--- stderr ---", stderr]
    text = "\n".join(parts).rstrip("\n") + "\n"
    if timed_out:
        raise RunError(
            text
            + "[the whole process group was killed; a slow job does not belong in "
            "run_file -- raise timeout_s, or use the kernel's exec/poll]"
        )
    if exit_code != 0:
        raise RunError(text)
    return text
