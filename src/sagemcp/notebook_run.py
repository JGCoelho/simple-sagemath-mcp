"""Run every code cell of a notebook and shape the inline view.

`_run_nb_locked` owns the whole run: it restarts the kernel, executes the
code cells in order, writes the file once, and cuts the returned cells to
one `constants.MAX_INLINE_CHARS` budget. Tracing shaping is here because
`error.details` has to be plain text without the generated wrapper's frames.
"""

import copy
import json
import re

import nbformat

from . import constants, kernel
from .errors import ExecTimeout, GapCrashLoop, KernelDied, NotebookError
from .kernel import _restart_locked
from .notebook_io import _save_nb
from .shaping import _summarise


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
            outs = kernel._run_source(state, cell.get("source") or "",
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
