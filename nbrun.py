#!/usr/bin/env python3
"""nbrun.py -- notebook helpers for the sage-mcp `notebook` tool.

Rule 18: ``nb run`` writes atomically (temp file + ``os.replace``) and the
nbconvert child's stdout/stderr go to a *file*, never a pipe.  A Jupyter kernel
can outlive the run and would keep an inherited pipe open, making the call look
hung long after the notebook was written.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import time


class NotebookError(Exception):
    pass


def available():
    """True when both reading and executing notebooks is possible."""
    return bool(
        importlib.util.find_spec("nbformat")
        and importlib.util.find_spec("nbconvert")
        and importlib.util.find_spec("nbclient")
    )


def _nbformat():
    import nbformat

    return nbformat


def read_notebook(path):
    nbformat = _nbformat()
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        raise NotebookError("no such notebook: %s" % path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return path, nbformat.read(fh, as_version=4)
    except Exception as exc:
        raise NotebookError("cannot read %s as a notebook: %s" % (path, exc))


def _kernel_name(nb):
    spec = (nb.get("metadata") or {}).get("kernelspec") or {}
    return spec.get("name") or "sagemath"


def _code_cells(nb):
    return [c for c in nb.cells if c.cell_type == "code"]


def _atomic_write(nb, dest):
    nbformat = _nbformat()
    dest = os.path.abspath(dest)
    folder = os.path.dirname(dest) or "."
    tmp = os.path.join(
        folder, ".%s.sage-mcp-%d.tmp" % (os.path.basename(dest), os.getpid())
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


def check(path):
    nbformat = _nbformat()
    path, nb = read_notebook(path)
    try:
        nbformat.validate(nb)
        validity = ""
    except Exception as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else "invalid"
        validity = "  [validation: %s]" % first
    code = _code_cells(nb)
    with_output = [c for c in code if c.get("outputs")]
    return (
        "OK   %s v%d.%d kernel=%s cells=%d (code=%d, with-output=%d)%s"
        % (
            path,
            nb.nbformat,
            nb.nbformat_minor,
            _kernel_name(nb),
            len(nb.cells),
            len(code),
            len(with_output),
            validity,
        )
    )


def extract(path):
    path, nb = read_notebook(path)
    code = _code_cells(nb)
    lines = ["# extracted from %s (%d code cells)" % (path, len(code))]
    for index, cell in enumerate(code, 1):
        lines.append("")
        lines.append("# --- cell %d (code) ---" % index)
        lines.append(cell.source.rstrip("\n"))
    return "\n".join(lines)


def clear(path, out=None, in_place=False):
    path, nb = read_notebook(path)
    cleared = 0
    for cell in _code_cells(nb):
        if cell.get("outputs") or cell.get("execution_count") is not None:
            cleared += 1
        cell["outputs"] = []
        cell["execution_count"] = None
    dest = path if (in_place or not out) else os.path.abspath(os.path.expanduser(out))
    _atomic_write(nb, dest)
    suffix = "" if dest == path else " -> %s" % dest
    return "cleared outputs in %d code cells%s" % (cleared, suffix)


def default_output_path(path):
    path = os.path.abspath(path)
    base = path[: -len(".ipynb")] if path.endswith(".ipynb") else path
    return base + ".out.ipynb"


def run(path, out=None, in_place=False, cell_timeout_s=120, timeout_s=None, state_dir=None):
    path, nb = read_notebook(path)
    code = _code_cells(nb)
    if in_place:
        dest = path
    elif out:
        dest = os.path.abspath(os.path.expanduser(out))
    else:
        dest = default_output_path(path)
    destdir = os.path.dirname(dest) or "."
    if not os.path.isdir(destdir):
        raise NotebookError("output directory does not exist: %s" % destdir)
    if timeout_s:
        budget = float(timeout_s)
    else:
        budget = float(cell_timeout_s) * (len(code) + 1) + 60.0

    state_dir = state_dir or os.environ.get("SAGE_MCP_STATE") or "/tmp"
    log_path = os.path.join(
        state_dir, "nbconvert-%d-%d.log" % (os.getpid(), int(time.time()))
    )
    base = os.path.basename(dest)
    if base.endswith(".ipynb"):
        base = base[: -len(".ipynb")]
    tmp_base = ".%s.sage-mcp-%d" % (base, os.getpid())
    produced = os.path.join(destdir, tmp_base + ".ipynb")

    cmd = [
        sys.executable,
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
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log,  # rule 18: files, never pipes
            stderr=log,
            cwd=os.path.dirname(path) or ".",
            start_new_session=True,
        )
        try:
            proc.wait(timeout=budget)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, 9)
            except OSError:
                proc.kill()
            proc.wait()
            raise NotebookError(
                "notebook execution timed out after %.0fs; the kernel was stopped.\n"
                "--- %s (last 12 lines) ---\n%s" % (budget, log_path, _tail(log_path))
            )
    rc = proc.returncode
    if rc != 0 or not os.path.exists(produced):
        if os.path.exists(produced):
            try:
                os.unlink(produced)
            except OSError:
                pass
        raise NotebookError(
            "nbconvert failed (exit %s)\n--- %s (last 12 lines) ---\n%s"
            % (rc, log_path, _tail(log_path))
        )
    os.replace(produced, dest)  # rule 18: atomic publish
    try:
        os.unlink(log_path)
    except OSError:
        pass

    _, result = read_notebook(dest)
    errors = 0
    for cell in _code_cells(result):
        if any(o.get("output_type") == "error" for o in cell.get("outputs") or []):
            errors += 1
    return "wrote %s (cells with errors: %d)" % (dest, errors)


def _tail(path, lines=12):
    try:
        with open(path, "r", errors="replace") as fh:
            rows = [r for r in fh.read()[-200000:].splitlines() if r.strip()]
    except OSError:
        return "(no log)"
    return "\n".join(rows[-lines:]) if rows else "(log is empty)"
