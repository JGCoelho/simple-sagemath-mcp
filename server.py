#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["fastmcp", "nbformat", "jupyter_client", "ipykernel"]
# ///
# Run:      python server.py     (the python of your Sage install)
# Install:  sage -pip install .  (or, from git: sage -pip install
#           git+https://github.com/JGCoelho/simple-sagemath-mcp)
# Or:       ./install.sh         (no pip: checks the python, installs what is
#                                missing, and prints the config block)

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

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))

from sagemcp import constants
from sagemcp.errors import (ExecTimeout, GapCrashLoop, KernelDied,
                            NotebookError, SAGE_NOT_FOUND, SageNotFound,
                            SourceError)
from sagemcp.kernel import (_ensure_kernel, _interrupt, _locked, _nb_lock,
                            _restart, _restart_locked, _run_source,
                            _stop_kernel)
from sagemcp.notebook_io import (_apply_ops, _fresh_id, _load_nb,
                                 _new_cell, _nb_read_cells,
                                 _prefix_validation_error, _save_nb,
                                 _validate_cells, nbformat)
from sagemcp.shaping import _exec_view, _summarise, _truncate

import argparse
import asyncio
import copy
import json
import os
import re
from typing import Annotated, Any

from fastmcp import FastMCP, Context
from fastmcp.server.lifespan import lifespan
from pydantic import Field


# --- output shaping ----------------------------------------------------------


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


# IPython colours its tracebacks, and the frames it shows for the generated
# wrapper (`Cell In[n], line k`) contain the server's source, not the user's.
# Both are noise in `error.details`, which an agent has to read as plain text.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_WRAPPER_FRAME_MARKERS = ("_mcp_last", "_mcp_value", "_mcp_tree", "_mcp_ns",
                          "_mcp_seed", "_mcp_display", "__mcp_ns__")


def _clean_traceback(lines: list) -> str:
    """Return a traceback as plain text, without the wrapper's own frames.

    Drops the ANSI escapes and every traceback entry that mentions a name from
    the generated wrapper, keeping the exception header, the frames in the
    user's file and the library frames, and the final `Ename: evalue` line.
    The notebook's on-disk outputs are untouched: only `error.details` is
    cleaned. Pure, and never raises.
    """
    kept: list[str] = []
    for line in lines:
        text = _ANSI_RE.sub("", str(line))
        if any(marker in text for marker in _WRAPPER_FRAME_MARKERS):
            continue
        kept.append(text.rstrip())
    # The header separator is decoration once the colours are gone.
    while kept and (not kept[0].strip() or set(kept[0].strip()) == {"-"}):
        kept.pop(0)
    return "\n".join(kept).strip()


def _cell_error_detail(cell_id: str, outs: list[dict]) -> str:
    """Build `"<cell_id>: <traceback or Ename: evalue>"`. Pure."""
    for out in outs:
        if out.get("output_type") == "error":
            traceback = out.get("traceback") or []
            fallback = f"{out.get('ename')}: {out.get('evalue')}"
            text = _clean_traceback(traceback) or fallback
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
    inline, truncated = _inline_cells(executed, constants.MAX_INLINE_CHARS)
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
        "description": ("null on success; on failure {code, message, details}. "
                        "`details` is a traceback, an offending source "
                        "fragment, or an op/cell identifier, or null."),
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
            "description": ("repr() of the trailing expression; empty string if "
                            "the source is statements-only or the value is None."),
        },
        "stdout": {
            "type": "string",
            "description": "Anything printed to stdout.",
        },
        "stderr": {
            "type": "string",
            "description": "Anything written to stderr; never folded into stdout.",
        },
        "truncated": {
            "type": "boolean",
            "default": False,
            "description": ("True if result+stdout+stderr was cut at the per-call "
                            "budget; the untruncated text is not recoverable."),
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
            "description": ("True if a running call was sent an interrupt; false "
                            "if idle or no kernel. After `timeout`, the abandoned "
                            "call keeps this true until it completes or the kernel "
                            "restarts."),
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
            "description": "True iff a new ready kernel is running after this call.",
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
            "description": ("Code cells in execution order with inline outputs; "
                            "non-code cells omitted."),
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
            "description": ("True if inline outputs were cut; full outputs are "
                            "on disk (recover with `nb_read`)."),
        },
        "saved": {
            "type": "boolean",
            "description": "True iff the notebook file was written by this call.",
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
            "description": ("Selected cells in the requested order (or notebook "
                            "order); empty on failure."),
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
            "description": ("Ops applied in order; empty if the call failed."),
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
    `truncated:true`; the cell that overflows and every later code cell come
    back as `outputs:[]`. The file always holds the full outputs; read them back
    with `nb_read`.
    Plots: end the cell with `P.save('p.png')` (relative to the kernel cwd) + `display(Image(filename='p.png'))`; a bare `Graphics` reprs as a PGF/LaTeX blob, not an image.
    Errors: invalid_source, cell_execution_failed, kernel_died, sage_not_found,
    not_found, invalid_notebook, unsupported_magic, write_failed, internal.
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
            description="Cells in order, each {cell_type, source}.",
            json_schema_extra={
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "cell_type": {"type": "string",
                                      "enum": ["code", "markdown", "raw"]},
                        "source": {"type": "string"},
                    },
                    "required": ["cell_type", "source"],
                }
            },
        )],
        overwrite: Annotated[bool, Field(
            description="Replace an existing file. Default false.")] = False) -> dict:
    """Create a new notebook from cells, validate it, and write it atomically.

    State: writes the file at `path` at nbformat 4.5, so every cell has an id;
    the ids are returned in `cell_ids` and can go straight into `nb_edit`.
    Metadata is the Sage kernelspec. It never touches the kernel namespace.
    Never: it never creates parent directories, and never creates or truncates
    the file on a validation or write failure. Without `overwrite:true`,
    an existing file returns `exists` and is left byte-identical; with
    `overwrite:true` the cells are rewritten and get fresh ids.
    Errors: invalid_source, exists, invalid_notebook, unsupported_magic,
    write_failed, internal.
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
                metadata=copy.deepcopy(constants.KERNEL_META), cells=built)
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
    are minted); the file is not written and `nbformat`/`nbformat_minor` report
    on-disk values.
    Never: it does not write, execute, or restart a kernel. An unknown id in
    `cell_ids` returns `unknown_cell_id` with the id in `details`, `cells:[]`,
    and both version fields null.
    Output: code cells carry their full on-disk `outputs`; use this to recover
    the outputs that an `nb_run` inline view truncated. A host may truncate a
    very large result and name a file that holds the full text.
    Errors: not_found, invalid_notebook, unknown_cell_id, internal.
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
                        "source}, {op:add, source, cell_type?, after?} (`after` "
                        "is an existing cell id; omit it to append; `add` takes "
                        "no cell_id), or {op:remove, cell_id}. Validated here, "
                        "never trusted.")]
        ) -> dict:
    """Apply edit/add/remove cell ops to an existing notebook atomically.

    State: the ops are applied in memory and the whole result is re-validated;
    only then is the file rewritten once. Cells are addressed by `cell_id`
    (call `nb_read` first); an `add` without `cell_type` defaults to `code`,
    takes `after` as an existing cell id (omit it to append), and its new id is
    returned in `applied`.
    Never: it does not create a file (a missing path is `not_found`), does not
    execute the notebook, and never writes on failure: if any op is malformed
    (`invalid_op`), names an unknown id (`unknown_cell_id`), or the result has a
    magic/syntax error, `applied` is empty and the file is byte-identical. The
    error's `details` starts with `op <zero-based index> (<op>):` for an op that
    failed.
    Errors: invalid_source, not_found, invalid_notebook, unsupported_magic,
    unknown_cell_id, invalid_op, write_failed, internal.
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
    Errors: internal.
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
    Errors: sage_not_found, kernel_died, internal.
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


def main(argv: list[str] | None = None) -> None:
    """Run the server on stdio. Parse `argv` (default: the command line).

    `--directory` changes the working directory before the kernel starts, so
    relative paths in the agent's code point at the agent's work. This is the
    `simple-sagemath-mcp` console-script entry point.
    """
    parser = argparse.ArgumentParser(description="SageMath MCP server.")
    parser.add_argument("--directory", metavar="DIR",
                        help="start the kernel in this directory")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.directory:
        os.chdir(args.directory)
    mcp.run()


if __name__ == "__main__":
    main()
