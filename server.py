#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["fastmcp", "nbformat", "jupyter_client", "ipykernel"]
# ///
# Run:      python server.py     (the python of your Sage install, see install.sh)
# Install:  ./install.sh

"""A stateful SageMath MCP server: one kernel, one namespace, three tools.

`exec` runs Sage code in the kernel, `interrupt` stops a running computation
without losing it, and `restart` replaces the kernel and wipes the namespace.
The kernel starts on the first call and lives as long as the MCP connection.

This is not a sandbox: the kernel runs the code that the agent sends, with the
user's permissions.
"""

import argparse
import ast
import asyncio
import os
import re
import shutil
import subprocess
import time
from queue import Empty
from typing import Annotated, Any, Callable

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
    _seed_namespace(state)


def _ensure_kernel(state: dict) -> None:
    """Start a kernel if there is none. Keeps a live kernel as it is."""
    if state["km"] is None:
        _start_kernel(state)


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
        await _kernel_lock.acquire()
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
    imports no Sage. Never starts a kernel.
    """
    _run_source(state, "", "<seed>", prepared="")


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
    except ExecTimeout:
        # The kernel is still computing. Keep busy set, so `interrupt` can reach
        # it; the next successful call clears it again.
        timed_out = True
        raise
    finally:
        state["busy"] = timed_out
    _drain_shell(kc, msg_id)
    return outs


# --- output shaping ----------------------------------------------------------


def _summarise(outs: list[dict]) -> tuple[str, str, str | None, str | None]:
    """Pure: make the short text view of outputs.

    Return `(stdout, stderr, last text/plain value or None, "Ename: evalue" or
    None)`. Reads the raw text/plain of the last result, so the caller decides
    on truncation.
    """
    def stream(name):
        return "".join(o["text"] for o in outs
                       if o["output_type"] == "stream" and o.get("name") == name)

    result = next((o["data"]["text/plain"] for o in reversed(outs)
                   if "text/plain" in o.get("data", {})), None)
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
            "description": ("True if a computation was running and has been "
                            "interrupted; false if the kernel was idle or absent."),
        },
        "error": _error_schema(["kernel_died", "internal"]),
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


def _exec_out(*, result: str = "", stdout: str = "", stderr: str = "",
              truncated: bool = False, error: dict | None = None) -> dict:
    """The only constructor of an `exec` result. Keys match OUTPUT_SCHEMA_EXEC."""
    return {"result": result, "stdout": stdout, "stderr": stderr,
            "truncated": truncated, "error": error}


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
