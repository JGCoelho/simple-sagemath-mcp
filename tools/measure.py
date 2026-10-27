"""Measure the agent-visible `tools/list` surface.

Pi's MCP host exposes only these four fields per tool; `_meta`, `title`,
`annotations` and the rest never reach the model. Serializing just the visible
fields, compactly (the wire form), keeps this measurement aligned with what the
agent actually pays for.
"""
import asyncio, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server

VISIBLE = ("name", "description", "inputSchema", "outputSchema")

async def main():
    tools = await server.mcp.list_tools()
    rows = []
    for t in tools:
        wire = t.to_mcp_tool().model_dump(mode="json", by_alias=True, exclude_none=True)
        rows.append({k: wire[k] for k in VISIBLE if k in wire})
    payload = json.dumps(rows, separators=(",", ":"), sort_keys=True)
    sys.stdout.write(payload + "\n")
    sys.stderr.write(f"chars={len(payload)} words={len(payload.split())}\n")

asyncio.run(main())
