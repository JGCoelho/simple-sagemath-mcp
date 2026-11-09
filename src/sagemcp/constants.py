"""Kernel and notebook constants for the server.

Read these through the module (`constants.MAX_INLINE_CHARS`); never bind them
with a from-import, or the tests' patches stop reaching the call sites. The one
exception is `re`, which the module needs at import time.
"""

import re


# --- constants ---------------------------------------------------------------

# There are no environment overrides for these: MCP hosts sanitize the
# environment. SAGE_BIN is the one exception, because a host that hides PATH
# must still be able to name the Sage install.

KERNEL_READ_TIMEOUT = 2400.0     # default per-call budget for exec and cell runs
HEARTBEAT_INTERVAL = 15.0        # progress report interval while a call runs
SHELL_DRAIN_TIMEOUT = 30.0       # wait for the shell reply after idle
KERNEL_READY_TIMEOUT = 60.0      # wait_for_ready on a new kernel
SHUTDOWN_TIMEOUT = 30.0          # restart: wait this long for an in-flight call
MAX_INLINE_CHARS = 20000         # inline budget per call
GAP_CRASH_LIMIT = 3              # stderr matches of "Gap crashed" before abort
NOTEBOOK_NBFORMAT = 4
NOTEBOOK_MINOR = 5
KERNEL_META = {
    "kernelspec": {"name": "sagemath",
                   "display_name": "SageMath",
                   "language": "sage"},
    "language_info": {"name": "sage"},
}
GAP_CRASH_PATTERN = re.compile(r"Gap crashed|Restarting Gap", re.IGNORECASE)
