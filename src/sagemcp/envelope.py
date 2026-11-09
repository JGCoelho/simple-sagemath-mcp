"""The error envelope and the five closed output schemas.

The dict literals and the key order are the wire contract: `tools/list`
carries them, and the builders are the only constructors of a tool result.
Nothing here imports anything — the schemas are inlined, not `$ref`s.

Each field is defined once, as a shared fragment below, and every schema that
returns it is built from that fragment. A tool adds only its own fields and its
own `error.code` enum. `exec` and `run_file` share one job schema; only their
codes differ.

`exec` and `run_file` answer with a job envelope: `status`, `job_id` and
`elapsed` are present only while the call is still running. A finished call
returns the five base keys, as it always did.
"""

import copy

# --- error envelope ----------------------------------------------------------

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
                        "`details` holds the failure's text: a traceback, an "
                        "offending source fragment, a path, a job id, or null."),
        "properties": {
            "code": {"type": "string", "enum": list(codes)},
            "message": {"type": "string"},
            "details": {"type": ["string", "null"]},
        },
        "required": ["code", "message", "details"],
        "additionalProperties": False,
    }


# --- shared fragments --------------------------------------------------------

# One dict per field. Every schema copies them (`_closed`), so no two schemas
# alias one dict.

_RESULT = {
    "type": "string",
    "description": ("repr() of the trailing expression; empty string if the source "
                    "is statements-only or the value is None. A job_status reply "
                    "has it only once the job has finished."),
}
_STDOUT = {
    "type": "string",
    "description": "Anything printed to stdout; while a job runs, the output so far.",
}
_STDERR = {
    "type": "string",
    "description": ("Anything written to stderr; never folded into stdout. While a "
                    "job runs, the output so far."),
}
_TRUNCATED = {
    "type": "boolean",
    "default": False,
    "description": ("True if result, stdout or stderr was cut at the per-call inline "
                    "budget; the untruncated text is not recoverable."),
}
_ELAPSED = {
    "type": "number",
    "description": "Seconds from the job's start to its finish, or to now while it runs.",
}

# The codes a job can end with. exec and run_file return all of them; job_status
# returns them for a finished job, and adds `unknown_job` for an id it does not know.
_JOB_FAILURES = ["cell_execution_failed", "gap_crash_loop", "kernel_died",
                 "sage_not_found", "internal"]


def _closed(properties: dict, required: list[str]) -> dict:
    """Build one closed object schema. Copies each field, so schemas share no dict."""
    return {"type": "object",
            "properties": copy.deepcopy(properties),
            "required": list(required),
            "additionalProperties": False}


def _job_fields(codes: list[str]) -> dict:
    """The five base keys of a job's result. The same for exec, run_file and job_status."""
    return {"result": _RESULT, "stdout": _STDOUT, "stderr": _STDERR,
            "truncated": _TRUNCATED, "error": _error_schema(codes)}


def _job_call_schema(codes: list[str]) -> dict:
    """The output schema of a tool that starts a job (`exec`, `run_file`)."""
    base = _job_fields(codes)
    running = {
        "status": {
            "type": "string",
            "enum": ["running"],
            "description": ("Present only while the call is still running. The job "
                            "keeps the kernel; poll `job_status` with job_id."),
        },
        "job_id": {
            "type": "string",
            "description": "Present only with status `running`.",
        },
        "elapsed": _ELAPSED,
    }
    return _closed({**base, **running}, required=list(base))


OUTPUT_SCHEMA_EXEC = _job_call_schema(["invalid_source", *_JOB_FAILURES, "kernel_busy"])

# run_file reads a file first, so it alone can answer file_not_found. exec never does.
OUTPUT_SCHEMA_RUN_FILE = _job_call_schema(["invalid_source", "file_not_found",
                                           *_JOB_FAILURES, "kernel_busy"])

OUTPUT_SCHEMA_JOB_STATUS = _closed({
    "status": {
        "type": "string",
        "enum": ["running", "done", "error", "interrupted"],
        "description": ("Present when the job exists. `done`: finished without "
                        "error; `error`: finished with an error result; "
                        "`interrupted`: a restart ended it."),
    },
    "elapsed": _ELAPSED,
    "stdout": _STDOUT,
    "stderr": _STDERR,
    "truncated": _TRUNCATED,
    "result": _RESULT,
    "error": _error_schema(["invalid_source", *_JOB_FAILURES, "unknown_job"]),
}, required=["stdout", "stderr", "truncated", "error"])


OUTPUT_SCHEMA_INTERRUPT = _closed({
    "interrupted": {
        "type": "boolean",
        "description": ("True if a running call was sent an interrupt; false "
                        "if idle or no kernel. A job that outlived its wait "
                        "keeps this true until it completes or the kernel "
                        "restarts."),
    },
    "error": _error_schema(["internal"]),
}, required=["interrupted", "error"])

OUTPUT_SCHEMA_RESTART = _closed({
    "restarted": {
        "type": "boolean",
        "description": "True iff a new ready kernel is running after this call.",
    },
    "error": _error_schema(["sage_not_found", "kernel_died", "internal"]),
}, required=["restarted", "error"])


# --- builders ----------------------------------------------------------------

def _exec_out(*, result: str = "", stdout: str = "", stderr: str = "",
              truncated: bool = False, error: dict | None = None) -> dict:
    """The only constructor of a finished `exec`/`run_file` result. Five keys."""
    return {"result": result, "stdout": stdout, "stderr": stderr,
            "truncated": truncated, "error": error}


def _exec_running_out(*, job_id: str, elapsed: float, result: str = "",
                      stdout: str = "", stderr: str = "",
                      truncated: bool = False) -> dict:
    """The envelope of a job still running at its wait: the output so far."""
    return {**_exec_out(result=result, stdout=stdout, stderr=stderr,
                        truncated=truncated),
            "status": "running", "job_id": job_id, "elapsed": elapsed}


def _job_status_out(*, status: str | None = None, elapsed: float | None = None,
                    stdout: str = "", stderr: str = "", truncated: bool = False,
                    result: str | None = None, error: dict | None = None) -> dict:
    """The only constructor of a `job_status` result.

    `status` and `elapsed` are present only when the job exists; `result` only
    once it has finished. Keys match OUTPUT_SCHEMA_JOB_STATUS.
    """
    out: dict = {}
    if status is not None:
        out["status"] = status
        out["elapsed"] = elapsed
    out.update({"stdout": stdout, "stderr": stderr,
                "truncated": truncated, "error": error})
    if result is not None:
        out["result"] = result
    return out


def _kernel_busy_err(job_id: str | None) -> dict:
    """The `kernel_busy` envelope. `details` names the job that holds the kernel."""
    return _err("kernel_busy",
                "The Sage kernel is busy with another call; calls do not queue. "
                "Poll job_status for the job, or interrupt it.",
                job_id or "a restart holds the kernel")


def _interrupt_out(*, interrupted: bool = False,
                   error: dict | None = None) -> dict:
    """The only constructor of an `interrupt` result."""
    return {"interrupted": interrupted, "error": error}


def _restart_out(*, restarted: bool = False, error: dict | None = None) -> dict:
    """The only constructor of a `restart` result."""
    return {"restarted": restarted, "error": error}
