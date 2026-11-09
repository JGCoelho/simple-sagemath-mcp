"""The error envelope and the seven closed output schemas. Copy verbatim.

The dict literals and the key order are the wire contract: `tools/list`
carries them, and the builders are the only constructors of a tool result.
Nothing here imports anything — the schemas are inlined, not `$ref`s.
"""

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
