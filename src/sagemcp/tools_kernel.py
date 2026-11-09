"""The `interrupt` and `restart` tools.

Both are thin wrappers over the kernel lifecycle helpers and answer with the
shared error envelope.
"""

from fastmcp import Context

from .app import mcp
from .envelope import (OUTPUT_SCHEMA_INTERRUPT, OUTPUT_SCHEMA_RESTART, _err,
                       _interrupt_out, _restart_out)
from .errors import KernelDied, SageNotFound
from .kernel import _interrupt, _restart


# --- kernel tools ------------------------------------------------------------

@mcp.tool(name="interrupt", output_schema=OUTPUT_SCHEMA_INTERRUPT)
async def sage_interrupt(ctx: Context) -> dict:
    """Stop the running computation and keep the kernel and its namespace.

    State: the namespace is untouched, so variables defined before the
    interrupted call are still available; the interrupted call returns
    `execution_failed` with `KeyboardInterrupt` in `details`.
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

