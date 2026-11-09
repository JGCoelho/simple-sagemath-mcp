"""The `exec` tool: run Sage code in the kernel namespace.

`_exec_locked` does the work under the kernel lock; `sage_exec` wraps it and
maps every failure type onto the shared error envelope.
"""

from typing import Annotated

from fastmcp import Context
from pydantic import Field

from .app import mcp
from .envelope import OUTPUT_SCHEMA_EXEC, _err, _exec_out
from .errors import (ExecTimeout, GapCrashLoop, KernelDied, SageNotFound,
                     SourceError)
from .kernel import _ensure_kernel, _locked, _run_source
from .shaping import _exec_view, _summarise


# --- exec --------------------------------------------------------------------

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
    Plots: a Sage `Graphics` reprs as PGF/LaTeX, not an image; save with
    `P.save('p.png')` (relative to the kernel cwd, not the notebook) and
    `display(Image(filename='p.png'))` in a notebook.
    Errors: invalid_source, cell_execution_failed, timeout, gap_crash_loop,
    kernel_died, sage_not_found, internal.
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

