#!/usr/bin/env python3
# Run:      python server.py     (the python of your Sage install, see install.sh)
# Install:  ./install.sh

import asyncio
import json
import os
import time
from dataclasses import dataclass, fields
from pathlib import Path
from queue import Empty

from fastmcp import FastMCP, Context
from fastmcp.server.lifespan import lifespan
from jupyter_client import KernelManager
from jupyter_client.kernelspec import KernelSpec
from jupyter_client.blocking import BlockingKernelClient
import nbformat


# --- config ------------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    kernel_read_timeout: float = 2400.0
    heartbeat_interval: float = 15.0
    shell_drain_timeout: float = 30.0
    kernel_ready_timeout: float = 60.0

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        known = {f.name for f in fields(cls)}
        data = {}
        if path is None:
            path = Path(__file__).with_suffix(".json")
        if path.exists():
            data.update({k: v for k, v in json.loads(path.read_text()).items()
                         if k in known})
        for name in known:
            env = os.environ.get(f"SAGE_MCP_{name.upper()}")
            if env is not None:
                data[name] = float(env)
        return cls(**data)     

CFG = Config.load()


# --- kernel ------------------------------------------------------------------

_kernel_lock = asyncio.Lock()

def _start_kernel() -> tuple[KernelManager, BlockingKernelClient]:
    import sys
    # The kernel must be Sage. SAGE_PYTHON lets the server run on another python.
    py = os.environ.get("SAGE_PYTHON") or sys.executable
    km = KernelManager(kernel_name="python3")
    km._kernel_spec = KernelSpec(
        argv=[py, "-m", "ipykernel_launcher",
              "-f", "{connection_file}"],
        display_name="SageMath (conda)",
        language="sage",
    )
    # Put the bin directory of the kernel python first. Sage finds Singular and
    # other programs there.
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(py) + os.pathsep + env.get("PATH", "")
    km.start_kernel(env=env)
    kc = km.blocking_client()
    try:
        kc.start_channels()
        kc.wait_for_ready(timeout=CFG.kernel_ready_timeout)
    except BaseException:
        # Do not leave a live kernel and its channels behind.
        for fn in (kc.stop_channels, lambda: km.shutdown_kernel(now=True)):
            try:
                fn()
            except Exception:
                pass
        raise
    return km, kc


def _reset_kernel(state: dict) -> None:
    km, kc = state["km"], state["kc"]
    for fn in (kc.stop_channels, lambda: km.shutdown_kernel(now=True)):
        try:
            fn()
        except Exception:
            pass
    state["km"], state["kc"] = _start_kernel()


# --- execution ---------------------------------------------------------------

# The server has one kernel. Each session name has one namespace in this kernel.
# Do not use ctx.session_id: with the default protocol it changes on each call.

def _execute_sync(state: dict, code: str, session: str = "default",
                  filename: str = "<sage_exec>") -> list[dict]:
    """Run code in one session namespace. Return the outputs in notebook order."""
    km, kc = state["km"], state["kc"]
    if not km.is_alive():
        _reset_kernel(state)
        km, kc = state["km"], state["kc"]

    ns = f"__mcp_sage_ns__{session}"
    wrapper = f"""
if {ns!r} not in globals():
    _mcp_seed = {{}}
    exec("from sage.all import *", _mcp_seed)
    globals()[{ns!r}] = _mcp_seed
import ast, sage.repl.preparse as _mcp_preparse
_mcp_ns = globals()[{ns!r}]
_mcp_tree = ast.parse(_mcp_preparse.preparse({code!r}), {filename!r})
if _mcp_tree.body and isinstance(_mcp_tree.body[-1], ast.Expr):
    _mcp_last = ast.Expression(_mcp_tree.body.pop().value)
    exec(compile(_mcp_tree, {filename!r}, "exec"), _mcp_ns, _mcp_ns)
    _mcp_val = eval(compile(_mcp_last, {filename!r}, "eval"), _mcp_ns, _mcp_ns)
    if _mcp_val is not None:
        from IPython.display import display as _mcp_display
        _mcp_data = {{"text/plain": repr(_mcp_val)}}
        # Sage objects give LaTeX and HTML text. Use them when a notebook shows the value.
        for _mcp_mime, _mcp_attr, _mcp_fmt in (("text/latex", "_latex_", "$%s$"),
                                               ("text/html", "_html_", "%s")):
            _mcp_fn = getattr(_mcp_val, _mcp_attr, None)
            if _mcp_fn is not None:
                try:
                    _mcp_data[_mcp_mime] = _mcp_fmt % _mcp_fn()
                except Exception:
                    pass
        _mcp_display(_mcp_data, raw=True, metadata={{"_mcp_trailing": True}})
else:
    exec(compile(_mcp_tree, {filename!r}, "exec"), _mcp_ns, _mcp_ns)
"""

    deadline = time.monotonic() + CFG.kernel_read_timeout
    outs: list[dict] = []
    try:
        msg_id = kc.execute(wrapper, allow_stdin=False)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"sage_exec exceeded {CFG.kernel_read_timeout}s")
            try:
                msg = kc.get_iopub_msg(timeout=min(remaining, 5.0))
            except Empty:
                # A dead kernel never sends the idle message. Do not wait for it.
                if not km.is_alive():
                    raise RuntimeError("the Sage kernel died during execution") from None
                continue
            if msg.get("parent_header", {}).get("msg_id") != msg_id:
                continue
            t = msg["header"]["msg_type"]
            c = msg["content"]
            if t == "stream":
                outs.append({"output_type": "stream", "name": c.get("name", "stdout"),
                             "text": c.get("text", "")})
            elif t in ("execute_result", "display_data"):
                md = dict(c.get("metadata") or {})
                # The wrapper marks the value of the last expression.
                kind = "execute_result" if md.pop("_mcp_trailing", False) else t
                outs.append({"output_type": kind, "data": c.get("data") or {},
                             "metadata": md})
            elif t == "error":
                outs.append({"output_type": "error", "ename": c.get("ename"),
                             "evalue": c.get("evalue"),
                             "traceback": c.get("traceback") or []})
            elif t == "status" and c.get("execution_state") == "idle":
                break
    except TimeoutError:
        # The kernel is alive and busy. Keep the variables for the next call.
        raise
    except Exception as e:
        _reset_kernel(state)
        raise RuntimeError(
            f"the Sage kernel failed ({type(e).__name__}: {e}). "
            "The kernel restarted. All sessions lost."
        ) from e

    try:
        drain_deadline = time.monotonic() + CFG.shell_drain_timeout
        while True:
            r = kc.get_shell_msg(timeout=max(0.0, drain_deadline - time.monotonic()))
            if r.get("parent_header", {}).get("msg_id") == msg_id:
                break
    except Exception:
        pass

    return outs


def _summarise(outs: list[dict]) -> tuple[str, str | None, str | None]:
    """Make a short text view of the outputs: stdout, last value, error."""
    stdout = "".join(o["text"] for o in outs if o["output_type"] == "stream")
    result = next((o["data"]["text/plain"] for o in reversed(outs)
                   if "text/plain" in o.get("data", {})), None)
    error = next((f"{o['ename']}: {o['evalue']}"
                  for o in outs if o["output_type"] == "error"), None)
    return stdout, result, error


# --- notebooks ---------------------------------------------------------------

KERNEL_META = {
    "kernelspec": {"name": "sagemath", "display_name": "SageMath", "language": "sage"},
    "language_info": {"name": "sage"},
}


def _load_nb(path: str):
    """Read a notebook and check it against the nbformat schema."""
    nb = nbformat.reads(Path(path).read_text(), as_version=4)
    nbformat.validate(nb)
    return nb


def _save_nb(nb, path: str) -> None:
    """Write a notebook. Use a temp file, so a bad write cannot break the notebook."""
    tmp = Path(str(path) + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        nbformat.write(nb, f)
    os.replace(tmp, path)


def _check_magics(index: int, source: str) -> None:
    """Reject IPython magics. The kernel runs plain Sage code, not magics."""
    for line in source.splitlines():
        if line.lstrip().startswith(("%", "!")):
            raise ValueError(f"cell {index}: IPython magics are not supported: "
                             f"{line.strip()}")


async def _run_nb(ctx: Context, nb, path: str, session: str,
                  indices: list[int] | None = None) -> list[dict]:
    """Run the code cells in one namespace and write the outputs into the notebook.

    Stop at the first error.
    """
    count = max([c.get("execution_count") or 0 for c in nb.cells] + [0])
    report = []
    for i, cell in enumerate(nb.cells):
        if cell.cell_type != "code":
            continue
        if indices is not None and i not in indices:
            continue
        _check_magics(i, cell.source)
        try:
            outs = await _invoke(ctx, _execute_sync, cell.source, session,
                                 f"{path}:cell {i}")
        except Exception as e:
            report.append({"cell": i, "error": f"{type(e).__name__}: {e}"})
            break
        count += 1
        for o in outs:
            if o["output_type"] == "execute_result":
                o["execution_count"] = count
        cell.outputs = [nbformat.from_dict(o) for o in outs]
        cell.execution_count = count
        stdout, result, error = _summarise(outs)
        report.append({"cell": i, "stdout": stdout, "result": result, "error": error})
        if error:
            break
    if report:
        _save_nb(nb, path)
    return report


# --- MCP ---------------------------------------------------------------------

@lifespan
async def sage_lifespan(server):
    # The kernel starts on the first call. Nothing starts at connect time.
    state = {"km": None, "kc": None}
    try:
        yield state
    finally:
        if state["km"] is not None:
            await asyncio.to_thread(state["kc"].stop_channels)
            await asyncio.to_thread(state["km"].shutdown_kernel, now=True)


mcp = FastMCP("SageMath", lifespan=sage_lifespan)


async def _invoke(ctx: Context, fn, *args, **kwargs):
    state = ctx.lifespan_context
    async with _kernel_lock:
        if state["km"] is None:
            state["km"], state["kc"] = await asyncio.to_thread(_start_kernel)
        task = asyncio.create_task(asyncio.to_thread(fn, state, *args, **kwargs))
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=CFG.heartbeat_interval)
            if not done:
                await ctx.report_progress(progress=0, total=None,
                                          message="Sage kernel still running...")
        return await task


@mcp.tool()
async def sage_exec(ctx: Context, code: str, session: str = "default") -> dict:
    """Run Sage code in a persistent kernel. The kernel keeps the variables of the session.

    Use the same session name in each call to keep the same variables.
    """
    outs = await _invoke(ctx, _execute_sync, code, session)
    stdout, result, error = _summarise(outs)
    return {"stdout": stdout, "result": result, "error": error}


@mcp.tool()
async def sage_exec_notebook(ctx: Context, path: str, session: str = "default",
                             cells: list[int] | None = None) -> dict:
    """Run the code cells of a Jupyter notebook in a persistent kernel.

    Write the outputs into the notebook. Stop at the first error. Use `cells` to run
    only some cells. The kernel keeps the variables of the session.
    """
    nb = await asyncio.to_thread(_load_nb, path)
    return {"path": path, "cells": await _run_nb(ctx, nb, path, session, cells)}


@mcp.tool()
async def sage_write_notebook(ctx: Context, path: str, cells: list[str],
                              run: bool = False, session: str = "default",
                              overwrite: bool = False) -> dict:
    """Write a Jupyter notebook that runs Sage code, and validate the result.

    One string is one code cell. Set run=true to run the notebook after the write.
    """
    if Path(path).exists() and not overwrite:
        return {"path": path, "valid": False,
                "error": "the file exists. Set overwrite=true."}
    for i, source in enumerate(cells):
        _check_magics(i, source)
    nb = nbformat.v4.new_notebook(metadata=dict(KERNEL_META),
                                  cells=[nbformat.v4.new_code_cell(s) for s in cells])
    nbformat.validate(nb)
    _save_nb(nb, path)
    if not run:
        return {"path": path, "valid": True, "code_cells": len(cells)}
    # Read the file again. This proves that what is on disk is a valid notebook.
    return {"path": path, "valid": True,
            "cells": await _run_nb(ctx, _load_nb(path), path, session)}


@mcp.tool()
async def sage_interrupt(ctx: Context) -> dict:
    """Stop the running computation. The kernel keeps the variables of the session."""
    state = ctx.lifespan_context
    if state["km"] is None:
        return {"stdout": "", "result": "No kernel is running."}
    try:
        state["km"].interrupt_kernel()
    except Exception as e:
        return {"stdout": "", "result": f"{type(e).__name__}: {e}"}
    return {"stdout": "", "result": "Interrupted."}


if __name__ == "__main__":
    mcp.run()
