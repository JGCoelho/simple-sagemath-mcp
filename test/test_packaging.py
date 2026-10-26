#!/usr/bin/env python3
# Run from the repo root: python test/test_packaging.py -v
# Kernel-free and fast: packaging metadata, main(), and the sibling-sage
# resolution fallback. No Sage, no kernel, no server process.

import ast
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
import server

try:
    import tomllib
except ModuleNotFoundError:          # Python 3.10 has no tomllib
    tomllib = None

PYPROJECT = ROOT / "pyproject.toml"
SERVER = ROOT / "server.py"
MINORS = (9, 10, 11)


def read_pyproject() -> dict:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def parses(minor: int) -> bool:
    try:
        ast.parse(SERVER.read_text(encoding="utf-8"), feature_version=(3, minor))
    except SyntaxError:
        return False
    return True


def annotated_types(tree) -> list:
    """Return every annotation expression in the module."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is not None:
                found.append(node.returns)
            args = node.args
            for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
                if arg.annotation is not None:
                    found.append(arg.annotation)
            for arg in (args.vararg, args.kwarg):
                if arg is not None and arg.annotation is not None:
                    found.append(arg.annotation)
    return found


def uses_pep604_annotations(tree) -> bool:
    """True if a PEP 604 union appears in an annotation evaluated at def time."""
    future_annotations = any(
        isinstance(node, ast.ImportFrom) and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in ast.walk(tree)
    )
    if future_annotations:
        return False
    for annotation in annotated_types(tree):
        for node in ast.walk(annotation):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
                return True
    return False


def runtime_floor() -> int:
    """The smallest Python minor that can import server.py.

    `ast.parse` alone is not enough: a PEP 604 union in an annotation parses on
    3.9 but raises TypeError at import, because the annotation is evaluated when
    the function is defined. So the floor is the larger of the syntax floor and
    3.10 when such an annotation exists.
    """
    syntax = next(n for n in MINORS if parses(n))
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    if uses_pep604_annotations(tree):
        syntax = max(syntax, 10)
    return syntax


class TestPyproject(unittest.TestCase):
    def setUp(self):
        if tomllib is None:
            self.skipTest("tomllib needs Python 3.11+")
        self.data = read_pyproject()
        self.project = self.data["project"]

    def test_name(self):
        self.assertEqual(self.project["name"], "simple-sagemath-mcp")

    def test_console_script(self):
        self.assertEqual(
            self.data["project"]["scripts"],
            {"simple-sagemath-mcp": "server:main"})

    def test_single_module(self):
        self.assertEqual(self.data["tool"]["setuptools"]["py-modules"],
                         ["server"])

    def test_fastmcp_pin(self):
        deps = self.project["dependencies"]
        self.assertIn("fastmcp>=4.1,<5", deps)

    def test_requires_python_present(self):
        self.assertIn("requires-python", self.project)
        self.assertTrue(self.project["requires-python"])


class TestMain(unittest.TestCase):
    def test_help_exits_zero_and_names_directory(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as caught:
                server.main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("--directory", out.getvalue())

    def test_directory_changes_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.getcwd()
            self.addCleanup(os.chdir, old)
            with mock.patch.object(server.mcp, "run") as run:
                server.main(["--directory", tmp])
            run.assert_called_once_with()
            self.assertEqual(Path(os.getcwd()).resolve(), Path(tmp).resolve())


class TestSiblingSageFallback(unittest.TestCase):
    """The third resolution source: bin/sage next to sys.executable."""

    def make_fake(self, *, with_sage: bool):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        if with_sage:
            sage = bin_dir / "sage"
            sage.write_text("#!/bin/sh\nexit 1\n")
            sage.chmod(0o755)
        python = bin_dir / "python3"
        python.write_text("#!/bin/sh\nexit 1\n")
        python.chmod(0o755)
        return bin_dir, python

    def resolve(self, python: Path):
        with mock.patch.dict(os.environ, {"SAGE_BIN": ""}), \
                mock.patch.object(server.shutil, "which", return_value=None), \
                mock.patch.object(server.sys, "executable", str(python)):
            return server.resolve_kernel_python()

    def test_finds_sibling_sage_without_path(self):
        bin_dir, python = self.make_fake(with_sage=True)
        got_python, got_bin = self.resolve(python)
        self.assertEqual(Path(got_python), python)
        self.assertEqual(Path(got_bin), bin_dir)

    def test_no_sibling_sage_is_not_found(self):
        _, python = self.make_fake(with_sage=False)
        with self.assertRaises(server.SageNotFound):
            self.resolve(python)


class TestSyntaxFloor(unittest.TestCase):
    def setUp(self):
        if tomllib is None:
            self.skipTest("tomllib needs Python 3.11+")

    def test_syntax_floor_matches_requires_python(self):
        declared = read_pyproject()["project"]["requires-python"]
        self.assertRegex(declared, r"^>=\d+\.\d+$")
        minor = int(declared.split(">=")[1].split(".")[1])
        self.assertEqual(minor, runtime_floor())
        self.assertTrue(parses(minor))


if __name__ == "__main__":
    unittest.main()
