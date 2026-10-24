"""subproc.py -- the one subprocess core behind `run_file` and `notebook`.

Everything that starts a child process for a tool goes through `_run`: one
process group, a timeout that kills the whole group, and output captured to
*files* (never a pipe: a Jupyter kernel can outlive nbconvert and would keep
the pipe open, making the call look hung).  The bytes handed back inline are
capped.

`lang=python` is **Sage's** interpreter, not `sys.executable`: the server may
run on a plain Python that has `mcp` but no Sage (DESIGN.md, two interpreters).
The same rule holds for `notebook`: `available()` asks *Sage's* interpreter, and
all four actions run in it (this very file, executed by that interpreter), so the
server imports neither `nbformat` nor `nbconvert` -- and the tool is advertised
exactly when the interpreter that will run it can do the work.
"""

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import limits
import sage_client

INLINE_LIMIT = 20000  # chars of each stream handed back to the model
KILL_GRACE = 2.0
MAX_TIMEOUT = 3600.0
FROM_EXTENSION = {".sh": "bash", ".bash": "bash", ".py": "python", ".sage": "sage"}
LANGUAGES = ("bash", "python", "sage", "exec")


class SubprocessError(Exception):
    """A child could not be started or failed; the tool reports this text."""


def sage_python():
    """Sage's own interpreter -- the one that can `import sage.all`."""
    try:
        return sage_client.sage_python()
    except sage_client.KernelError as exc:
        raise SubprocessError(str(exc))


def sage_binary():
    candidate = os.path.join(os.path.dirname(sage_python()), "sage")
    return candidate if os.access(candidate, os.X_OK) else shutil.which("sage")


def _limits_config():
    try:
        return limits.parse()
    except limits.LimitError:
        return {}


def _child_env():
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["NO_COLOR"] = "1"
    env.setdefault("TERM", "dumb")
    python = sage_client.resolve_python()  # None: Sage missing, e.g. for lang=bash
    if python:  # Singular and friends live beside Sage's interpreter
        env["PATH"] = os.pathsep.join([env.get("PATH", ""), os.path.dirname(python)])
    return env


def _cap(fh):
    fh.seek(0)
    data = fh.read(INLINE_LIMIT + 1).decode("utf-8", "replace")
    if len(data) > INLINE_LIMIT:
        size = os.fstat(fh.fileno()).st_size
        data = data[:INLINE_LIMIT] + "\n[... %d more bytes ...]" % (size - INLINE_LIMIT)
    return data


def _tail(path, lines=12):
    try:
        with open(path, "r", errors="replace") as fh:
            rows = [row for row in fh.read()[-200000:].splitlines() if row.strip()]
    except OSError:
        return "(no log)"
    return "\n".join(rows[-lines:]) if rows else "(log is empty)"


def _run(command, cwd, stdin_bytes, timeout, log_path=None):
    """Run `command`; return (exit_code, stdout, stderr, timed_out, seconds).

    With `log_path` both streams go to that file (nbconvert) and the returned
    streams are empty; otherwise they are captured in temp files and capped.
    """
    try:  # children inherit it: re-armed because the per-UID count drifts
        limits.arm_nproc(_limits_config())
    except limits.LimitError:
        pass
    out_fh = tempfile.TemporaryFile()
    err_fh = tempfile.TemporaryFile()
    sink = open(log_path, "ab") if log_path else None
    started = time.time()
    try:
        try:
            proc = subprocess.Popen(
                command,
                cwd=cwd,
                env=_child_env(),
                stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
                stdout=sink or out_fh,
                stderr=sink or err_fh,
                start_new_session=True,  # one process group per run: kill all of it
            )
        except OSError as exc:
            raise SubprocessError("could not start %s: %s" % (command[0], exc))
        timed_out = False
        try:
            proc.communicate(stdin_bytes, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except OSError:
                    pass
                try:
                    proc.wait(timeout=KILL_GRACE)
                    break
                except subprocess.TimeoutExpired:
                    continue
        duration = time.time() - started
        if sink is not None:
            return proc.returncode, "", "", timed_out, duration
        return proc.returncode, _cap(out_fh), _cap(err_fh), timed_out, duration
    finally:
        out_fh.close()
        err_fh.close()
        if sink is not None:
            sink.close()

# run_file


def _shebang(path):
    try:
        with open(path, "rb") as fh:
            return fh.readline(200).startswith(b"#!")
    except OSError:
        return False


def _resolve_lang(lang, path):
    lang = (lang or "auto").strip().lower()
    if lang != "auto":
        if lang not in LANGUAGES:
            raise SubprocessError(
                "unknown lang %r (use bash, python, sage, exec or auto)" % (lang,)
            )
        return lang
    guess = FROM_EXTENSION.get(os.path.splitext(path)[1].lower())
    if guess:
        return guess
    if os.access(path, os.X_OK) and _shebang(path):
        return "exec"  # honour the file's own #!
    raise SubprocessError(
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
            raise SubprocessError(
                "lang='sage' needs the `sage` command next to this Python (%s); "
                "use lang='python' and `from sage.all import *` instead"
                % (os.path.dirname(sage_python()),)
            )
        return [binary, path]
    return [path]  # exec: the file's own shebang decides


def run_file(path, lang="auto", args=None, timeout_s=None, stdin=None, cwd=None):
    """Run `path` once and return its output as text (raises on failure)."""
    if not path:
        raise SubprocessError("run_file needs path=<file to run>")
    source = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.isfile(source):
        raise SubprocessError("no such file: %s" % (source,))
    lang = _resolve_lang(lang, source)
    try:
        timeout = float(120.0 if timeout_s is None else timeout_s)
    except (TypeError, ValueError):
        raise SubprocessError("timeout_s must be a number of seconds")
    timeout = min(max(timeout, 0.2), MAX_TIMEOUT)
    argv_extra = [str(a) for a in (args or [])]
    workdir = (
        os.path.abspath(os.path.expanduser(str(cwd))) if cwd else os.path.dirname(source)
    )
    if not os.path.isdir(workdir):
        raise SubprocessError("no such directory: %s" % (workdir,))
    command = _command(lang, source) + argv_extra
    stdin_data = None if stdin is None or str(stdin) == "" else str(stdin).encode()
    exit_code, stdout, stderr, timed_out, duration = _run(
        command, workdir, stdin_data, timeout
    )
    head = "%s  %s  %.2fs  %s" % (
        "timeout after %gs" % (timeout,) if timed_out else "exit=%s" % (exit_code,),
        os.path.basename(source),
        duration,
        " ".join([os.path.basename(command[0]), os.path.basename(source)] + argv_extra),
    )
    parts = [head, "cwd=%s  lang=%s" % (workdir, lang), "--- stdout ---", stdout]
    if stderr.strip():
        parts += ["--- stderr ---", stderr]
    text = "\n".join(parts).rstrip("\n") + "\n"
    if timed_out:
        raise SubprocessError(
            text
            + "[the whole process group was killed; a slow job does not belong in "
            "run_file -- raise timeout_s, or use the kernel's exec/poll]"
        )
    if exit_code != 0:
        raise SubprocessError(text)
    return text

# notebook: *one* interpreter decides and executes
#
# `available()` probes Sage's python and the four actions run there too, as this
# file executed by that interpreter.  The server therefore imports nothing but
# `mcp`: with the nbformat import in the server process, a documented install
# (plain Python + `mcp` next to Sage) advertised the tool and every action died
# with the SDK's message-less "Error executing tool notebook".
NOTEBOOK_ACTIONS = ("check", "extract", "clear", "run")
_AVAILABLE = []  # cached: one probe per server process


def available():
    """True when *Sage's* interpreter can read and execute notebooks.

    Asking our own sys.path would answer for the wrong interpreter: the server
    may run on a plain Python with only `mcp` installed.  The probe asks for
    exactly what the child imports, so "advertised" and "runnable" cannot
    disagree.
    """
    if not _AVAILABLE:
        ok = False
        python = sage_client.resolve_python()
        if python:
            probe = (
                "import importlib.util as u; raise SystemExit(0 if all(u.find_spec(m)"
                " for m in ('nbformat', 'nbconvert', 'nbclient')) else 1)"
            )
            try:
                ok = (
                    subprocess.run(
                        [python, "-c", probe],
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        timeout=120,
                    ).returncode
                    == 0
                )
            except (OSError, subprocess.SubprocessError):
                ok = False
        _AVAILABLE.append(ok)
    return _AVAILABLE[0]


def notebook(
    action, path, out=None, in_place=False, cell_timeout_s=120, timeout_s=None,
    state_dir=None,
):
    """Run one notebook action in Sage's interpreter; return its text.

    `run` executes the notebook with nbconvert (atomically publishing
    `<name>.out.ipynb`), always as a child: a Jupyter kernel must never run in
    the server.  The child enforces the real budget (per cell plus slack) and
    kills its own nbconvert, so `budget` here is only a backstop for a wedged
    child.
    """
    if not path:
        raise SubprocessError("notebook needs a path to an .ipynb file")
    action = (action or "").strip().lower()
    if action not in NOTEBOOK_ACTIONS:
        raise SubprocessError(
            "action must be one of %s (got %r)" % (", ".join(NOTEBOOK_ACTIONS), action)
        )
    if not available():
        raise SubprocessError(
            "notebook needs nbformat, nbconvert and nbclient in Sage's interpreter "
            "(%s); install them there and restart the server"
            % (sage_client.resolve_python() or "Sage not found",)
        )
    argv = [
        sage_python(),
        os.path.abspath(__file__),
        "--notebook",
        action,
        "--path",
        os.path.abspath(os.path.expanduser(str(path))),
    ]
    if out:
        argv += ["--out", os.path.abspath(os.path.expanduser(str(out)))]
    if in_place:
        argv.append("--in-place")
    argv += ["--cell-timeout", "%g" % float(cell_timeout_s)]
    if timeout_s:
        argv += ["--timeout", "%g" % float(timeout_s)]
    if state_dir:
        argv += ["--state-dir", str(state_dir)]
    budget = min(max(float(timeout_s or MAX_TIMEOUT) + 60.0, 60.0), MAX_TIMEOUT)
    exit_code, stdout, stderr, timed_out, _ = _run(
        argv,
        os.path.abspath(os.environ.get("SAGE_CWD") or os.getcwd()),
        None,
        budget,
    )
    if timed_out:
        raise SubprocessError(
            "notebook %s did not finish within %.0fs; the whole process group was "
            "killed.\n%s" % (action, budget, stderr.strip())
        )
    if exit_code != 0:
        raise SubprocessError(
            stderr.strip()
            or stdout.strip()
            or "notebook %s failed (exit %s)" % (action, exit_code)
        )
    return stdout.rstrip("\n")


# -- the child: this file, executed by Sage's interpreter ------------------
def _read(path):
    import nbformat

    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        raise SubprocessError("no such notebook: %s" % path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return path, nbformat.read(fh, as_version=4)
    except Exception as exc:
        raise SubprocessError("cannot read %s as a notebook: %s" % (path, exc))


def _write(nb, dest):
    import nbformat

    dest = os.path.abspath(dest)
    tmp = os.path.join(
        os.path.dirname(dest) or ".",
        ".%s.sage-mcp-%d.tmp" % (os.path.basename(dest), os.getpid()),
    )
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            nbformat.write(nb, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return dest


def _kernel_name(nb):
    spec = (nb.get("metadata") or {}).get("kernelspec") or {}
    return spec.get("name") or "sagemath"


def _code_cells(nb):
    return [cell for cell in nb.cells if cell.cell_type == "code"]


def _check(path):
    import nbformat

    path, nb = _read(path)
    try:
        nbformat.validate(nb)
        validity = ""
    except Exception as exc:
        first = str(exc).strip().splitlines()
        validity = "  [validation: %s]" % (first[0] if first else "invalid")
    code = _code_cells(nb)
    return (
        "OK   %s v%d.%d kernel=%s cells=%d (code=%d, with-output=%d)%s"
        % (
            path,
            nb.nbformat,
            nb.nbformat_minor,
            _kernel_name(nb),
            len(nb.cells),
            len(code),
            len([cell for cell in code if cell.get("outputs")]),
            validity,
        )
    )


def _extract(path):
    path, nb = _read(path)
    code = _code_cells(nb)
    lines = ["# extracted from %s (%d code cells)" % (path, len(code))]
    for index, cell in enumerate(code, 1):
        lines += ["", "# --- cell %d (code) ---" % index, cell.source.rstrip("\n")]
    return "\n".join(lines)


def _clear(path, out=None, in_place=False):
    path, nb = _read(path)
    cleared = 0
    for cell in _code_cells(nb):
        if cell.get("outputs") or cell.get("execution_count") is not None:
            cleared += 1
        cell["outputs"] = []
        cell["execution_count"] = None
    dest = path if (in_place or not out) else os.path.abspath(os.path.expanduser(out))
    _write(nb, dest)
    return "cleared outputs in %d code cells%s" % (
        cleared,
        "" if dest == path else " -> %s" % dest,
    )


def _run_notebook(
    path, out=None, in_place=False, cell_timeout_s=120, timeout_s=None, state_dir=None
):
    """Execute a notebook with nbconvert, atomically publishing `<name>.out.ipynb`."""
    path, nb = _read(path)
    if in_place:
        dest = path
    elif out:
        dest = os.path.abspath(os.path.expanduser(out))
    else:
        dest = (path[: -len(".ipynb")] if path.endswith(".ipynb") else path) + ".out.ipynb"
    destdir = os.path.dirname(dest) or "."
    if not os.path.isdir(destdir):
        raise SubprocessError("output directory does not exist: %s" % destdir)
    code_cells = _code_cells(nb)
    if timeout_s:
        budget = float(timeout_s)
    else:  # per-cell budget plus slack, so the child's own timeout reports first
        budget = float(cell_timeout_s) * (len(code_cells) + 1) + 60.0
    log_path = os.path.join(
        state_dir or os.environ.get("SAGE_MCP_STATE") or "/tmp",
        "nbconvert-%d-%d.log" % (os.getpid(), int(time.time())),
    )
    base = os.path.basename(dest)
    tmp_base = ".%s.sage-mcp-%d" % (
        base[: -len(".ipynb")] if base.endswith(".ipynb") else base,
        os.getpid(),
    )
    produced = os.path.join(destdir, tmp_base + ".ipynb")
    command = [
        sage_python(),
        "-m",
        "nbconvert",
        "--to",
        "notebook",
        "--execute",
        "--ExecutePreprocessor.timeout=%d" % int(cell_timeout_s),
        "--ExecutePreprocessor.allow_errors=True",
        "--ExecutePreprocessor.kernel_name=%s" % _kernel_name(nb),
        "--output",
        tmp_base,
        "--output-dir",
        destdir,
        path,
    ]
    exit_code, _, _, timed_out, _ = _run(
        command, os.path.dirname(path) or ".", None, budget, log_path=log_path
    )
    if timed_out:
        raise SubprocessError(
            "notebook execution timed out after %.0fs; the kernel was stopped.\n"
            "--- %s (last 12 lines) ---\n%s" % (budget, log_path, _tail(log_path))
        )
    if exit_code != 0 or not os.path.exists(produced):
        if os.path.exists(produced):
            try:
                os.unlink(produced)
            except OSError:
                pass
        raise SubprocessError(
            "nbconvert failed (exit %s)\n--- %s (last 12 lines) ---\n%s"
            % (exit_code, log_path, _tail(log_path))
        )
    os.replace(produced, dest)  # atomic publish
    try:
        os.unlink(log_path)
    except OSError:
        pass
    _, result = _read(dest)
    errors = sum(
        1
        for cell in _code_cells(result)
        if any(out.get("output_type") == "error" for out in cell.get("outputs") or [])
    )
    return "wrote %s (cells with errors: %d)" % (dest, errors)


def notebook_child(argv):
    """The child entry point: one notebook action, in this interpreter.

    The result text goes to stdout (the server hands it back as the tool's
    result); a failure goes to stderr with exit 1, so the server can report the
    real message instead of a bare "the child failed".
    """
    parser = argparse.ArgumentParser(prog="subproc.py --notebook")
    parser.add_argument("--notebook", dest="action", required=True, choices=NOTEBOOK_ACTIONS)
    parser.add_argument("--path", required=True)
    parser.add_argument("--out", default=None)
    parser.add_argument("--in-place", action="store_true")
    parser.add_argument("--cell-timeout", type=float, default=120.0)
    parser.add_argument("--timeout", type=float, default=None)
    parser.add_argument("--state-dir", default=None)
    args = parser.parse_args(argv)
    try:
        if args.action == "check":
            print(_check(args.path))
        elif args.action == "extract":
            print(_extract(args.path))
        elif args.action == "clear":
            print(_clear(args.path, args.out, args.in_place))
        else:
            print(
                _run_notebook(
                    args.path,
                    out=args.out,
                    in_place=args.in_place,
                    cell_timeout_s=args.cell_timeout,
                    timeout_s=args.timeout,
                    state_dir=args.state_dir,
                )
            )
    except SubprocessError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print("%s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(notebook_child(sys.argv[1:]))
