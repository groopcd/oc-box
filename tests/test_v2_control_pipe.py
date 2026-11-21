"""Credential-bearing bridge readiness must use private IPC, never the console."""

import json
import os
from pathlib import Path
import pty
import subprocess
import sys
import tempfile
import unittest

from oc_box import opencode_v2_io as bridge


class PrivateControlPipeTests(unittest.TestCase):
    def test_real_child_preserves_pipe_records_and_refuses_console_and_file(self):
        code = (
            "from oc_box.opencode_v2_io import require_control_pipe, write_control_record\n"
            "fd = require_control_pipe()\n"
            "write_control_record(fd, {'marker':'synthetic-control'})\n"
        )
        root = Path(bridge.__file__).resolve().parents[1]
        command = [sys.executable, "-B", "-c", code]
        env = {"PATH": os.defpath, "PYTHONPATH": str(root)}
        with tempfile.TemporaryFile() as file:
            result = subprocess.run(
                command, env=env, stdout=file, stderr=subprocess.PIPE, timeout=10
            )
            self.assertNotEqual(result.returncode, 0)
            file.seek(0)
            self.assertEqual(file.read(), b"")
            self.assertNotIn(b"synthetic-control", result.stderr)
        master, slave = pty.openpty()
        try:
            result = subprocess.run(
                command, env=env, stdout=slave, stderr=subprocess.PIPE, timeout=10
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(b"synthetic-control", result.stderr)
        finally:
            os.close(slave)
            os.close(master)
        result = subprocess.run(command, env=env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.endswith(b"\n"))
        self.assertEqual(json.loads(result.stdout), {"marker": "synthetic-control"})
        self.assertEqual(result.stderr, b"")

    def test_oversized_control_record_refuses_without_writing(self):
        read, write = os.pipe()
        try:
            with self.assertRaisesRegex(ValueError, "Invalid private OpenCode"):
                bridge.write_control_record(write, {"marker": "x" * 4096})
        finally:
            os.close(write)
        try:
            self.assertEqual(os.read(read, 128), b"")
        finally:
            os.close(read)
