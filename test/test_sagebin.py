#!/usr/bin/env python3
# Run from the repo root: python test/test_sagebin.py -v
# Kernel-free: resolves Sage against fake prefixes under a temp dir.

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from queue import Empty
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from sagemcp import constants, kernel, sageresolve
from sagemcp.errors import InternalBudgetExpired, KernelDied, SageNotFound

# Recorded right after importing the server, before any test needs Sage.
SAGE_LOADED_ON_IMPORT = "sage" in sys.modules or "sage.all" in sys.modules


def make_prefix(root: Path, *, pythons=("python3",), sage_body: str | None = None):
    """Make a fake Sage prefix: <root>/bin/{sage,python3,...}."""
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    sage = bin_dir / "sage"
    sage.write_text(sage_body if sage_body is not None
                    else "#!/bin/sh\nexit 1\n")
    sage.chmod(0o755)
    for name in pythons:
        p = bin_dir / name
        p.write_text("#!/bin/sh\nexit 1\n")
        p.chmod(0o755)
    return bin_dir


class TestResolve(unittest.TestCase):
    """resolve_kernel_python and _kernel_env. No kernel, no Sage import."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # Steer PATH through the environment only: never rely on the real PATH.
        self.empty = self.root / "empty"
        self.empty.mkdir()

    def run_resolve(self, env: dict):
        with mock.patch.dict(os.environ, env, clear=True):
            return sageresolve.resolve_kernel_python()

    def test_sage_bin_wins_over_path(self):
        on_path = make_prefix(self.root / "onpath")
        winner = make_prefix(self.root / "winner")
        env = {"PATH": str(on_path), "SAGE_BIN": str(winner / "sage")}
        python, bin_dir = self.run_resolve(env)
        self.assertEqual(Path(bin_dir), winner)
        self.assertEqual(Path(python), winner / "python3")

    def test_sibling_python3(self):
        prefix = make_prefix(self.root / "p3", pythons=("python3", "python"))
        env = {"PATH": str(prefix), "SAGE_BIN": str(prefix / "sage")}
        python, bin_dir = self.run_resolve(env)
        self.assertEqual(Path(python), prefix / "python3")
        self.assertEqual(Path(bin_dir), prefix)

    def test_sibling_python_fallback(self):
        prefix = make_prefix(self.root / "py", pythons=("python",))
        env = {"PATH": str(prefix), "SAGE_BIN": str(prefix / "sage")}
        python, bin_dir = self.run_resolve(env)
        self.assertEqual(Path(python), prefix / "python")
        self.assertEqual(Path(bin_dir), prefix)

    def test_sage_c_fallback(self):
        # No sibling python: the sage executable must be asked for sys.executable.
        interpreter = sys.executable
        prefix = make_prefix(self.root / "ask", pythons=(),
                             sage_body=f'#!/bin/sh\necho "{interpreter}"\n')
        env = {"PATH": str(prefix), "SAGE_BIN": str(prefix / "sage")}
        python, bin_dir = self.run_resolve(env)
        self.assertEqual(python, interpreter)
        self.assertEqual(Path(bin_dir), prefix)

    def test_not_found_message(self):
        # No sibling sage next to our interpreter either: patch it out so the
        # third resolution source (the console-script fallback) cannot fire.
        no_sage = str(self.root / "nosage" / "python3")
        with mock.patch.dict(os.environ, {"PATH": str(self.empty)}, clear=True), \
                mock.patch.object(sys, "executable", no_sage):
            with self.assertRaises(SageNotFound) as caught:
                sageresolve.resolve_kernel_python()
        self.assertEqual(
            str(caught.exception),
            "SageMath not found. Install it, or set SAGE_BIN to the sage executable.")

    def test_missing_sage_bin_is_not_found(self):
        env = {"PATH": str(self.empty), "SAGE_BIN": str(self.root / "nope" / "sage")}
        no_sage = str(self.root / "nosage" / "python3")
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(sys, "executable", no_sage):
            with self.assertRaises(SageNotFound):
                sageresolve.resolve_kernel_python()

    def test_env_path_prefix(self):
        env = sageresolve._kernel_env("/opt/sage/bin")
        self.assertEqual(env["PATH"].split(os.pathsep)[0], "/opt/sage/bin")
        self.assertIsNot(env, os.environ)
        old = os.environ.get("PATH", "")
        self.assertEqual(env["PATH"], "/opt/sage/bin" + os.pathsep + old)

    def test_seed_failure_is_sage_not_found(self):
        # Regression: a kernel that cannot import Sage was accepted as ready.
        outputs = [{"output_type": "error", "ename": "ImportError",
                    "evalue": "no sage.all",
                    "traceback": ["Traceback", "ImportError: no sage.all"]}]
        state = {"km": None, "kc": None, "busy": False}
        with mock.patch.object(kernel, "_run_source", return_value=outputs):
            with self.assertRaises(SageNotFound) as caught:
                kernel._seed_namespace(state)
        self.assertIn("no sage.all", str(caught.exception))

    def test_seed_success_is_silent(self):
        state = {"km": None, "kc": None, "busy": False}
        with mock.patch.object(kernel, "_run_source", return_value=[]):
            kernel._seed_namespace(state)          # must not raise

    def test_start_failure_is_kernel_died(self):
        # Regression: a first-call start failure used to surface as `internal`.
        state = {"km": None, "kc": None, "busy": False}
        with mock.patch.object(kernel, "_start_kernel",
                               side_effect=RuntimeError("no ipykernel")):
            with self.assertRaises(KernelDied):
                kernel._ensure_kernel(state)

    def test_import_starts_nothing(self):
        # Importing the server must not import Sage: `_prepare` imports it lazily.
        self.assertFalse(SAGE_LOADED_ON_IMPORT)
        corpus = sorted((ROOT / "src" / "sagemcp").glob("*.py"))
        sources = [path.read_text() for path in corpus + [ROOT / "server.py"]]
        module_level = [line for source in sources for line in source.splitlines()
                        if re.match(r"(?:import|from) sage[\s.]", line)]
        self.assertEqual(module_level, [])
        lazy = sum(source.count("from sage.repl.preparse import preparse")
                   for source in sources)
        self.assertEqual(lazy, 1)


class _AliveKM:
    """A kernel manager that always reports a live kernel."""

    def is_alive(self):
        return True

    def interrupt_kernel(self):
        pass

    def shutdown_kernel(self, now=False):
        pass


class _SilentKC:
    """A kernel client that accepts the code and then publishes nothing."""

    def execute(self, code, allow_stdin=False):
        return "msg-1"

    def get_iopub_msg(self, timeout=None):
        raise Empty

    def get_shell_msg(self, timeout=None):
        raise Empty

    def stop_channels(self):
        pass


class TestSeedBudget(unittest.TestCase):
    """The seed's internal read budget and its failure. No kernel, no Sage import."""

    def test_seed_passes_its_own_budget(self):
        state = {"km": None, "kc": None, "busy": False}
        seen = []

        def fake_run_source(*args, **kwargs):
            seen.append(kwargs)
            return []

        with mock.patch.object(kernel, "_run_source", side_effect=fake_run_source):
            kernel._seed_namespace(state)
        self.assertEqual(seen[0]["budget"], constants.SAGE_SEED_TIMEOUT)

    def test_seed_budget_expiry_is_kernel_died_and_stops_the_kernel(self):
        state = {"km": _AliveKM(), "kc": _SilentKC(), "busy": False}
        with mock.patch.object(constants, "SAGE_SEED_TIMEOUT", 0.05):
            with self.assertRaises(KernelDied) as caught:
                # The caller is `_start_kernel`; its wrapper stops the kernel on any
                # seed failure. These lines are that wrapper, not a second stop.
                try:
                    kernel._seed_namespace(state)
                except BaseException:
                    kernel._stop_kernel(state)
                    raise
        self.assertNotIsInstance(caught.exception, InternalBudgetExpired)
        self.assertIn("SAGE_SEED_TIMEOUT", str(caught.exception))
        self.assertIn("0.05", str(caught.exception))
        self.assertIsNone(state["km"])
        self.assertIsNone(state["kc"])


if __name__ == "__main__":
    unittest.main()
