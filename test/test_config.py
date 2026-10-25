#!/usr/bin/env python3
# Run from the repo root, with the python of your Sage: python test/test_config.py

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import Config


def write_json(d: str, data: dict) -> Path:
    p = Path(d) / "server.json"
    p.write_text(json.dumps(data))
    return p


class TestConfig(unittest.TestCase):

    def test_defaults_when_no_file(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Config.load(Path(d) / "missing.json")
        self.assertEqual(cfg.kernel_read_timeout, 2400.0)
        self.assertEqual(cfg.heartbeat_interval, 15.0)
        self.assertEqual(cfg.shell_drain_timeout, 30.0)
        self.assertEqual(cfg.kernel_ready_timeout, 60.0)

    def test_json_overrides_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            p = write_json(d, {"kernel_read_timeout": 10.0, "heartbeat_interval": 1.5})
            cfg = Config.load(p)
        self.assertEqual(cfg.kernel_read_timeout, 10.0)
        self.assertEqual(cfg.heartbeat_interval, 1.5)
        self.assertEqual(cfg.shell_drain_timeout, 30.0)   # untouched default

    def test_unknown_json_keys_are_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            p = write_json(d, {"kernel_read_timeout": 10.0, "bogus": 42})
            cfg = Config.load(p)
        self.assertEqual(cfg.kernel_read_timeout, 10.0)

    def test_frozen(self):
        cfg = Config.load(Path("/nonexistent/missing.json"))
        with self.assertRaises(Exception):
            cfg.kernel_read_timeout = 1.0


if __name__ == "__main__":
    unittest.main()
