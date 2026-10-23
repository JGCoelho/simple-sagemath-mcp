#!/usr/bin/env python3
"""snapshot_tools.py -- freeze or compare the tools/list contract.

    python tests/snapshot_tools.py --write        # (re)freeze tests/snapshots/tools-list.json
    python tests/snapshot_tools.py --compare      # compare the live server with the freeze

Why: the rewrite may change *how* the server talks, never *what* it promises.  The
frozen wire contract is the eight tool names, their parameter names/defaults and
which are required.  A snapshot is the only honest way to assert "identical" --
comparing hand-written dicts in the test would just re-state the code under test.

How: spawn the server over stdio (a throwaway SAGE_MCP_STATE, so no live kernel is
touched) through the *official* MCP client, so the same code path records the
baseline and checks the port.  tools/list never starts a kernel; the snapshot needs
no Sage.
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "sage_mcp.py")
SNAPSHOT = os.path.join(HERE, "snapshots", "tools-list.json")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


def _server_params(state_dir):
    env = dict(os.environ)
    env["SAGE_MCP_STATE"] = state_dir
    env.setdefault("SAGE_CWD", "/tmp")
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    return StdioServerParameters(
        command=sys.executable, args=[SERVER], env=env, cwd=ROOT
    )


async def live_tools(state_dir):
    """The eight tools as the server advertises them right now."""
    async with stdio_client(_server_params(state_dir), errlog=sys.stderr) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listing = await session.list_tools()
            # Dump inside the session: the result is only valid while it is live.
            return normalise(listing.tools)


def normalise(tools):
    """Only the frozen part: name, description, inputSchema (order preserved)."""
    out = []
    for tool in tools:
        schema = tool.input_schema or {}
        out.append(
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": {
                    "type": schema.get("type", "object"),
                    "properties": schema.get("properties") or {},
                    "required": list(schema.get("required") or []),
                },
            }
        )
    return out


_ABSENT = object()


def _declared_type(entry):
    """(type, nullable) -- reading through pydantic's `X | None` anyOf encoding.

    The hand-written schema said `"type": "string"` and no default for an optional
    parameter; pydantic says `anyOf[string, null]` with `default: null`.  Both mean
    "omittable, may be null", so the comparison looks through the encoding instead
    of requiring the same bytes (that would be a formatting test, not a contract
    test).  The encoding change is printed as a note so a reviewer sees it.
    """
    part = _concrete_part(entry)
    return (part or {}).get("type"), part is not entry


def _concrete_part(entry):
    """The non-null branch of a pydantic nullable property (else the entry itself)."""
    if "type" in entry:
        return entry
    for part in entry.get("anyOf") or []:
        if part.get("type") and part["type"] != "null":
            return part
    return None


def compare(frozen, live):
    """Differences in the frozen contract (empty list == clean).

    Fatal: a tool renamed/reordered, a parameter added or removed, a type or enum
    value changed, a stated default changed, `required` changed.  Not fatal, but
    reported: the wording of a description, and the pydantic nullable encoding
    (`anyOf[X, null]` + `default: null`) of a parameter the frozen schema carried as
    `X` with no default -- the same promise to a caller that omits it.
    """
    problems = []
    frozen_names = [t["name"] for t in frozen]
    live_names = [t["name"] for t in live]
    if frozen_names != live_names:
        problems.append("tool names/order: frozen %s, live %s" % (frozen_names, live_names))
    by_name = {t["name"]: t for t in live}
    for old in frozen:
        new = by_name.get(old["name"])
        if new is None:
            problems.append("%s: missing" % old["name"])
            continue
        old_props = old["inputSchema"]["properties"]
        new_props = new["inputSchema"]["properties"]
        if sorted(old_props) != sorted(new_props):
            problems.append(
                "%s: properties frozen %s, live %s"
                % (old["name"], sorted(old_props), sorted(new_props))
            )
        for key in sorted(set(old_props) & set(new_props)):
            _compare_property(old["name"], key, old_props[key], new_props[key], problems)
        if list(old["inputSchema"]["required"]) != list(new["inputSchema"]["required"]):
            problems.append(
                "%s: required frozen %s, live %s"
                % (old["name"], old["inputSchema"]["required"], new["inputSchema"]["required"])
            )
        if old["description"] != new["description"]:
            print("note: %s description changed (%d -> %d chars)"
                  % (old["name"], len(old["description"] or ""), len(new["description"] or "")))
    return problems


def _compare_property(tool, key, old, new, problems):
    where = "%s.%s" % (tool, key)
    old_type, _ = _declared_type(old)
    new_type, nullable = _declared_type(new)
    if old_type != new_type:
        problems.append("%s: type frozen %r, live %r" % (where, old_type, new_type))
    elif nullable:
        print("note: %s rendered as anyOf[%s, null] + default null (was %r, no default)"
              % (where, new_type, old_type))
    old_items = (old.get("items") or {}).get("type")
    new_items = ((_concrete_part(new) or {}).get("items") or {}).get("type")
    if old_items is not None or new_items is not None:
        if old_items != new_items:
            problems.append("%s: items type frozen %r, live %r" % (where, old_items, new_items))
    if old.get("enum") is not None or new.get("enum") is not None:
        if list(old.get("enum") or []) != list(new.get("enum") or []):
            problems.append(
                "%s: enum frozen %r, live %r" % (where, old.get("enum"), new.get("enum"))
            )
    old_def = old.get("default", _ABSENT)
    new_def = new.get("default", _ABSENT)
    if old_def is not _ABSENT and new_def is _ABSENT:
        problems.append("%s: default frozen %r, live has none" % (where, old_def))
    elif old_def is _ABSENT and new_def is not _ABSENT:
        if not nullable:  # a nullable parameter already got its encoding note above
            print("note: %s now states default %r (frozen schema stated none; omitting "
                  "behaves the same)" % (where, new_def))
    elif old_def is not _ABSENT and new_def not in (_ABSENT,):
        if old_def != new_def:
            problems.append("%s: default frozen %r, live %r" % (where, old_def, new_def))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true", help="freeze the snapshot")
    ap.add_argument("--compare", action="store_true", help="compare with the snapshot")
    args = ap.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="sage-mcp-snapshot-") as state_dir:
        tools = asyncio.run(live_tools(state_dir))

    if args.write:
        os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
        with open(SNAPSHOT, "w", encoding="utf-8") as fh:
            json.dump({"tools": tools}, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print("wrote %s (%d tools: %s)"
              % (SNAPSHOT, len(tools), ", ".join(t["name"] for t in tools)))
        return 0

    if args.compare:
        with open(SNAPSHOT, encoding="utf-8") as fh:
            frozen = json.load(fh)["tools"]
        problems = compare(frozen, tools)
        for line in problems:
            print("MISMATCH:", line)
        print("snapshot compare: %d tools, %d mismatches" % (len(tools), len(problems)))
        return 1 if problems else 0

    ap.error("pass --write or --compare")
    return 2


if __name__ == "__main__":
    sys.exit(main())
