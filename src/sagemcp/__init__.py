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
