#!/usr/bin/env python3
# Run from the repo root: python test/test_tool_docs.py -v
# Kernel-free: imports the `sagemcp` package and reads the registered tool
# schemas. No Sage, no kernel, no server process. The deprecated notebook tools
# are not on the default tool list, so their docstrings are checked directly.

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from sagemcp import app, tools_exec, tools_kernel, tools_notebook

TOOL_MODULES = (tools_exec, tools_notebook, tools_kernel)


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


def find_tool(name: str) -> list[tuple]:
    """Every (module, function) pair defining `sage_<name>` in a tool module."""
    found = [(module, getattr(module, f"sage_{name}", None))
             for module in TOOL_MODULES]
    return [(module, function) for module, function in found
            if function is not None]


class TestToolDocErrors(unittest.TestCase):
    """Each docstring's Errors line must match its outputSchema code enum.

    Direct MCP callers do not receive `outputSchema` from Pi, so the docstring
    is the only place they can see the error taxonomy. This test keeps the two
    copies from drifting.
    """

    @classmethod
    def setUpClass(cls):
        tools = asyncio.run(app.mcp.list_tools())
        cls.wire = {
            tool.name: tool.to_mcp_tool().model_dump(
                mode="json", by_alias=True, exclude_none=True)
            for tool in tools
        }

    def test_every_tool_has_an_errors_line(self):
        for name in self.wire:
            with self.subTest(tool=name):
                found = find_tool(name)
                self.assertEqual(
                    len(found), 1,
                    f"sage_{name} must be defined in exactly one tool module, "
                    f"found in {[module.__name__ for module, _ in found]}")
                function = found[0][1]
                errors = docstring_errors(function.__doc__)
                self.assertIsNotNone(
                    errors, f"{name} docstring has no Errors: line")
                self.assertTrue(errors, f"{name} has an empty Errors: line")

    def test_docstring_errors_match_output_schema(self):
        for name, wire in self.wire.items():
            with self.subTest(tool=name):
                function = find_tool(name)[0][1]
                errors = docstring_errors(function.__doc__)
                enum = (wire["outputSchema"]["properties"]["error"]
                        ["properties"]["code"]["enum"])
                self.assertEqual(errors, enum,
                                 f"{name}: docstring and outputSchema differ")


class TestLegacyToolDocs(unittest.TestCase):
    """The deprecated nb_* tools: a DEPRECATED summary, and an Errors line that matches."""

    def test_summary_says_deprecated(self):
        for name, function, _ in tools_notebook.LEGACY_TOOLS:
            with self.subTest(tool=name):
                first = (function.__doc__ or "").strip().splitlines()[0]
                self.assertEqual(first, "DEPRECATED: use run_file.")

    def test_errors_match_output_schema(self):
        for name, function, schema in tools_notebook.LEGACY_TOOLS:
            with self.subTest(tool=name):
                errors = docstring_errors(function.__doc__)
                enum = schema["properties"]["error"]["properties"]["code"]["enum"]
                self.assertEqual(errors, enum,
                                 f"{name}: docstring and outputSchema differ")


if __name__ == "__main__":
    unittest.main()
