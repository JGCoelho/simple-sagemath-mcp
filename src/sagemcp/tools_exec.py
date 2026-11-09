"""The job tools: `exec` runs Sage code as a job, `job_status` reads a job.

`exec` (and `run_file`, next) start a job with `kernel._start_job` and wait at
most their `timeout`. A finished job returns the envelope it recorded; a job
still running at the wait returns the `running` envelope and keeps the kernel.
`_failure_out` maps every failure of a job onto the shared error codes.
"""

import functools
import math
from typing import Annotated

from fastmcp import Context
from pydantic import Field

from . import constants, jobs, kernel
from .app import mcp
from .envelope import (OUTPUT_SCHEMA_EXEC, OUTPUT_SCHEMA_JOB_STATUS, _err,
                       _exec_out, _exec_running_out, _job_status_out,
                       _kernel_busy_err)
from .errors import (GapCrashLoop, KernelBusy, KernelDied, SageNotFound,
                     SourceError)
from .notebook_run import _clean_traceback
from .shaping import _exec_view, _summarise


# --- jobs --------------------------------------------------------------------

def _run_locked(state: dict, code: str, filename: str, outs: list, *,
                full_traceback: bool) -> dict:
    """The job body, kernel lock held: ensure a kernel, run `code`, shape the envelope.

    A user exception becomes `cell_execution_failed`. `full_traceback` picks its
    details: the `Ename: evalue` line (exec), or the cleaned traceback, which
    names the file (run_file). The other failures are raised for `_failure_out`.
    """
    kernel._ensure_kernel(state)
    # No budget: the wait belongs to the request. The job runs until the kernel
    # is idle, is interrupted, or is restarted (see `_run_source`).
    kernel._run_source(state, code, filename, timeout=math.inf, outs=outs)
    view = _exec_view(outs)
    detail = _summarise(outs)[3]
    if detail is None:
        return _exec_out(**view)
    if full_traceback:
        detail = _traceback_text(outs, detail)
    return _exec_out(**view, error=_err(
        "cell_execution_failed", "The source raised an exception.", detail))


def _traceback_text(outs: list[dict], fallback: str) -> str:
    """The first error output as plain text, or `fallback` if it has none."""
    for out in outs:
        if out.get("output_type") == "error":
            return _clean_traceback(out.get("traceback") or []) or fallback
    return fallback


def _failure_out(exc: BaseException, outs: list[dict]) -> dict:
    """Map a job's exception to the envelope `exec` has always returned for it."""
    if isinstance(exc, SourceError):
        return _exec_out(error=_err(
            "invalid_source",
            "The source did not preparse into valid Sage code.", exc))
    if isinstance(exc, GapCrashLoop):
        return _exec_out(**_exec_view(exc.outputs), error=_err(
            "gap_crash_loop",
            "GAP crashed repeatedly; the call was interrupted to stop a "
            "runaway process.", exc.stderr))
    if isinstance(exc, KernelDied):
        return _exec_out(error=_err(
            "kernel_died", "The Sage kernel died during the call.",
            "the namespace was lost; a new kernel starts on the next call"))
    if isinstance(exc, SageNotFound):
        return _exec_out(error=_err("sage_not_found", str(exc)))
    return _exec_out(error=_err(
        "internal", "Unexpected failure inside the server.",
        f"{type(exc).__name__}: {exc}"))


async def _job_call(ctx: Context, work, timeout: float | None) -> dict:
    """Start `work` as a job and wait up to `timeout` for its envelope.

    Finished in time: the job's envelope, exactly as recorded. Still running at
    the wait: the `running` envelope with the output so far, and the job keeps
    running. A job that cannot start because the kernel is held gets `kernel_busy`.
    """
    state = ctx.lifespan_context
    try:
        job = kernel._start_job(state, work, _failure_out)
    except KernelBusy as e:
        return _exec_out(error=_kernel_busy_err(e.job_id))
    budget = constants.KERNEL_READ_TIMEOUT if timeout is None else timeout
    if await kernel._wait_job(ctx, job, budget):
        return job.result
    view = _exec_view(list(job.outputs))
    return _exec_running_out(job_id=job.job_id, elapsed=job.elapsed(),
                             result=view["result"], stdout=view["stdout"],
                             stderr=view["stderr"], truncated=view["truncated"])


# --- exec --------------------------------------------------------------------

@mcp.tool(name="exec", output_schema=OUTPUT_SCHEMA_EXEC)
async def sage_exec(
        ctx: Context,
        code: Annotated[str, Field(
            description="Sage source to execute. It is preparsed before it is "
                        "sent to the kernel.")],
        timeout: Annotated[float | None, Field(
            gt=0,
            description="Seconds to wait for this call. null uses the server's "
                        "2400 s default. On expiry the call returns status "
                        "`running` and a job_id; the job keeps running."),
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
    Jobs: the call runs as a job. `timeout` (seconds, default 2400) bounds how
    long this call waits, not the job. On expiry it returns status `running`, a
    `job_id`, `elapsed` and the output so far, and the job keeps running. Poll
    `job_status` with the id, or stop it with `interrupt` or `restart`. While a
    job holds the kernel, exec, run_file and nb_* calls return `kernel_busy`.
    Plots: a Sage `Graphics` reprs as PGF/LaTeX, not an image; save with
    `P.save('p.png')` (relative to the kernel cwd, not the notebook) and
    `display(Image(filename='p.png'))` in a notebook.
    Errors: invalid_source, cell_execution_failed, gap_crash_loop, kernel_died,
    sage_not_found, internal, kernel_busy.
    """
    work = functools.partial(_run_locked, ctx.lifespan_context, code,
                             "<exec>", full_traceback=False)
    return await _job_call(ctx, work, timeout)


# --- job_status --------------------------------------------------------------

@mcp.tool(name="job_status", output_schema=OUTPUT_SCHEMA_JOB_STATUS)
async def sage_job_status(
        ctx: Context,
        job_id: Annotated[str, Field(
            description="The job_id that exec or run_file returned with status "
                        "`running`.")]) -> dict:
    """Report a job started by exec or run_file: its status and its output so far.

    State: reads the job record only. It never waits and never touches the kernel.
    `status` is `running`, `done` (finished without error), `error` (finished with
    an error result) or `interrupted` (a restart ended it). While running, stdout
    and stderr are the output so far. Once finished, `result`, stdout, stderr,
    truncated and error are the final values, exactly as exec returns them.
    Never: it does not cancel the job; `interrupt` or `restart` do that.
    Records of the most recent finished jobs are kept in memory and a server
    restart drops them; an id that is unknown or dropped returns `unknown_job`.
    Errors: invalid_source, cell_execution_failed, gap_crash_loop, kernel_died,
    sage_not_found, internal, unknown_job.
    """
    try:
        job = jobs.find(ctx.lifespan_context, job_id)
        if job is None:
            return _job_status_out(error=_err(
                "unknown_job",
                "No job has this id; it may have expired, or the server restarted.",
                job_id))
        if job.result is None:
            view = _exec_view(list(job.outputs))
            return _job_status_out(status=job.status, elapsed=job.elapsed(),
                                   stdout=view["stdout"], stderr=view["stderr"],
                                   truncated=view["truncated"])
        return _job_status_out(status=job.status, elapsed=job.elapsed(),
                               **job.result)
    except Exception as e:
        return _job_status_out(error=_err(
            "internal", "Unexpected failure inside the server.",
            f"{type(e).__name__}: {e}"))
