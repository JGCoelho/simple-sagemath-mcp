"""Measure the `tools/list` surface.

`tools/list` carries name, description, inputSchema, and outputSchema. Pi's
MCP host declares only name, description, and inputSchema to a direct caller, so
outputSchema bytes are wire-only. stdout is the wire payload used for
before/after files; stderr reports both sizes.
"""
import asyncio, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server

VISIBLE = ("name", "description", "inputSchema", "outputSchema")
MODEL = ("name", "description", "inputSchema")

async def main():
    tools = await server.mcp.list_tools()
    rows = []
    for t in tools:
        wire = t.to_mcp_tool().model_dump(mode="json", by_alias=True, exclude_none=True)
        rows.append({k: wire[k] for k in VISIBLE if k in wire})
    payload = json.dumps(rows, separators=(",", ":"), sort_keys=True)
    model = json.dumps([{k: t[k] for k in MODEL if k in t} for t in rows],
                       separators=(",", ":"), sort_keys=True)
    sys.stdout.write(payload + "\n")
    sys.stderr.write(f"chars={len(payload)} words={len(payload.split())} "
                     f"model_chars={len(model)}\n")

asyncio.run(main())
