#!/usr/bin/env python3
# Run from the repo root, with the python of your Sage: python test/test_config.py

import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import Config


@contextmanager
def env(**overrides):
    """Temporarily replace os.environ, dropping any pre-existing SAGE_MCP_* vars."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("SAGE_MCP_")}
    clean.update(overrides)
    with patch.dict(os.environ, clean, clear=True):
        yield


def write_json(d: str, data: dict) -> Path:
    p = Path(d) / "server.json"
    p.write_text(json.dumps(data))
    return p


class TestConfig(unittest.TestCase):

    def test_defaults_when_no_file_and_no_env(self):
        with tempfile.TemporaryDirectory() as d, env():
            cfg = Config.load(Path(d) / "missing.json")
        self.assertEqual(cfg.kernel_read_timeout, 2400.0)
        self.assertEqual(cfg.heartbeat_interval, 15.0)
        self.assertEqual(cfg.shell_drain_timeout, 30.0)
        self.assertEqual(cfg.kernel_ready_timeout, 60.0)

    def test_json_overrides_defaults(self):
        with tempfile.TemporaryDirectory() as d, env():
            p = write_json(d, {"kernel_read_timeout": 10.0, "heartbeat_interval": 1.5})
            cfg = Config.load(p)
        self.assertEqual(cfg.kernel_read_timeout, 10.0)
        self.assertEqual(cfg.heartbeat_interval, 1.5)
        self.assertEqual(cfg.shell_drain_timeout, 30.0)   # untouched default

    def test_env_overrides_json(self):
        with tempfile.TemporaryDirectory() as d, env(SAGE_MCP_KERNEL_READ_TIMEOUT="99"):
            p = write_json(d, {"kernel_read_timeout": 10.0})
            cfg = Config.load(p)
        self.assertEqual(cfg.kernel_read_timeout, 99.0)

    def test_env_alone(self):
        with env(SAGE_MCP_HEARTBEAT_INTERVAL="2.5"):
            cfg = Config.load(Path("/nonexistent/missing.json"))
        self.assertEqual(cfg.heartbeat_interval, 2.5)

    def test_unknown_json_keys_are_ignored(self):
        with tempfile.TemporaryDirectory() as d, env():
            p = write_json(d, {"kernel_read_timeout": 10.0, "bogus": 42})
            cfg = Config.load(p)
        self.assertEqual(cfg.kernel_read_timeout, 10.0)

    def test_frozen(self):
        with env():
            cfg = Config.load(Path("/nonexistent/missing.json"))
        with self.assertRaises(Exception):
            cfg.kernel_read_timeout = 1.0


if __name__ == "__main__":
    unittest.main()
