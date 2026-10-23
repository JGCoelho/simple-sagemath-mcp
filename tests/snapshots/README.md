# Frozen tool contract

`tools-list.json` is the `tools/list` reply of the server at the moment the SDK
migration started (v1 hand-rolled JSON-RPC, commit `44b950b`), recorded through the
official `mcp` client so the baseline and every later check travel the same code
path. It holds the part of the wire contract that must not move: the eight tool
names **and their order**, each `inputSchema.properties` (name, type, default), and
`required`. Descriptions are recorded too, but they are compared with a note rather
than a failure: they are prose, and the migration moves their wording into
docstrings.

Why a snapshot instead of assertions: the promise is "identical to what agents
already use", and a hand-written expectation would just restate the code under
test. Regenerate deliberately with `python tests/snapshot_tools.py --write`; check
any change with `--compare`, which exits non-zero and prints every frozen field that
moved. Recapture only when the user has accepted a contract change.

The snapshot needs no Sage and no kernel: `tools/list` is answered before any
kernel is spawned, so the generator uses a throwaway `SAGE_MCP_STATE` and leaves
the live kernel at `/tmp/sage-mcp-$UID` alone.
