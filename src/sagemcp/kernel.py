"""The Sage kernel: the lock, the lifecycle, the jobs, and the execution loop.

One lock serializes every computation. The kernel starts lazily on the first
call and is seeded with sage.all before it is handed out. Nothing here imports
Sage: only the kernel process does, when it seeds the namespace.
"""

import asyncio
import time
from queue import Empty
from typing import Callable

from fastmcp import Context
from jupyter_client import KernelManager
from jupyter_client.kernelspec import KernelSpec
from jupyter_client.blocking import BlockingKernelClient

from . import constants
from .errors import ExecTimeout, GapCrashLoop, KernelBusy, KernelDied, SageNotFound
from .jobs import Job, new_job, remember, retire
from .sageresolve import _kernel_env, resolve_kernel_python
from .shaping import _summarise
from .source import _prepare, _wrapper


# --- kernel lifecycle --------------------------------------------------------

# One lock serializes every computation. A job holds it from its start to its
# end (`_run_job`), and `_restart` takes it; all other kernel helpers are sync
# and assume the caller holds it.

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
        kc.wait_for_ready(timeout=constants.KERNEL_READY_TIMEOUT)
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
    # A restart ends the running job. Mark it before the interrupt, so the job
    # reports `interrupted` whatever its computation returns on the way out.
    job = state.get("active")
    if job is not None and job.status == "running":
        job.status = "interrupted"
    km = state["km"]
    if km is not None and km.is_alive():
        try:
            km.interrupt_kernel()
        except Exception:
            pass
    try:
        await asyncio.wait_for(_kernel_lock.acquire(), constants.SHUTDOWN_TIMEOUT)
    except asyncio.TimeoutError:
        km = state["km"]
        if km is not None:
            try:
                km.shutdown_kernel(now=True)
            except Exception:
                pass
        try:
            await asyncio.wait_for(_kernel_lock.acquire(), constants.SHUTDOWN_TIMEOUT)
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


# --- jobs --------------------------------------------------------------------

# A job is one asyncio task that owns `_kernel_lock` for its whole run: the lock
# outlives the request that started it, so a call that outlasts its wait keeps
# the kernel. A job runs `work(outputs)` in a thread and records the envelope
# `failure(exc, outputs)` maps an exception to. Jobs never queue: `_claim` is
# checked synchronously before a job is created, so a second call is refused
# with `KernelBusy` while the first holds the kernel.


def _claim(state: dict) -> None:
    """Raise `KernelBusy` unless no job or other holder has the kernel.

    Synchronous, and the caller starts its work with no `await` in between, so
    the check and the start cannot interleave with another call.
    """
    active = state.get("active")
    if active is not None:
        raise KernelBusy(active.job_id)
    if _kernel_lock.locked():
        raise KernelBusy(None)


def _start_job(state: dict, work: Callable[[list], dict],
               failure: Callable[[BaseException, list], dict]) -> Job:
    """Start `work` as a job and return its record at once. Never waits.

    Raises `KernelBusy` when another call holds the kernel. The job record is
    registered before the task starts, so a call made right after sees it.
    """
    _claim(state)
    job = new_job()
    state["active"] = job
    remember(state, job)
    job.task = asyncio.ensure_future(_run_job(state, job, work, failure))
    return job


def _execute(work: Callable[[list], dict],
             failure: Callable[[BaseException, list], dict],
             outs: list) -> dict:
    """Run `work` on the job's outputs in a thread; map a failure to its envelope."""
    try:
        return work(outs)
    except Exception as e:
        return failure(e, outs)


async def _run_job(state: dict, job: Job,
                   work: Callable[[list], dict],
                   failure: Callable[[BaseException, list], dict]) -> None:
    """The job's task: hold the kernel lock, run the work, record the envelope.

    A restart that reaches the job first (`_restart` sets `interrupted`) means the
    work never starts or its result is discarded; either way the record ends as a
    `kernel_died` envelope. The record is final before the lock is released, so
    a restart that waits for the lock sees it finished.
    """
    try:
        async with _kernel_lock:
            try:
                value = None
                if job.status == "running":
                    value = await asyncio.to_thread(_execute, work, failure,
                                                    job.outputs)
                if job.status == "interrupted":
                    value = failure(KernelDied("the kernel was restarted"),
                                    job.outputs)
                else:
                    job.status = "error" if value["error"] else "done"
                job.result = value
            finally:
                if job.result is None:      # cancelled before it could settle
                    job.status = "error"
                    job.result = failure(KernelDied("the job was cancelled"),
                                         job.outputs)
                if state.get("active") is job:
                    state["active"] = None
    finally:
        job.finished = time.monotonic()
        retire(state, job)


async def _wait_job(ctx: Context, job: Job, timeout: float) -> bool:
    """Wait up to `timeout` seconds for `job`; True once it has finished.

    Reports progress every HEARTBEAT_INTERVAL while it waits. Never cancels the
    job: on False the job keeps the kernel, and the caller polls `job_status`.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if job.task.done():
            return True
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.wait({job.task},
                           timeout=min(remaining, constants.HEARTBEAT_INTERVAL))
        if not job.task.done():
            try:
                await ctx.report_progress(progress=0, total=None,
                                          message="Sage kernel still running...")
            except Exception:
                pass


# --- execution ---------------------------------------------------------------

# The kernel has one namespace: the dict `__mcp_ns__` in its globals, seeded
# once with `from sage.all import *`. Helper names in the kernel use the `_mcp_`
# prefix so they do not clutter `dir()` for user code.


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
    deadline = time.monotonic() + constants.SHELL_DRAIN_TIMEOUT
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
                timeout: float | None = None,
                outs: list[dict] | None = None) -> list[dict]:
    """Run one source in the shared namespace and return its outputs in order.

    The single execution path. Marks the state busy for the whole read loop,
    collects stream/execute_result/display_data/error messages for this call's
    message id, drains the shell reply, and guards against a GAP crash loop.
    Raises `SourceError`, `ExecTimeout`, `KernelDied`, or `GapCrashLoop`; on a
    timeout the kernel keeps computing (busy stays set so `interrupt` works).
    A dead kernel is stopped and forgotten before `KernelDied` is raised.
    `outs`, when given, is the list the outputs are appended to as they arrive,
    so a job can read them while it runs. `timeout=math.inf` means no budget:
    the read loop runs until the kernel is idle, which is what a job needs.
    Assumes the lock is held; starts no kernel.
    """
    if prepared is None:
        prepared = _prepare(source)
    budget = constants.KERNEL_READ_TIMEOUT if timeout is None else timeout
    km, kc = state["km"], state["kc"]
    if km is None or kc is None or not km.is_alive():
        _died(state, "the kernel is not running")
    deadline = time.monotonic() + budget
    outs = [] if outs is None else outs
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
                    gap_crashes += len(constants.GAP_CRASH_PATTERN.findall(text))
                    if gap_crashes >= constants.GAP_CRASH_LIMIT:
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
        # it (the flag means "may be computing"; it clears on the next completed
        # call or a restart).
        timed_out = True
        raise
    finally:
        state["busy"] = timed_out
    _drain_shell(kc, msg_id)
    return outs
