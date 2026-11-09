#!/usr/bin/env python3
# Run from the repo root: python test/test_packaging.py -v
# Kernel-free and fast: packaging metadata, main(), the shim, and the
# sibling-sage resolution fallback. No Sage, no kernel, no server process.

import ast
import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))
from sagemcp import app
from sagemcp import sageresolve
from sagemcp.errors import SageNotFound

try:
    import tomllib
except ModuleNotFoundError:          # Python 3.10 has no tomllib
    tomllib = None

PYPROJECT = ROOT / "pyproject.toml"
SHIM = ROOT / "server.py"
SRC = ROOT / "src"
PACKAGE = SRC / "sagemcp"
MODULES = sorted(PACKAGE.glob("*.py"))
MINORS = (9, 10, 11)


def read_pyproject() -> dict:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def parses(path: Path, minor: int) -> bool:
    try:
        ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, minor))
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


def module_tree(path: Path):
    return ast.parse(path.read_text(encoding="utf-8"))


def annotated_modules() -> list[str]:
    """Modules with a PEP 604 union in an annotation evaluated at def time."""
    return [path.name for path in MODULES
            if uses_pep604_annotations(module_tree(path))]


def file_floor(path: Path) -> int:
    """The smallest Python minor that can import `path`.

    `ast.parse` alone is not enough: a PEP 604 union in an annotation parses on
    3.9 but raises TypeError at import, because the annotation is evaluated when
    the function is defined. So the floor is the larger of the syntax floor and
    3.10 when such an annotation exists.
    """
    text = path.read_text(encoding="utf-8")
    syntax = next(n for n in MINORS if parses(path, n))
    if uses_pep604_annotations(ast.parse(text)):
        syntax = max(syntax, 10)
    return syntax


def runtime_floor() -> int:
    """The floor of the package: the largest floor over every module."""
    assert MODULES, "no modules found under src/sagemcp"
    return max(file_floor(path) for path in MODULES)


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
            {"simple-sagemath-mcp": "sagemcp.app:main"})

    def test_package_layout(self):
        tool = self.data["tool"]["setuptools"]
        self.assertEqual(tool["package-dir"], {"": "src"})
        self.assertEqual(tool["packages"]["find"]["where"], ["src"])
        self.assertNotIn("py-modules", tool)

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
                app.main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("--directory", out.getvalue())

    def test_directory_changes_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.getcwd()
            self.addCleanup(os.chdir, old)
            with mock.patch.object(app.mcp, "run") as run:
                app.main(["--directory", tmp])
            run.assert_called_once_with()
            self.assertEqual(Path(os.getcwd()).resolve(), Path(tmp).resolve())


class TestShim(unittest.TestCase):
    """The root server.py: a PEP 723 header, the src/ path insert, and main()."""

    def test_help_runs(self):
        # The only executable proof that the sys.path insert works.
        proc = subprocess.run([sys.executable, str(SHIM), "--help"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--directory", proc.stdout)

    def test_pep723_header(self):
        # `uv run server.py` resolves the four dependencies from this block.
        if tomllib is None:
            self.skipTest("tomllib needs Python 3.11+")
        text = SHIM.read_text(encoding="utf-8")
        block = re.search(r"^# /// script$(.*?)^# ///$", text,
                          re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(block, "server.py has no PEP 723 script block")
        body = block.group(1)
        declared = read_pyproject()["project"]["requires-python"]
        self.assertIn(f'# requires-python = "{declared}"', body)
        deps = re.search(r"^# dependencies = \[(.*)\]$", body, re.MULTILINE)
        self.assertIsNotNone(deps, "the PEP 723 block has no dependencies line")
        names = re.findall(r'"([^"]+)"', deps.group(1))
        self.assertEqual(sorted(names), ["fastmcp", "ipykernel",
                                         "jupyter_client", "nbformat"])


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
                mock.patch.object(shutil, "which", return_value=None), \
                mock.patch.object(sys, "executable", str(python)):
            return sageresolve.resolve_kernel_python()

    def test_finds_sibling_sage_without_path(self):
        bin_dir, python = self.make_fake(with_sage=True)
        got_python, got_bin = self.resolve(python)
        self.assertEqual(Path(got_python), python)
        self.assertEqual(Path(got_bin), bin_dir)

    def test_no_sibling_sage_is_not_found(self):
        _, python = self.make_fake(with_sage=False)
        with self.assertRaises(SageNotFound):
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
        for path in MODULES:
            with self.subTest(module=path.name):
                self.assertTrue(parses(path, minor))

    def test_floor_is_not_vacuous(self):
        # An annotation-free corpus cannot satisfy the floor by accident: at
        # least one module must evaluate a PEP 604 union at def time.
        self.assertTrue(annotated_modules(),
                        "no module under src/sagemcp has a PEP 604 union in an "
                        "evaluated annotation, so the 3.10 floor is untested")


if __name__ == "__main__":
    unittest.main()
