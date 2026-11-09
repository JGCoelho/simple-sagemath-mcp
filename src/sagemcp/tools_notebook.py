"""The four notebook tools: `nb_run`, `nb_write`, `nb_read`, `nb_edit`.

Every write goes through `notebook_io`'s atomic replace; the three tools that
mutate the file hold the notebook lock, and `nb_run` holds it across the restart
and the cell execution so an edit cannot race a run. `nbformat` and `Path` stay
module/name imports the tools call directly.
"""

import copy
from pathlib import Path
from typing import Annotated, Any

import nbformat
from fastmcp import Context
from pydantic import Field

from . import constants
from .app import mcp
from .envelope import (OUTPUT_SCHEMA_NB_EDIT, OUTPUT_SCHEMA_NB_READ,
                       OUTPUT_SCHEMA_NB_RUN, OUTPUT_SCHEMA_NB_WRITE, _err,
                       _kernel_busy_err, _nb_edit_out, _nb_read_out,
                       _nb_run_out, _nb_write_out)
from .errors import KernelBusy, KernelDied, NotebookError, SageNotFound, SourceError
from .kernel import _claim, _locked, _nb_lock
from .notebook_io import (_apply_ops, _fresh_id, _load_nb, _new_cell,
                          _nb_read_cells, _prefix_validation_error, _save_nb,
                          _validate_cells)
from .notebook_run import _run_nb_locked


# --- notebook tools ----------------------------------------------------------

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
    not_found, invalid_notebook, unsupported_magic, write_failed, internal,
    kernel_busy.
    """
    state = ctx.lifespan_context
    async with _nb_lock:
        try:
            _claim(state)
        except KernelBusy as e:
            return _nb_run_out(error=_kernel_busy_err(e.job_id))
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
    write_failed, internal, kernel_busy.
    """
    async with _nb_lock:
        try:
            _claim(ctx.lifespan_context)
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
        except KernelBusy as e:
            return _nb_write_out(error=_kernel_busy_err(e.job_id))
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
    Errors: not_found, invalid_notebook, unknown_cell_id, internal, kernel_busy.
    """
    try:
        _claim(ctx.lifespan_context)
        nb, disk_format, disk_minor = _load_nb(path)
        return _nb_read_out(nbformat=disk_format,
                            nbformat_minor=disk_minor,
                            cells=_nb_read_cells(nb, cell_ids))
    except KernelBusy as e:
        return _nb_read_out(error=_kernel_busy_err(e.job_id))
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
    unknown_cell_id, invalid_op, write_failed, internal, kernel_busy.
    """
    async with _nb_lock:
        try:
            _claim(ctx.lifespan_context)
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
        except KernelBusy as e:
            return _nb_edit_out(error=_kernel_busy_err(e.job_id))
        except NotebookError as e:
            return _nb_edit_out(error=_err(e.code, e.message, e.details))
        except SageNotFound as e:
            return _nb_edit_out(error=_err(
                "internal", "Sage is needed to validate code cells.", str(e)))
        except Exception as e:
            return _nb_edit_out(error=_err(
                "internal", "Unexpected failure inside the server.",
                f"{type(e).__name__}: {e}"))
