"""Find the python that runs the Sage kernel.

Imports no Sage and starts no kernel. `os`, `shutil`, `subprocess` and `sys`
stay module imports on purpose: the tests patch `sys.executable` and
`shutil.which` through the module attribute.
"""

import os
import shutil
import subprocess
import sys

from .errors import SAGE_NOT_FOUND, SageNotFound


# --- Sage resolution ---------------------------------------------------------

def _kernel_env(bin_dir: str) -> dict[str, str]:
    """Return this process's environment with `bin_dir` first in PATH.

    The kernel needs it: Sage calls Singular and other programs that live next
    to the sage executable. It reads the process environment once per start.
    """
    env = dict(os.environ)
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    return env


def resolve_kernel_python() -> tuple[str, str]:
    """Resolve the python that runs the Sage kernel. Return (python, bin_dir).

    `SAGE_BIN` wins over `sage` on PATH, which wins over the sibling `sage` of
    this process's interpreter (an installed console script lives in Sage's bin,
    so that finds Sage even when the host hides PATH). The sibling
    `python3`/`python` of the sage executable is preferred; otherwise the sage
    executable is asked for `sys.executable`. Never falls back to this
    process's interpreter: a kernel started by a non-Sage python silently lacks
    Sage. Reads the environment and may run `sage -c`; it starts no kernel and
    imports no Sage.
    """
    sage_bin = os.environ.get("SAGE_BIN") or shutil.which("sage")
    if not sage_bin:
        sibling = os.path.join(
            os.path.dirname(os.path.realpath(sys.executable)), "sage")
        if os.path.exists(sibling):
            sage_bin = sibling
    if not sage_bin:
        raise SageNotFound(SAGE_NOT_FOUND)
    bin_dir = os.path.dirname(os.path.realpath(sage_bin))
    for name in ("python3", "python"):
        candidate = os.path.join(bin_dir, name)
        if os.path.exists(candidate):
            return candidate, bin_dir
    try:
        done = subprocess.run(
            [sage_bin, "-c", "import sys; print(sys.executable)"],
            capture_output=True, text=True, timeout=120,
            env=_kernel_env(bin_dir),
        )
    except (OSError, subprocess.SubprocessError):
        raise SageNotFound(SAGE_NOT_FOUND) from None
    lines = [line.strip() for line in done.stdout.splitlines() if line.strip()]
    python = lines[-1] if lines else ""
    if not python or not os.path.exists(python):
        raise SageNotFound(SAGE_NOT_FOUND)
    return python, bin_dir
