#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["fastmcp", "nbformat", "jupyter_client", "ipykernel"]
# ///
# Run:      python server.py     (the python of your Sage install, see install.sh)
# Install:  ./install.sh

"""A stateful SageMath MCP server: one kernel, one namespace, seven tools.

`exec` runs Sage code in the kernel namespace; `interrupt` stops a running
computation without losing it; `restart` replaces the kernel and wipes the
namespace. The notebook tools work on `.ipynb` files on disk: `nb_write`
creates one, `nb_read` reads it, `nb_edit` changes it in one atomic write, and
`nb_run` restarts the kernel, runs every code cell, and writes the outputs back.
Every tool answers with the same error envelope, and `error: null` means
success. The kernel starts on the first call and lives as long as the MCP
connection.

This is not a sandbox: the kernel runs the code that the agent sends, with the
user's permissions.
"""

import argparse
import ast
import asyncio
import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from queue import Empty
from typing import Annotated, Any, Callable

import nbformat
from fastmcp import FastMCP, Context
from fastmcp.server.lifespan import lifespan
from jupyter_client import KernelManager
from jupyter_client.kernelspec import KernelSpec
from jupyter_client.blocking import BlockingKernelClient
from pydantic import Field


# --- constants ---------------------------------------------------------------

# There are no environment overrides for these: MCP hosts sanitize the
# environment. SAGE_BIN is the one exception, because a host that hides PATH
# must still be able to name the Sage install.

KERNEL_READ_TIMEOUT = 2400.0     # default per-call budget for exec and cell runs
HEARTBEAT_INTERVAL = 15.0        # progress report interval while a call runs
SHELL_DRAIN_TIMEOUT = 30.0       # wait for the shell reply after idle
KERNEL_READY_TIMEOUT = 60.0      # wait_for_ready on a new kernel
SHUTDOWN_TIMEOUT = 30.0          # restart: wait this long for an in-flight call
MAX_INLINE_CHARS = 20000         # inline budget per call
GAP_CRASH_LIMIT = 3              # stderr matches of "Gap crashed" before abort
NOTEBOOK_NBFORMAT = 4
NOTEBOOK_MINOR = 5
KERNEL_META = {
    "kernelspec": {"name": "sagemath",
                   "display_name": "SageMath",
                   "language": "sage"},
    "language_info": {"name": "sage"},
}
GAP_CRASH_PATTERN = re.compile(r"Gap crashed|Restarting Gap", re.IGNORECASE)


# --- Sage resolution ---------------------------------------------------------

SAGE_NOT_FOUND = ("SageMath not found. Install it, or set SAGE_BIN to the sage "
                  "executable.")


class SageNotFound(Exception):
    """No Sage installation, or no python that can run its kernel, was found."""


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

    `SAGE_BIN` wins over `sage` on PATH. The sibling `python3`/`python` of the
    sage executable is preferred; otherwise the sage executable is asked for
    `sys.executable`. Never falls back to this process's interpreter: a kernel
    started by a non-Sage python silently lacks Sage. Reads the environment and
    may run `sage -c`; it starts no kernel and imports no Sage.
    """
    sage_bin = os.environ.get("SAGE_BIN") or shutil.which("sage")
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


# --- kernel lifecycle --------------------------------------------------------

# One lock serializes every computation. Only `_locked` and `_restart` touch it;
# all other kernel helpers are sync and assume the caller holds it.

_kernel_lock = asyncio.Lock()

# Serializes notebook file mutations across tool calls. `nb_run` holds it
# for the whole run, so a concurrent `nb_edit`/`nb_write` cannot land
# between its load and its write (that was a silent lost update). Lock
# order is always notebook lock, then kernel lock; nothing takes them the
# other way, so the two cannot deadlock.
_nb_lock = asyncio.Lock()


def _stop_kernel(state: dict) -> None:
    """Stop the channels and the kernel, then forget them.

    Ignores the errors of an already dead kernel. Never starts anything.
    """
    km, kc = state.get("km"), state.get("kc")
    if kc is not None:
        try:
            kc.stop_channels()
        except Exception:
            pass
    if km is not None:
        try:
            km.shutdown_kernel(now=True)
        except Exception:
            pass
    state["km"] = state["kc"] = None


def _start_kernel(state: dict) -> None:
    """Start a Sage kernel and wait until it answers, then store it in `state`.

    Resolution failures raise `SageNotFound`; a kernel that never becomes ready
    raises the underlying error. A failed start leaves no kernel and no channels
    behind. Assumes the lock is held.
    """
    python, bin_dir = resolve_kernel_python()
    km = KernelManager(kernel_name="python3")
    km._kernel_spec = KernelSpec(
        argv=[python, "-m", "ipykernel_launcher", "-f", "{connection_file}"],
        display_name="SageMath",
        language="sage",
    )
    km.start_kernel(env=_kernel_env(bin_dir))
    kc = km.blocking_client()
    state["km"], state["kc"] = km, kc
    try:
        kc.start_channels()
        kc.wait_for_ready(timeout=KERNEL_READY_TIMEOUT)
    except BaseException:
        _stop_kernel(state)
        raise
    # Seed the namespace now, not on the first user call. A SIGINT sent while the
    # seed is importing sage.all is deferred by the import machinery, so a runaway
    # first call would ignore `interrupt` and cost SHUTDOWN_TIMEOUT to restart.
    try:
        _seed_namespace(state)
    except BaseException:
        # A kernel that cannot import Sage is not usable. Do not leave it running.
        _stop_kernel(state)
        raise


def _ensure_kernel(state: dict) -> None:
    """Start a kernel if there is none. Keeps a live kernel as it is.

    A start failure is reported as `KernelDied` (no ready kernel exists), the
    same way `_restart_locked` reports it. `SageNotFound` keeps its own class,
    because it is an install problem, not a dead kernel.
    """
    if state["km"] is None:
        try:
            _start_kernel(state)
        except SageNotFound:
            raise
        except Exception as e:
            _stop_kernel(state)
            raise KernelDied(
                f"the kernel did not start ({type(e).__name__}: {e})") from e


def _restart_locked(state: dict) -> None:
    """Stop the kernel, start a fresh one, clear busy. Assumes the lock is held.

    A failure to start is reported as `KernelDied` (no ready kernel exists), not
    as a raw exception. The old namespace is lost either way.
    """
    state["busy"] = False
    if state["km"] is not None:
        _stop_kernel(state)
    try:
        _start_kernel(state)
    except SageNotFound:
        raise
    except Exception as e:
        _stop_kernel(state)
        raise KernelDied(
            f"the new kernel did not become ready ({type(e).__name__}: {e})") from e


async def _restart(state: dict) -> None:
    """Restart the kernel: interrupt, take the lock, replace the kernel.

    Interrupts an in-flight computation first (so a busy call lets go of the
    lock); if it does not within SHUTDOWN_TIMEOUT the kernel is killed, which
    makes the busy reader return `kernel_died`. Starts the new kernel eagerly,
    so a restart that cannot start one fails now, not on the next call. Never
    starts a second kernel and never touches the namespace directly.
    """
    km = state["km"]
    if km is not None and km.is_alive():
        try:
            km.interrupt_kernel()
        except Exception:
            pass
    try:
        await asyncio.wait_for(_kernel_lock.acquire(), SHUTDOWN_TIMEOUT)
    except asyncio.TimeoutError:
        km = state["km"]
        if km is not None:
            try:
                km.shutdown_kernel(now=True)
            except Exception:
                pass
        try:
            await asyncio.wait_for(_kernel_lock.acquire(), SHUTDOWN_TIMEOUT)
        except asyncio.TimeoutError:
            # The in-flight call is stuck outside the kernel (draining, or
            # preparing code). Do not touch the kernel from here: the call may
            # still be using its channels, and clearing the state under it is
            # how kernels get leaked. Report and let the caller retry.
            raise KernelDied(
                "a running call is still using the kernel; try again") from None
    try:
        await asyncio.to_thread(_restart_locked, state)
    finally:
        _kernel_lock.release()


def _interrupt(state: dict) -> bool:
    """Interrupt the running computation. Return whether one was running.

    True iff the kernel is alive and may be computing. `busy` is set for the
    whole read loop and stays set after a `timeout` (that computation is still
    running), until the next call completes or `restart` replaces the kernel;
    in that window a repeated interrupt is harmless and still reports true.
    Sends the kernel's interrupt request; keeps the namespace and never takes
    the lock (a running computation holds it).
    """
    km = state["km"]
    if km is None or not km.is_alive() or not state["busy"]:
        return False
    try:
        km.interrupt_kernel()
    except Exception:
        pass
    return True


def _consume(task: asyncio.Future) -> None:
    """Read a finished future's exception so asyncio does not warn about it."""
    if not task.cancelled():
        task.exception()


async def _locked(ctx: Context, fn: Callable[..., Any], *args) -> Any:
    """Run sync `fn` in a thread while holding the kernel lock, with heartbeats.

    Reports progress every HEARTBEAT_INTERVAL so a long computation keeps the
    connection alive. Returns fn's value or re-raises its exception. Never calls
    fn outside the lock and never starts a kernel itself.
    """
    async with _kernel_lock:
        task = asyncio.ensure_future(asyncio.to_thread(fn, *args))
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_INTERVAL)
                if done:
                    return task.result()
                try:
                    await ctx.report_progress(progress=0, total=None,
                                              message="Sage kernel still running...")
                except Exception:
                    pass
        finally:
            if not task.done():
                task.cancel()
                task.add_done_callback(_consume)


# --- execution ---------------------------------------------------------------

# The kernel has one namespace: the dict `__mcp_ns__` in its globals, seeded
# once with `from sage.all import *`. Helper names in the kernel use the `_mcp_`
# prefix so they do not clutter `dir()` for user code.


class SourceError(Exception):
    """The source is not valid Sage code (preparse, parse, or compile failed)."""


class ExecTimeout(Exception):
    """The per-call budget expired. The kernel may still be computing."""

    def __init__(self, seconds: float):
        super().__init__(f"the call exceeded {seconds:g} s")
        self.seconds = seconds


class KernelDied(Exception):
    """The kernel died, so the namespace is gone."""


class GapCrashLoop(Exception):
    """GAP crashed `GAP_CRASH_LIMIT` times in one call; the call was interrupted."""

    def __init__(self, outputs: list[dict]):
        super().__init__("GAP crashed repeatedly")
        self.outputs = outputs
        self.stderr = "".join(
            o["text"] for o in outputs
            if o["output_type"] == "stream" and o.get("name") == "stderr")


def _prepare(source: str) -> str:
    """Preparse and compile Sage source; return the preparsed text.

    This is the static check that execution and notebook validation share, so a
    source that passes here fails at the same place when it runs. Imports Sage
    lazily (never at module import) and raises `SourceError` with the exception
    text on a syntax error, `SageNotFound` if Sage cannot be imported.
    """
    try:
        from sage.repl.preparse import preparse
    except ImportError as e:
        raise SageNotFound(SAGE_NOT_FOUND) from e
    prepared = preparse(source)
    try:
        ast.parse(prepared)
        compile(prepared, "<sage>", "exec")
    except SyntaxError as e:
        detail = e.msg or "invalid syntax"
        if e.lineno is not None:
            detail = f"{detail} (line {e.lineno})"
        raise SourceError(detail) from None
    except UnicodeError as e:
        # A lone surrogate can come out of a JSON \uD800 escape and is not valid
        # source. It is a property of the source, so `invalid_source` is right.
        raise SourceError(f"the source is not valid Unicode ({e})") from None
    return prepared


def _wrapper(prepared: str, filename: str) -> str:
    """Build the kernel-side code that runs one prepared source.

    Seeds the shared namespace once, then execs the prepared tree in it. A
    trailing expression is evaluated after the statements and published through
    IPython with the `_mcp_trailing` metadata, so the collector can turn it into
    an `execute_result`; a `None` value publishes nothing. Pure: it runs no code.
    """
    return f'''
if "__mcp_ns__" not in globals():
    _mcp_seed = {{"__name__": "__main__"}}
    exec("from sage.all import *", _mcp_seed)
    globals()["__mcp_ns__"] = _mcp_seed
_mcp_ns = globals()["__mcp_ns__"]
import ast as _mcp_ast
_mcp_tree = _mcp_ast.parse({prepared!r})
if _mcp_tree.body and isinstance(_mcp_tree.body[-1], _mcp_ast.Expr):
    _mcp_last = _mcp_ast.Expression(_mcp_tree.body.pop().value)
    exec(compile(_mcp_tree, {filename!r}, "exec"), _mcp_ns, _mcp_ns)
    _mcp_value = eval(compile(_mcp_last, {filename!r}, "eval"), _mcp_ns, _mcp_ns)
    if _mcp_value is not None:
        from IPython.display import display as _mcp_display
        _mcp_data = {{"text/plain": repr(_mcp_value)}}
        # Sage objects can give LaTeX and HTML. Use them when a notebook shows the value.
        for _mcp_mime, _mcp_attr, _mcp_fmt in (("text/latex", "_latex_", "$%s$"),
                                               ("text/html", "_html_", "%s")):
            _mcp_method = getattr(_mcp_value, _mcp_attr, None)
            if _mcp_method is not None:
                try:
                    _mcp_data[_mcp_mime] = _mcp_fmt % _mcp_method()
                except Exception:
                    pass
        _mcp_display(_mcp_data, raw=True, metadata={{"_mcp_trailing": True}})
else:
    exec(compile(_mcp_tree, {filename!r}, "exec"), _mcp_ns, _mcp_ns)
'''


def _seed_namespace(state: dict) -> None:
    """Fill `__mcp_ns__` with sage.all by running an empty source. Assumes the lock.

    Done at kernel start, so no user call pays the import and no user call can be
    during it (SIGINT is deferred inside the import, which would make `interrupt`
    a no-op). `prepared=""` skips the server-side preparse, so this module still
    imports no Sage. Raises `SageNotFound` when the kernel cannot import Sage (a
    broken install or a wrong python), and `KernelDied` when the seed itself
    fails or is interrupted: a kernel that cannot seed is not a usable kernel, so
    the caller must stop it. Never starts a kernel and never writes a file.
    """
    try:
        outs = _run_source(state, "", "<seed>", prepared="")
    except KeyboardInterrupt:
        raise KernelDied("the namespace seed was interrupted") from None
    detail = _summarise(outs)[3]
    if detail is not None:
        text = "".join(
            line for out in outs if out.get("output_type") == "error"
            for line in (out.get("traceback") or []))
        reason = text.strip().splitlines()[-1] if text.strip() else detail
        raise SageNotFound(
            "The kernel python cannot import SageMath, so the kernel has no Sage. "
            f"Check SAGE_BIN. ({reason})")


def _drain_shell(kc: BlockingKernelClient, msg_id: str) -> None:
    """Read the shell reply for `msg_id` for up to SHELL_DRAIN_TIMEOUT, ignoring failures."""
    deadline = time.monotonic() + SHELL_DRAIN_TIMEOUT
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            reply = kc.get_shell_msg(timeout=remaining)
            if reply.get("parent_header", {}).get("msg_id") == msg_id:
                return
    except Exception:
        return


def _died(state: dict, message: str) -> None:
    """Stop and forget a dead kernel, then raise `KernelDied`. Holds the lock.

    Clearing the state here, while the lock is still held, is what keeps a
    concurrent `restart` from starting a kernel that the failing call then
    forgets. Never starts anything.
    """
    _stop_kernel(state)
    raise KernelDied(message)


def _run_source(state: dict, source: str, filename: str, *,
                prepared: str | None = None,
                timeout: float | None = None) -> list[dict]:
    """Run one source in the shared namespace and return its outputs in order.

    The single execution path. Marks the state busy for the whole read loop,
    collects stream/execute_result/display_data/error messages for this call's
    message id, drains the shell reply, and guards against a GAP crash loop.
    Raises `SourceError`, `ExecTimeout`, `KernelDied`, or `GapCrashLoop`; on a
    timeout the kernel keeps computing (busy stays set so `interrupt` works).
    A dead kernel is stopped and forgotten before `KernelDied` is raised.
    Assumes the lock is held; starts no kernel.
    """
    if prepared is None:
        prepared = _prepare(source)
    budget = KERNEL_READ_TIMEOUT if timeout is None else timeout
    km, kc = state["km"], state["kc"]
    if km is None or kc is None or not km.is_alive():
        _died(state, "the kernel is not running")
    deadline = time.monotonic() + budget
    outs: list[dict] = []
    gap_crashes = 0
    timed_out = False
    state["busy"] = True
    try:
        try:
            msg_id = kc.execute(_wrapper(prepared, filename), allow_stdin=False)
        except Exception:
            if not km.is_alive():
                _died(state, "the kernel died before it accepted the code")
            raise
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ExecTimeout(budget)
            try:
                msg = kc.get_iopub_msg(timeout=min(remaining, 5.0))
            except Empty:
                # A dead kernel never sends the idle message. Do not wait for it.
                if not km.is_alive():
                    _died(state, "the kernel died during execution")
                continue
            except Exception:
                if not km.is_alive():
                    _died(state, "the kernel died during execution")
                raise
            if msg.get("parent_header", {}).get("msg_id") != msg_id:
                continue
            mtype = msg["header"]["msg_type"]
            content = msg["content"]
            if mtype == "stream":
                name = content.get("name", "stdout")
                text = content.get("text", "")
                outs.append({"output_type": "stream", "name": name, "text": text})
                if name == "stderr":
                    gap_crashes += len(GAP_CRASH_PATTERN.findall(text))
                    if gap_crashes >= GAP_CRASH_LIMIT:
                        # Stop the runaway process. Do not wait for a shell reply:
                        # an interrupted GAP may never send one.
                        try:
                            km.interrupt_kernel()
                        except Exception:
                            pass
                        raise GapCrashLoop(outs)
            elif mtype in ("execute_result", "display_data"):
                metadata = dict(content.get("metadata") or {})
                # The wrapper marks the value of the trailing expression.
                kind = ("execute_result" if metadata.pop("_mcp_trailing", False)
                        else mtype)
                outs.append({"output_type": kind,
                             "data": content.get("data") or {},
                             "metadata": metadata})
            elif mtype == "error":
                outs.append({"output_type": "error",
                             "ename": content.get("ename"),
                             "evalue": content.get("evalue"),
                             "traceback": content.get("traceback") or []})
            elif mtype == "status" and content.get("execution_state") == "idle":
                break
    except ExecTimeout as e:
        # The kernel is still computing. Keep busy set, so `interrupt` can reach
        # it (the flag means "may be computing"; it clears on the next completed
        # call or a restart). Keep the partial outputs for a notebook caller.
        timed_out = True
        e.outputs = outs
        raise
    finally:
        state["busy"] = timed_out
    _drain_shell(kc, msg_id)
    return outs


# --- output shaping ----------------------------------------------------------


def _summarise(outs: list[dict]) -> tuple[str, str, str | None, str | None]:
    """Pure: make the short text view of outputs.

    Return `(stdout, stderr, last trailing value or None, "Ename: evalue" or
    None)`. The trailing value is the `text/plain` of an `execute_result`, which
    only the wrapper's trailing-expression display produces; a `display(...)` in
    user code is a `display_data` and is never reported as the result. So a
    statement-only source has no result, even when it displays something. Pure.
    """
    def stream(name):
        return "".join(o["text"] for o in outs
                       if o["output_type"] == "stream" and o.get("name") == name)

    result = next((o["data"]["text/plain"] for o in reversed(outs)
                   if o.get("output_type") == "execute_result"
                   and "text/plain" in o.get("data", {})), None)
    error = next((f"{o['ename']}: {o['evalue']}"
                  for o in outs if o["output_type"] == "error"), None)
    return stream("stdout"), stream("stderr"), result, error


def _truncate(text: str, budget: int) -> tuple[str, bool]:
    """Cut `text` to `budget` characters and mark the cut. Pure.

    Returns `(text, cut)`; a cut text keeps the first `budget` characters and
    appends a marker naming how many were dropped. The marker is not counted in
    the budget. Never touches the source of the text (a file, a notebook).
    """
    if len(text) <= budget:
        return text, False
    if budget < 0:
        budget = 0
    cut = len(text) - budget
    return text[:budget] + f"\n… [truncated {cut} characters]", True


def _exec_view(outs: list[dict]) -> dict:
    """Shape a call's outputs into `{result, stdout, stderr, truncated}`. Pure.

    One MAX_INLINE_CHARS budget shared by `result`, then `stdout`, then
    `stderr`, in that order; a later field is cut when the earlier ones already
    used the budget. `truncated` is true iff anything was cut. The untruncated
    text is not kept anywhere.
    """
    stdout, stderr, result, _ = _summarise(outs)
    result = result or ""
    budget = MAX_INLINE_CHARS
    result, cut_result = _truncate(result, budget)
    # The cut marker is not content: charge only the characters that were kept.
    budget = max(0, budget - (budget if cut_result else len(result)))
    stdout, cut_stdout = _truncate(stdout, budget)
    budget = max(0, budget - (budget if cut_stdout else len(stdout)))
    stderr, cut_stderr = _truncate(stderr, budget)
    return {"result": result, "stdout": stdout, "stderr": stderr,
            "truncated": cut_result or cut_stdout or cut_stderr}


def _exec_locked(state: dict, code: str, timeout: float | None) -> dict:
    """Lock held: ensure a kernel, run `code`, shape the result.

    Returns the arguments for `_exec_out` (result, stdout, stderr, truncated,
    error). A user exception becomes `cell_execution_failed` here; the other
    failure types are raised for the tool wrapper to map.
    """
    _ensure_kernel(state)
    outs = _run_source(state, code, "<exec>", timeout=timeout)
    view = _exec_view(outs)
    detail = _summarise(outs)[3]
    if detail is None:
        return {**view, "error": None}
    return {**view,
            "error": _err("cell_execution_failed",
                          "The source raised an exception.", detail)}


# --- notebook I/O ------------------------------------------------------------

# One file is the only source of truth: every read comes from disk and every
# write replaces it atomically. Notebooks below nbformat 4.5 are upgraded in
# memory (cell ids are minted) so ops can address every cell, but the upgrade
# only reaches the disk file when a writing tool runs.


class NotebookError(Exception):
    """A notebook failure with the SPEC §9.2 `code`, message, and details."""

    def __init__(self, code: str, message: str, details: object = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = None if details is None else str(details)


def _fresh_id(seen: set[str]) -> str:
    """Return a cell id that is not in `seen`. Pure apart from reading `seen`."""
    while True:
        cid = uuid.uuid4().hex[:8]
        if cid not in seen:
            return cid


def _check_raw_ids(raw: dict) -> None:
    """Reject a 4.5+ file whose cells lack a unique, non-empty `id`. Pure.

    nbformat.reads silently repairs a missing or duplicate id, which would hide
    the defect this check exists for, so the raw JSON is inspected first.
    """
    cells = raw.get("cells")
    if not isinstance(cells, list):
        return                       # nbformat.validate reports the real problem
    seen: set[str] = set()
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        cid = cell.get("id")
        if not isinstance(cid, str) or not cid:
            raise NotebookError(
                "invalid_notebook", "A cell is missing its id.", cid)
        if cid in seen:
            raise NotebookError(
                "invalid_notebook", "Two cells share the same id.", cid)
        seen.add(cid)


def _check_ids(nb) -> None:
    """Require every cell to have a present, unique id in the in-memory notebook.

    Addressing is by id, so this is load-bearing. Raises `invalid_notebook` and
    names the offending id. Never changes the notebook.
    """
    seen: set[str] = set()
    for cell in nb.cells:
        cid = cell.get("id")
        if not isinstance(cid, str) or not cid:
            raise NotebookError(
                "invalid_notebook", "A cell is missing its id.", cid)
        if cid in seen:
            raise NotebookError(
                "invalid_notebook", "Two cells share the same id.", cid)
        seen.add(cid)


def _legacy_id(index: int, seen: set[str]) -> str:
    """Return a deterministic id for the id-less cell at `index`, unused in `seen`.

    Deterministic on purpose: a pre-4.5 notebook is upgraded in memory on every
    load, so random ids would make a `nb_read` → `nb_edit` chain fail with
    `unknown_cell_id`. Pure.
    """
    candidate = f"cell-{index}"
    n = 2
    while candidate in seen:
        candidate = f"cell-{index}-{n}"
        n += 1
    return candidate


def _upgrade_ids(nb) -> None:
    """Upgrade a pre-4.5 notebook in memory: set 4.5 and mint missing ids.

    The file is not touched. Ids are seeded explicitly (deterministically, see
    `_legacy_id`) rather than through `normalize`, so nothing else in the
    notebook is rewritten and two loads of the same file give the same ids.
    Never writes.
    """
    seen = {c.get("id") for c in nb.cells if isinstance(c.get("id"), str)}
    nb.nbformat_minor = NOTEBOOK_MINOR
    for index, cell in enumerate(nb.cells):
        cid = cell.get("id")
        if not isinstance(cid, str) or not cid:
            cid = _legacy_id(index, seen)
            cell["id"] = cid
        seen.add(cid)


def _check_text(nb) -> None:
    """Reject a notebook whose cells cannot be sent as UTF-8 text. Pure.

    A JSON `\\ud800` escape is legal JSON and legal nbformat, but a lone
    surrogate cannot be encoded in the JSON-RPC response, so returning such a
    cell would break the connection. Refusing the notebook with a declared code
    is the only safe answer. Never changes the notebook.
    """
    for index, cell in enumerate(nb.cells):
        source = cell.get("source") or ""
        if isinstance(source, list):
            source = "".join(source)
        try:
            source.encode("utf-8")
        except UnicodeEncodeError as e:
            raise NotebookError(
                "invalid_notebook",
                "A cell contains text that cannot be sent as UTF-8.",
                f"cell {index} ({cell.get('id')}): {e}") from None


def _load_nb(path: str) -> tuple[Any, int, int]:
    """Load, parse, and validate the notebook at `path`.

    Returns `(nb, on_disk_nbformat, on_disk_nbformat_minor)`: the notebook is
    upgraded in memory to 4.5 with unique cell ids, while the two integers are
    the values actually stored on disk (3/0 and 4/0 for a v3/4.0 file, not the
    version `nbformat.reads` converts to). Raises `NotebookError` with
    `not_found` when the file is absent or unreadable and `invalid_notebook`
    when it is not UTF-8, not JSON, is not schema-valid, or has
    missing/duplicate cell ids. Reads only; never writes, never executes, and
    never checks for magics or syntax (that is `_validate_cells`).
    """
    p = Path(path)
    try:
        data = p.read_bytes()
    except FileNotFoundError:
        raise NotebookError(
            "not_found", "The notebook file does not exist.", str(path)) from None
    except OSError as e:
        raise NotebookError(
            "not_found", "The notebook file cannot be read.",
            f"{type(e).__name__}: {e}") from None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise NotebookError(
            "invalid_notebook", "The notebook file is not UTF-8 text.",
            f"{type(e).__name__}: {e}") from None
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, TypeError) as e:
        raise NotebookError(
            "invalid_notebook", "The file is not valid JSON.",
            f"{type(e).__name__}: {e}") from None
    if not isinstance(raw, dict):
        raise NotebookError(
            "invalid_notebook", "The file is not a notebook object.",
            type(raw).__name__)
    disk_format = raw.get("nbformat")
    disk_minor = raw.get("nbformat_minor")
    if not isinstance(disk_format, int) or not isinstance(disk_minor, int):
        raise NotebookError(
            "invalid_notebook",
            "The notebook does not declare its nbformat version.",
            f"nbformat={disk_format!r}, nbformat_minor={disk_minor!r}")
    if disk_minor >= NOTEBOOK_MINOR:
        _check_raw_ids(raw)
    try:
        nb = nbformat.reads(text, as_version=4)
        nbformat.validate(nb)
    except Exception as e:
        raise NotebookError(
            "invalid_notebook", "The notebook is not nbformat-valid.",
            f"{type(e).__name__}: {e}") from None
    if disk_minor < NOTEBOOK_MINOR:
        _upgrade_ids(nb)
    _check_ids(nb)
    _check_text(nb)
    return nb, disk_format, disk_minor


def _validate_cells(nb) -> None:
    """Statically validate every code cell: no magics, and it must prepare.

    Walks the cells in order. A `%`/`!` as the first non-space character of any
    line raises `unsupported_magic`; a source that does not preparse/compile
    raises `invalid_source`. `details` is `"<cell_id>: <offending line/reason>"`.
    Markdown and raw cells are never validated as code. Never writes, never
    executes, and never restarts a kernel.
    """
    for cell in nb.cells:
        if cell.get("cell_type") != "code":
            continue
        cid = cell.get("id") or ""
        source = cell.get("source") or ""
        if isinstance(source, list):
            source = "".join(source)
        for line in source.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("%") or stripped.startswith("!"):
                raise NotebookError(
                    "unsupported_magic",
                    "A code cell contains an IPython magic or shell escape.",
                    f"{cid}: {stripped}")
        try:
            _prepare(source)
        except SourceError as e:
            raise NotebookError(
                "invalid_source",
                "A code cell does not preparse into valid Sage code.",
                f"{cid}: {e}") from None


def _save_nb(nb, path: str) -> None:
    """Write the notebook at nbformat 4.5 with an atomic replace.

    The parent directory must already exist (it is never created). A symlink is
    resolved first, so the real notebook is written and the link survives. The
    new bytes go to a `NamedTemporaryFile` in that same directory, are flushed
    and fsynced, get the mode of the file they replace (0600 on a fresh temp
    would silently restrict a 0644 notebook), and are then `os.replace`d over
    the target. Any failure unlinks the temp file and raises `write_failed`,
    leaving the target untouched. The only function in this module that writes a
    notebook file; never executes.
    """
    p = Path(path)
    target = p.resolve() if p.is_symlink() else p
    parent = target.parent
    if not parent.is_dir():
        raise NotebookError(
            "write_failed", "The notebook's directory does not exist.",
            str(parent))
    try:
        mode = target.stat().st_mode & 0o777
    except OSError:
        mode = 0o644
    nb.nbformat = NOTEBOOK_NBFORMAT
    nb.nbformat_minor = NOTEBOOK_MINOR
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(
                dir=parent, suffix=".tmp", delete=False,
                mode="w", encoding="utf-8") as f:
            tmp_name = f.name
            nbformat.write(nb, f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, target)
        tmp_name = None
    except Exception as e:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        raise NotebookError(
            "write_failed", "The notebook could not be written atomically.",
            f"{type(e).__name__}: {e}") from None


def _new_cell(cell_type: str, source: str):
    """Build one nbformat cell of `cell_type` with `source`. Pure.

    Raises `invalid_notebook` for an unknown cell type. The returned cell has an
    id from nbformat when the schema wants one; callers ensure uniqueness.
    """
    if cell_type == "code":
        return nbformat.v4.new_code_cell(source)
    if cell_type == "markdown":
        return nbformat.v4.new_markdown_cell(source)
    if cell_type == "raw":
        return nbformat.v4.new_raw_cell(source)
    raise NotebookError(
        "invalid_notebook", "Unknown cell type.", repr(cell_type))


def _nb_read_cells(nb, cell_ids: list[str] | None) -> list[dict]:
    """Select cells by id and shape them for `nb_read`. Pure.

    With `cell_ids`, cells come back in the order given; otherwise in notebook
    order. Each item is `{cell_id, cell_type, source}` plus `outputs` (full, from
    disk) only on code cells. An id that is not present raises `unknown_cell_id`
    with the id as details. Never writes and never executes.
    """
    by_id = {cell.get("id"): cell for cell in nb.cells}
    if cell_ids is None:
        selected = list(nb.cells)
    else:
        selected = []
        for cid in cell_ids:
            cell = by_id.get(cid)
            if cell is None:
                raise NotebookError(
                    "unknown_cell_id",
                    "The notebook has no cell with that id.", cid)
            selected.append(cell)
    result = []
    for cell in selected:
        source = cell.get("source") or ""
        if isinstance(source, list):
            source = "".join(source)
        item = {"cell_id": cell.get("id"),
                "cell_type": cell.get("cell_type"),
                "source": source}
        if cell.get("cell_type") == "code":
            item["outputs"] = copy.deepcopy(list(cell.get("outputs") or []))
        result.append(item)
    return result


_OP_KEYS = {
    "edit": {"op", "cell_id", "source"},
    "add": {"op", "cell_type", "source", "after"},
    "remove": {"op", "cell_id"},
}


def _op_fail(index: int, name: object, code: str, message: str,
             reason: object = None) -> NotebookError:
    """Build a `nb_edit` failure whose details name the zero-based op. Pure."""
    label = name if isinstance(name, str) and name else "?"
    details = f"op {index} ({label}):"
    if reason is not None:
        details += f" {reason}"
    return NotebookError(code, message, details)


def _find_cell(nb, cell_id: str):
    """Return the index of `cell_id`, or None. Pure."""
    for index, cell in enumerate(nb.cells):
        if cell.get("id") == cell_id:
            return index
    return None


def _apply_ops(nb, ops: list) -> list[dict]:
    """Apply `edit`/`add`/`remove` ops to the in-memory notebook. Returns applied.

    Every op is shape-checked here (the caller's input is not trusted), so a
    malformed op raises `invalid_op` and never reaches nbformat. An unknown id
    raises `unknown_cell_id`; both carry `details = "op <i> (<op>): <reason>"`.
    Mutates `nb` only; the caller writes it once if the whole result validates.
    Never writes a file, never executes, and never creates a notebook.
    """
    applied: list[dict] = []
    for index, op in enumerate(ops):
        if not isinstance(op, dict):
            raise _op_fail(index, None, "invalid_op",
                           "Each op must be an object.",
                           type(op).__name__)
        name = op.get("op")
        if name not in _OP_KEYS:
            raise _op_fail(index, name, "invalid_op",
                           "Unknown or missing op name.", repr(name))
        extra = set(op) - _OP_KEYS[name]
        if extra:
            raise _op_fail(index, name, "invalid_op",
                           "An op has an unexpected field.",
                           ", ".join(sorted(extra)))
        source = op.get("source")
        if name in ("edit", "add") and not isinstance(source, str):
            raise _op_fail(index, name, "invalid_op",
                           "`source` must be a string.", type(source).__name__)
        cell_id = op.get("cell_id")
        if name in ("edit", "remove") and not isinstance(cell_id, str):
            raise _op_fail(index, name, "invalid_op",
                           "`cell_id` must be a string.", type(cell_id).__name__)
        if name == "edit":
            pos = _find_cell(nb, cell_id)
            if pos is None:
                raise _op_fail(index, name, "unknown_cell_id",
                               "The notebook has no cell with that id.", cell_id)
            nb.cells[pos]["source"] = source
            applied.append({"op": "edit", "cell_id": cell_id})
        elif name == "remove":
            pos = _find_cell(nb, cell_id)
            if pos is None:
                raise _op_fail(index, name, "unknown_cell_id",
                               "The notebook has no cell with that id.", cell_id)
            del nb.cells[pos]
            applied.append({"op": "remove", "cell_id": cell_id})
        else:  # add
            cell_type = op.get("cell_type", "code")
            if cell_type not in ("code", "markdown", "raw"):
                raise _op_fail(index, name, "invalid_op",
                               "Unknown cell type.", repr(cell_type))
            after = op.get("after")
            if "after" in op and not isinstance(after, str):
                raise _op_fail(index, name, "invalid_op",
                               "`after` must be a cell id string.",
                               type(after).__name__)
            if after is not None:
                pos = _find_cell(nb, after)
                if pos is None:
                    raise _op_fail(index, name, "unknown_cell_id",
                                   "The notebook has no cell with that id.",
                                   after)
                pos += 1
            else:
                pos = len(nb.cells)
            new = _new_cell(cell_type, source)
            seen = {c.get("id") for c in nb.cells}
            if not new.get("id") or new.get("id") in seen:
                new["id"] = _fresh_id(seen)
            nb.cells.insert(pos, new)
            applied.append({"op": "add", "cell_id": new["id"]})
    return applied


def _prefix_validation_error(e: NotebookError, applied: list[dict]) -> NotebookError:
    """Name the op that produced a post-ops validation failure.

    `_apply_ops` already prefixes its own failures. A failure of the whole
    result is attributed to the last op that touched the offending cell (or to
    the last applied op when the cell predates the call), so every op-phase
    failure keeps the `"op <i> (<op>): <reason>"` shape. Pure.
    """
    if not applied or (e.details and e.details.startswith("op ")):
        return e
    cell_id = (e.details or "").split(":", 1)[0]
    index = len(applied) - 1
    for i in range(len(applied) - 1, -1, -1):
        if applied[i].get("cell_id") == cell_id:
            index = i
            break
    return NotebookError(
        e.code, e.message,
        f"op {index} ({applied[index]['op']}): {e.details or e.message}")


# --- notebook execution ------------------------------------------------------


def _normalise_outputs(outs: list[dict], execution_count: int) -> list:
    """Give collector outputs the fields nbformat requires, as NotebookNodes.

    `execute_result` needs an `execution_count`; the collector does not know it.
    Everything else is already nbformat-shaped. Pure; never touches the wire
    outputs the caller keeps for the inline view.
    """
    result = []
    for out in outs:
        item = copy.deepcopy(out)
        if item.get("output_type") == "execute_result":
            item.setdefault("execution_count", execution_count)
        result.append(nbformat.from_dict(item))
    return result


def _cell_error_detail(cell_id: str, outs: list[dict]) -> str:
    """Build `"<cell_id>: <traceback or Ename: evalue>"`. Pure."""
    for out in outs:
        if out.get("output_type") == "error":
            traceback = out.get("traceback") or []
            text = ("\n".join(str(line) for line in traceback)
                    if traceback else f"{out.get('ename')}: {out.get('evalue')}")
            return f"{cell_id}: {text}"
    return f"{cell_id}: the cell raised an exception"


def _run_nb_locked(state: dict, nb, path: str) -> tuple[list[dict], bool, bool, object]:
    """Restart and run every code cell of `nb`, then write the outputs once.

    Lock held. Returns `(inline_cells, truncated, saved, failure)` where
    `failure` is a `NotebookError` or None. `_restart_locked` runs first, so the
    namespace from prior calls is gone. Cells run in order; the first one that
    raises (or whose outputs carry an error) stops the run, and the notebook is
    written with outputs/execution_count up to and including it. Later cells
    keep what was on disk. The file receives the full outputs; the returned
    cells are deep copies cut to one MAX_INLINE_CHARS budget. A notebook with no
    code cell is not written. The kernel is left running and warm.
    """
    _restart_locked(state)
    code_cells = [c for c in nb.cells if c.get("cell_type") == "code"]
    if not code_cells:
        return [], False, False, None
    existing = [c.get("execution_count") for c in code_cells
                if isinstance(c.get("execution_count"), int)]
    count = (max(existing) if existing else 0) + 1
    executed: list = []
    failure = None
    for cell in code_cells:
        cell_id = cell.get("id") or ""
        outs: list[dict] = []
        try:
            outs = _run_source(state, cell.get("source") or "",
                               f"{path}:{cell_id}")
        except GapCrashLoop as e:
            outs = e.outputs
            cell.execution_count = count
            cell.outputs = _normalise_outputs(outs, count)
            executed.append(cell)
            failure = NotebookError(
                "cell_execution_failed", "A cell's GAP process crashed repeatedly.",
                f"{cell_id}: {e.stderr or 'GAP crashed repeatedly'}")
            break
        except ExecTimeout as e:
            # The kernel keeps computing; keep whatever the cell already printed
            # and say plainly that the budget, not the code, ended the cell.
            outs = getattr(e, "outputs", []) or []
            cell.execution_count = count
            cell.outputs = _normalise_outputs(outs, count)
            executed.append(cell)
            failure = NotebookError(
                "cell_execution_failed",
                "A cell exceeded the kernel read budget; partial outputs are kept.",
                f"{cell_id}: {e}")
            break
        except KernelDied as e:
            # The cells that already ran are still worth keeping: the kernel is
            # gone either way, so do not throw their outputs away.
            failure = NotebookError(
                "kernel_died", "The kernel died during the run.",
                f"{cell_id}: {e}. Outputs of the cells that ran are kept; the "
                "rest of the notebook did not run.")
            break
        cell.execution_count = count
        cell.outputs = _normalise_outputs(outs, count)
        executed.append(cell)
        detail = _summarise(outs)[3]
        if detail is not None:
            failure = NotebookError(
                "cell_execution_failed", "A notebook cell raised an exception.",
                _cell_error_detail(cell_id, outs))
            break
        count += 1
    _save_nb(nb, path)
    inline, truncated = _inline_cells(executed, MAX_INLINE_CHARS)
    return inline, truncated, True, failure

def _inline_cells(cells: list, budget: int) -> tuple[list[dict], bool]:
    """Copy executed cells and cut their outputs to one inline budget. Pure.

    Outputs are added whole in cell order; the first output that would exceed
    `budget` clears that cell's outputs and every later cell's outputs, and sets
    `truncated`. Operates on deep copies, so the caller's (on-disk) outputs are
    never touched. Never writes.
    """
    result: list[dict] = []
    used = 0
    cut = False
    for cell in cells:
        outputs: list = []
        if not cut:
            outputs = copy.deepcopy(list(cell.get("outputs") or []))
            size = sum(len(json.dumps(out)) for out in outputs)
            if used + size > budget:
                cut = True
                outputs = []
            else:
                used += size
        result.append({"cell_id": cell.get("id") or "",
                       "cell_type": cell.get("cell_type") or "code",
                       "outputs": outputs})
    return result, cut


# --- error envelope and schemas ----------------------------------------------

def _err(code: str, message: str, details: object = None) -> dict:
    """Build the D1 error envelope. Coerce `details` to str or None."""
    return {"code": code, "message": message,
            "details": None if details is None else str(details)}


def _error_schema(codes: list[str]) -> dict:
    """Build the inline error envelope for one tool's `code` enum.

    Inlined, not a `$ref`, so FastMCP's strict validation needs no resolver.
    """
    return {
        "type": ["object", "null"],
        "description": ("null on success. On failure, an object: `code` is the "
                        "failure class, `message` is one human sentence, "
                        "`details` is the machine-useful extra (traceback, "
                        "offending source) or null."),
        "properties": {
            "code": {"type": "string", "enum": list(codes)},
            "message": {"type": "string"},
            "details": {"type": ["string", "null"]},
        },
        "required": ["code", "message", "details"],
        "additionalProperties": False,
    }


OUTPUT_SCHEMA_EXEC = {
    "type": "object",
    "properties": {
        "result": {
            "type": "string",
            "description": ("repr() of the value of the final expression, if the "
                            "source ends in an expression. Empty string if the "
                            "source is purely statements or the final expression "
                            "is None."),
        },
        "stdout": {
            "type": "string",
            "description": "Anything printed to stdout.",
        },
        "stderr": {
            "type": "string",
            "description": ("Anything the code wrote to stderr. Kept apart from "
                            "`stdout`, never folded into it."),
        },
        "truncated": {
            "type": "boolean",
            "default": False,
            "description": ("True if the combined `result` + `stdout` + `stderr` "
                            "payload exceeded the per-call budget and was cut. "
                            "The untruncated text is not recoverable."),
        },
        "error": _error_schema(["invalid_source", "cell_execution_failed",
                                "timeout", "gap_crash_loop", "kernel_died",
                                "sage_not_found", "internal"]),
    },
    "required": ["result", "stdout", "stderr", "truncated", "error"],
    "additionalProperties": False,
}

OUTPUT_SCHEMA_INTERRUPT = {
    "type": "object",
    "properties": {
        "interrupted": {
            "type": "boolean",
            "description": ("True if the kernel may be computing and the interrupt "
                            "request was sent; false if no kernel exists or none is "
                            "computing. After a `timeout`, the abandoned computation "
                            "is still running, so this stays true until a later call "
                            "completes or the kernel is restarted."),
        },
        "error": _error_schema(["internal"]),
    },
    "required": ["interrupted", "error"],
    "additionalProperties": False,
}

OUTPUT_SCHEMA_RESTART = {
    "type": "object",
    "properties": {
        "restarted": {
            "type": "boolean",
            "description": ("True iff a new, ready kernel is running after this "
                            "call. False only with a non-null `error`."),
        },
        "error": _error_schema(["sage_not_found", "kernel_died", "internal"]),
    },
    "required": ["restarted", "error"],
    "additionalProperties": False,
}


OUTPUT_SCHEMA_NB_RUN = {
    "type": "object",
    "properties": {
        "cells": {
            "type": "array",
            "description": ("Code cells in execution order, with their inline "
                            "outputs. Non-code cells are omitted."),
            "items": {
                "type": "object",
                "properties": {
                    "cell_id": {"type": "string"},
                    "cell_type": {"type": "string",
                                  "enum": ["code", "markdown", "raw"]},
                    "outputs": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["cell_id", "cell_type", "outputs"],
                "additionalProperties": False,
            },
        },
        "truncated": {
            "type": "boolean",
            "description": ("True if the inline outputs exceeded the per-call "
                            "budget and were cut. On-disk outputs are complete; "
                            "use `nb_read` to recover them."),
        },
        "saved": {
            "type": "boolean",
            "description": ("True if the notebook file was written by this call. "
                            "False when nothing ran, validation failed, or the "
                            "write failed."),
        },
        "error": _error_schema(["invalid_source", "cell_execution_failed",
                                "kernel_died", "sage_not_found", "not_found",
                                "invalid_notebook", "unsupported_magic",
                                "write_failed", "internal"]),
    },
    "required": ["cells", "truncated", "saved", "error"],
    "additionalProperties": False,
}

OUTPUT_SCHEMA_NB_WRITE = {
    "type": "object",
    "properties": {
        "cell_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Cell IDs assigned to the written cells, in order.",
        },
        "saved": {
            "type": "boolean",
            "description": "True iff the notebook file was written by this call.",
        },
        "error": _error_schema(["invalid_source", "exists", "invalid_notebook",
                                "unsupported_magic", "write_failed", "internal"]),
    },
    "required": ["cell_ids", "saved", "error"],
    "additionalProperties": False,
}

OUTPUT_SCHEMA_NB_READ = {
    "type": "object",
    "properties": {
        "nbformat": {
            "type": ["integer", "null"],
            "description": ("The major nbformat version stored on disk; null on "
                            "failure."),
        },
        "nbformat_minor": {
            "type": ["integer", "null"],
            "description": ("The minor nbformat version stored on disk (4 for a "
                            "file upgraded in memory); null on failure."),
        },
        "cells": {
            "type": "array",
            "description": ("The selected cells, in the requested order (or "
                            "notebook order). Empty on failure."),
            "items": {
                "type": "object",
                "properties": {
                    "cell_id": {"type": "string"},
                    "cell_type": {"type": "string",
                                  "enum": ["code", "markdown", "raw"]},
                    "source": {"type": "string"},
                    "outputs": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": ("Only on code cells: the full nbformat "
                                        "outputs stored on disk."),
                    },
                },
                "required": ["cell_id", "cell_type", "source"],
                "additionalProperties": False,
            },
        },
        "error": _error_schema(["not_found", "invalid_notebook",
                                "unknown_cell_id", "internal"]),
    },
    "required": ["nbformat", "nbformat_minor", "cells", "error"],
    "additionalProperties": False,
}

OUTPUT_SCHEMA_NB_EDIT = {
    "type": "object",
    "properties": {
        "applied": {
            "type": "array",
            "description": ("One entry per op that was applied, in order. Empty if "
                            "the call failed (nothing was written)."),
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string",
                           "enum": ["edit", "add", "remove"]},
                    "cell_id": {"type": "string"},
                },
                "required": ["op", "cell_id"],
                "additionalProperties": False,
            },
        },
        "saved": {
            "type": "boolean",
            "description": "True iff the notebook file was written by this call.",
        },
        "error": _error_schema(["invalid_source", "not_found",
                                "invalid_notebook", "unsupported_magic",
                                "unknown_cell_id", "invalid_op",
                                "write_failed", "internal"]),
    },
    "required": ["applied", "saved", "error"],
    "additionalProperties": False,
}


def _exec_out(*, result: str = "", stdout: str = "", stderr: str = "",
              truncated: bool = False, error: dict | None = None) -> dict:
    """The only constructor of an `exec` result. Keys match OUTPUT_SCHEMA_EXEC."""
    return {"result": result, "stdout": stdout, "stderr": stderr,
            "truncated": truncated, "error": error}


def _nb_run_out(*, cells: list | None = None, truncated: bool = False,
                saved: bool = False, error: dict | None = None) -> dict:
    """The only constructor of an `nb_run` result."""
    return {"cells": [] if cells is None else cells, "truncated": truncated,
            "saved": saved, "error": error}


def _nb_write_out(*, cell_ids: list | None = None, saved: bool = False,
                  error: dict | None = None) -> dict:
    """The only constructor of an `nb_write` result."""
    return {"cell_ids": [] if cell_ids is None else cell_ids,
            "saved": saved, "error": error}


def _nb_read_out(*, nbformat: int | None = None,
                 nbformat_minor: int | None = None,
                 cells: list | None = None,
                 error: dict | None = None) -> dict:
    """The only constructor of an `nb_read` result."""
    return {"nbformat": nbformat, "nbformat_minor": nbformat_minor,
            "cells": [] if cells is None else cells, "error": error}


def _nb_edit_out(*, applied: list | None = None, saved: bool = False,
                 error: dict | None = None) -> dict:
    """The only constructor of an `nb_edit` result."""
    return {"applied": [] if applied is None else applied,
            "saved": saved, "error": error}


def _interrupt_out(*, interrupted: bool = False,
                   error: dict | None = None) -> dict:
    """The only constructor of an `interrupt` result."""
    return {"interrupted": interrupted, "error": error}


def _restart_out(*, restarted: bool = False, error: dict | None = None) -> dict:
    """The only constructor of a `restart` result."""
    return {"restarted": restarted, "error": error}


# --- MCP ---------------------------------------------------------------------

@lifespan
async def sage_lifespan(server):
    """Hold the one kernel. Nothing starts at connect; shutdown stops it."""
    state = {"km": None, "kc": None, "busy": False}
    try:
        yield state
    finally:
        await asyncio.to_thread(_stop_kernel, state)


mcp = FastMCP("SageMath", lifespan=sage_lifespan)


@mcp.tool(name="exec", output_schema=OUTPUT_SCHEMA_EXEC)
async def sage_exec(
        ctx: Context,
        code: Annotated[str, Field(
            description="Sage source to execute. It is preparsed before it is "
                        "sent to the kernel.")],
        timeout: Annotated[float | None, Field(
            gt=0,
            description="Seconds to wait for this call. null uses the server's "
                        "2400 s default. On expiry the call returns `timeout`, "
                        "the kernel keeps computing and the namespace is kept."),
        ] = None) -> dict:
    """Run Sage code in the agent's single kernel namespace and return its output.

    State: variables persist across calls in one namespace, until `restart` or
    kernel death. The kernel starts on the first call.
    Never: it does not start a second kernel, touch files, or sandbox the code
    (the kernel runs with the user's permissions).
    Output: `result` is repr() of a trailing expression ("" if there is none or
    it is None); `stdout` and `stderr` are separate and never folded together.
    Truncation: the inline payload (`result` + `stdout` + `stderr`) is cut at the
    server's per-call budget of 20000 characters; the untruncated text is not
    recoverable, so keep individual calls small.
    `timeout` (seconds, default 2400) bounds this call only. On expiry the call
    returns error code `timeout`; the kernel keeps computing and the namespace
    is kept, so call `interrupt` to stop the work or `restart` to discard it.
    """
    state = ctx.lifespan_context
    try:
        shaped = await _locked(ctx, _exec_locked, state, code, timeout)
    except SourceError as e:
        return _exec_out(error=_err(
            "invalid_source",
            "The source did not preparse into valid Sage code.", e))
    except ExecTimeout as e:
        return _exec_out(error=_err(
            "timeout",
            f"The call exceeded its {e.seconds:g} s budget; the kernel keeps "
            "computing and the namespace is kept.", e))
    except GapCrashLoop as e:
        return _exec_out(**_exec_view(e.outputs), error=_err(
            "gap_crash_loop",
            "GAP crashed repeatedly; the call was interrupted to stop a "
            "runaway process.", e.stderr))
    except KernelDied:
        return _exec_out(error=_err(
            "kernel_died", "The Sage kernel died during the call.",
            "the namespace was lost; a new kernel starts on the next call"))
    except SageNotFound as e:
        return _exec_out(error=_err("sage_not_found", str(e)))
    except Exception as e:
        return _exec_out(error=_err(
            "internal", "Unexpected failure inside the server.",
            f"{type(e).__name__}: {e}"))
    return _exec_out(**shaped)


@mcp.tool(name="nb_run", output_schema=OUTPUT_SCHEMA_NB_RUN)
async def sage_nb_run(
        ctx: Context,
        path: Annotated[str, Field(
            description="Filesystem path to the .ipynb notebook to run.")]) -> dict:
    """Run every code cell of a notebook in a fresh kernel and write its outputs.

    State: the kernel is restarted first, so every variable from prior `exec` or
    `nb_run` calls is discarded; afterwards it stays alive and warm with the
    notebook's variables up to and including the last executed cell. The
    notebook file at `path` is rewritten in place with the outputs and
    execution_counts.
    Never: it does not create a file (a missing path is `not_found`), does not
    start a second kernel, and does not raise for a domain failure. Load and
    static validation happen *before* the restart, so a notebook with a bad
    schema, a duplicate id, a magic, or a syntax error leaves both the file and
    the running namespace untouched. Execution stops at the first cell that
    raises; cells up to and including it are written and later cells keep what
    was already on disk.
    Truncation: the returned inline outputs are cut at the per-call budget, with
    `truncated:true` and the cut cells returned as `outputs:[]`. The file always
    holds the full outputs; read them back with `nb_read`.
    """
    state = ctx.lifespan_context
    async with _nb_lock:
        try:
            nb, _, _ = _load_nb(path)
            _validate_cells(nb)
        except NotebookError as e:
            return _nb_run_out(error=_err(e.code, e.message, e.details))
        except SageNotFound as e:
            return _nb_run_out(error=_err("sage_not_found", str(e)))
        except Exception as e:
            return _nb_run_out(error=_err(
                "internal", "Unexpected failure inside the server.",
                f"{type(e).__name__}: {e}"))
        try:
            cells, truncated, saved, failure = await _locked(
                ctx, _run_nb_locked, state, nb, path)
        except SageNotFound as e:
            return _nb_run_out(error=_err("sage_not_found", str(e)))
        except SourceError as e:
            return _nb_run_out(error=_err(
                "invalid_source", "A code cell did not preparse.", e))
        except KernelDied:
            return _nb_run_out(error=_err(
                "kernel_died", "The Sage kernel died during the run.",
                "the namespace was lost; a new kernel starts on the next call"))
        except NotebookError as e:
            return _nb_run_out(error=_err(e.code, e.message, e.details))
        except Exception as e:
            return _nb_run_out(error=_err(
                "internal", "Unexpected failure inside the server.",
                f"{type(e).__name__}: {e}"))
        error = None
        if failure is not None:
            error = _err(failure.code, failure.message, failure.details)
        return _nb_run_out(cells=cells, truncated=truncated, saved=saved,
                           error=error)


@mcp.tool(name="nb_write", output_schema=OUTPUT_SCHEMA_NB_WRITE)
async def sage_nb_write(
        ctx: Context,
        path: Annotated[str, Field(
            description="Filesystem path to the .ipynb to create.")],
        cells: Annotated[list[dict[str, Any]], Field(
            description="Cells in order, each {cell_type: code|markdown|raw, "
                        "source: str}. Validated here, never trusted.")],
        overwrite: Annotated[bool, Field(
            description="Replace an existing file. Default false.")] = False) -> dict:
    """Create a new notebook from cells, validate it, and write it atomically.

    State: writes the file at `path` at nbformat 4.5, so every cell has an id;
    the ids are returned in `cell_ids` and can go straight into `nb_edit`.
    Metadata is the Sage kernelspec. It never touches the kernel namespace.
    Never: it does not execute anything (use `nb_run`), never creates parent
    directories, and never creates or truncates the file on a validation or
    write failure. Without `overwrite:true`, an existing file returns `exists`
    and is left byte-identical.
    """
    async with _nb_lock:
        try:
            if Path(path).exists() and not overwrite:
                return _nb_write_out(error=_err(
                    "exists", "The notebook already exists; pass overwrite to "
                    "replace it.", str(path)))
            built = []
            for index, spec in enumerate(cells):
                if not isinstance(spec, dict):
                    raise NotebookError(
                        "invalid_notebook", "Each cell must be an object.",
                        f"cell {index}: {type(spec).__name__}")
                extra = set(spec) - {"cell_type", "source"}
                if extra:
                    raise NotebookError(
                        "invalid_notebook", "A cell has an unexpected field.",
                        f"cell {index}: {', '.join(sorted(extra))}")
                cell_type, source = spec.get("cell_type"), spec.get("source")
                if not isinstance(source, str):
                    raise NotebookError(
                        "invalid_notebook", "A cell source must be a string.",
                        f"cell {index}: {type(source).__name__}")
                if cell_type not in ("code", "markdown", "raw"):
                    raise NotebookError(
                        "invalid_notebook", "A cell type is unknown.",
                        f"cell {index}: {cell_type!r}")
                built.append(_new_cell(cell_type, source))
            nb = nbformat.v4.new_notebook(
                metadata=copy.deepcopy(KERNEL_META), cells=built)
            seen: set[str] = set()
            for cell in nb.cells:
                cid = cell.get("id")
                if not isinstance(cid, str) or not cid or cid in seen:
                    cell["id"] = _fresh_id(seen)
                seen.add(cell["id"])
            nbformat.validate(nb)
            _validate_cells(nb)
            _save_nb(nb, path)
            return _nb_write_out(cell_ids=[cell["id"] for cell in nb.cells],
                                 saved=True)
        except NotebookError as e:
            return _nb_write_out(error=_err(e.code, e.message, e.details))
        except SageNotFound as e:
            return _nb_write_out(error=_err(
                "internal", "Sage is needed to validate code cells.", str(e)))
        except Exception as e:
            return _nb_write_out(error=_err(
                "internal", "Unexpected failure inside the server.",
                f"{type(e).__name__}: {e}"))


@mcp.tool(name="nb_read", output_schema=OUTPUT_SCHEMA_NB_READ)
async def sage_nb_read(
        ctx: Context,
        path: Annotated[str, Field(description="Filesystem path to the .ipynb.")],
        cell_ids: Annotated[list[str] | None, Field(
            description="Optional. Return only these cells, in the order given "
                        "here. Unknown ids fail the whole call.")] = None) -> dict:
    """Read and validate a notebook from disk and return its cells.

    State: reads only. No in-memory notebook state is consulted, so every call
    reflects the file; a notebook below nbformat 4.5 is upgraded in memory (ids
    are minted) but the file is left byte-identical and `nbformat`/
    `nbformat_minor` report the on-disk values.
    Never: it does not write, execute, or restart a kernel. An unknown id in
    `cell_ids` returns `unknown_cell_id` with the id in `details`, `cells:[]`,
    and both version fields null.
    Output: code cells carry their full on-disk `outputs`; use this to recover
    the outputs that an `nb_run` inline view truncated.
    """
    try:
        nb, disk_format, disk_minor = _load_nb(path)
        return _nb_read_out(nbformat=disk_format,
                            nbformat_minor=disk_minor,
                            cells=_nb_read_cells(nb, cell_ids))
    except NotebookError as e:
        return _nb_read_out(error=_err(e.code, e.message, e.details))
    except Exception as e:
        return _nb_read_out(error=_err(
            "internal", "Unexpected failure inside the server.",
            f"{type(e).__name__}: {e}"))


@mcp.tool(name="nb_edit", output_schema=OUTPUT_SCHEMA_NB_EDIT)
async def sage_nb_edit(
        ctx: Context,
        path: Annotated[str, Field(description="Filesystem path to the .ipynb.")],
        ops: Annotated[list[dict[str, Any]], Field(
            min_length=1,
            description="Operations in order, each one of {op:edit, cell_id, "
                        "source}, {op:add, source, cell_type?, after?}, or "
                        "{op:remove, cell_id}. Validated here, never trusted.")]
        ) -> dict:
    """Apply edit/add/remove cell ops to an existing notebook atomically.

    State: the ops are applied in memory and the whole result is re-validated;
    only then is the file rewritten once. Cells are addressed by `cell_id`
    (call `nb_read` first); an `add` without `cell_type` defaults to `code` and
    its new id is returned in `applied`.
    Never: it does not create a file (a missing path is `not_found`), does not
    execute the notebook, and never writes on failure: if any op is malformed
    (`invalid_op`), names an unknown id (`unknown_cell_id`), or the result has a
    magic/syntax error, `applied` is empty and the file is byte-identical. The
    error's `details` starts with `op <zero-based index> (<op>):` for an op that
    failed.
    """
    async with _nb_lock:
        try:
            nb, _, _ = _load_nb(path)
            applied = _apply_ops(nb, ops)
            try:
                nbformat.validate(nb)
            except Exception as e:
                raise NotebookError(
                    "invalid_notebook",
                    "The edited notebook is not nbformat-valid.",
                    f"{type(e).__name__}: {e}") from None
            try:
                _validate_cells(nb)
            except NotebookError as e:
                raise _prefix_validation_error(e, applied) from None
            _save_nb(nb, path)
            return _nb_edit_out(applied=applied, saved=True)
        except NotebookError as e:
            return _nb_edit_out(error=_err(e.code, e.message, e.details))
        except SageNotFound as e:
            return _nb_edit_out(error=_err(
                "internal", "Sage is needed to validate code cells.", str(e)))
        except Exception as e:
            return _nb_edit_out(error=_err(
                "internal", "Unexpected failure inside the server.",
                f"{type(e).__name__}: {e}"))


@mcp.tool(name="interrupt", output_schema=OUTPUT_SCHEMA_INTERRUPT)
async def sage_interrupt(ctx: Context) -> dict:
    """Stop the running computation and keep the kernel and its namespace.

    State: the namespace is untouched, so variables defined before the
    interrupted call are still available; the interrupted call returns
    `cell_execution_failed` with `KeyboardInterrupt` in `details`.
    Never: it does not restart the kernel, wipe variables, or wait for the call
    to finish. `interrupted` is true only if the kernel is alive and a call is
    running; on an idle kernel this is a no-op returning false.
    """
    state = ctx.lifespan_context
    try:
        return _interrupt_out(interrupted=_interrupt(state))
    except Exception as e:
        return _interrupt_out(error=_err(
            "internal", "Unexpected failure inside the server.",
            f"{type(e).__name__}: {e}"))


@mcp.tool(name="restart", output_schema=OUTPUT_SCHEMA_RESTART)
async def sage_restart(ctx: Context) -> dict:
    """Restart the Sage kernel, discarding every variable in the namespace.

    State: the old namespace is gone and the new kernel is ready when this
    returns, so the next call starts clean. A running computation is interrupted
    first; if it does not stop within the shutdown timeout the kernel is killed
    and respawned, and that call ends with `kernel_died`.
    Never: it does not leave the kernel stopped, and a failure to resolve Sage or
    start the kernel is reported in `error` (`restarted:false`), not raised.
    """
    state = ctx.lifespan_context
    try:
        await _restart(state)
    except SageNotFound as e:
        return _restart_out(error=_err("sage_not_found", str(e)))
    except KernelDied as e:
        return _restart_out(error=_err(
            "kernel_died", "The new kernel did not become ready.", e))
    except Exception as e:
        return _restart_out(error=_err(
            "internal", "Unexpected failure inside the server.",
            f"{type(e).__name__}: {e}"))
    return _restart_out(restarted=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SageMath MCP server.")
    parser.add_argument("--directory", metavar="DIR",
                        help="start the kernel in this directory")
    args = parser.parse_args()
    if args.directory:
        os.chdir(args.directory)
    mcp.run()
