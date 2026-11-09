"""Read, validate, upgrade, and write `.ipynb` files.

One file is the only source of truth: every read comes from disk and every
write replaces it atomically. Nothing here starts a kernel or executes a
cell; `_prepare` is the static check that notebook validation shares with
execution. `nbformat` and `os` stay module imports on purpose: the tests
patch `notebook_io.os.replace` and read `notebook_io.nbformat`.
"""

import copy
import json
import os
import tempfile
import uuid
from pathlib import Path

import nbformat

from . import constants
from .errors import NotebookError, SourceError
from .source import _prepare


# --- notebook I/O ------------------------------------------------------------

# One file is the only source of truth: every read comes from disk and every
# write replaces it atomically. Notebooks below nbformat 4.5 are upgraded in
# memory (cell ids are minted) so ops can address every cell, but the upgrade
# only reaches the disk file when a writing tool runs.


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
    nb.nbformat_minor = constants.NOTEBOOK_MINOR
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
    if disk_minor >= constants.NOTEBOOK_MINOR:
        _check_raw_ids(raw)
    try:
        nb = nbformat.reads(text, as_version=4)
        nbformat.validate(nb)
    except Exception as e:
        raise NotebookError(
            "invalid_notebook", "The notebook is not nbformat-valid.",
            f"{type(e).__name__}: {e}") from None
    if disk_minor < constants.NOTEBOOK_MINOR:
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
    nb.nbformat = constants.NOTEBOOK_NBFORMAT
    nb.nbformat_minor = constants.NOTEBOOK_MINOR
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
