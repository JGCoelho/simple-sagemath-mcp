"""A stateful SageMath MCP server: one kernel, one namespace.

`exec` runs Sage code in the kernel namespace, and `run_file` runs a `.py` or
`.sage` file in it. A call that outlasts its wait returns `running` with a
`job_id`; `job_status` reads that job, `interrupt` stops a running computation
without losing the namespace, and `restart` replaces the kernel and wipes the
namespace. The four notebook tools (`nb_run`, `nb_write`, `nb_read`, `nb_edit`)
are deprecated: they are served only when the server runs with
`--legacy-notebook-tools`. Every tool answers with the same error envelope, and
`error: null` means success. The kernel starts on the first call and lives as
long as the MCP connection.

This is not a sandbox: the kernel runs the code that the agent sends, with the
user's permissions.
"""

from . import app                # noqa: F401  (mcp exists before tools register)
from . import tools_exec         # noqa: F401  (exec, run_file, job_status)
from . import tools_notebook     # noqa: F401  (legacy nb_* registration, behind a flag)
from . import tools_kernel       # noqa: F401  (interrupt, restart)
