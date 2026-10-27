#!/usr/bin/env python3
# Run from the repo root: python test/test_tool_docs.py -v
# Kernel-free: imports server.py and reads the registered tool schemas. No Sage,
# no kernel, no server process.

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import server


def docstring_errors(doc: str | None) -> list[str] | None:
    """The names after `Errors:` in a docstring, as a list.

    The line may wrap; it ends at a blank line or the end of the docstring.
    Returns None when the docstring has no `Errors:` paragraph at all.
    """
    lines = (doc or "").splitlines()
    for index, line in enumerate(lines):
        if not line.strip().startswith("Errors:"):
            continue
        parts = [line.split("Errors:", 1)[1]]
        for later in lines[index + 1:]:
            if not later.strip():
                break
            parts.append(later)
        text = " ".join(parts).strip()
        return [name.strip().rstrip(".") for name in text.split(",")
                if name.strip()]
    return None


class TestToolDocErrors(unittest.TestCase):
    """Each docstring's Errors line must match its outputSchema code enum.

    Direct MCP callers do not receive `outputSchema` from Pi, so the docstring
    is the only place they can see the error taxonomy. This test keeps the two
    copies from drifting.
    """

    @classmethod
    def setUpClass(cls):
        tools = asyncio.run(server.mcp.list_tools())
        cls.wire = {
            tool.name: tool.to_mcp_tool().model_dump(
                mode="json", by_alias=True, exclude_none=True)
            for tool in tools
        }

    def test_every_tool_has_an_errors_line(self):
        for name in self.wire:
            with self.subTest(tool=name):
                function = getattr(server, f"sage_{name}", None)
                self.assertIsNotNone(function, f"no sage_{name} function")
                errors = docstring_errors(function.__doc__)
                self.assertIsNotNone(
                    errors, f"{name} docstring has no Errors: line")
                self.assertTrue(errors, f"{name} has an empty Errors: line")

    def test_docstring_errors_match_output_schema(self):
        for name, wire in self.wire.items():
            with self.subTest(tool=name):
                function = getattr(server, f"sage_{name}")
                errors = docstring_errors(function.__doc__)
                enum = (wire["outputSchema"]["properties"]["error"]
                        ["properties"]["code"]["enum"])
                self.assertEqual(errors, enum,
                                 f"{name}: docstring and outputSchema differ")


if __name__ == "__main__":
    unittest.main()
