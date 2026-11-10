"""Behavioral stdin/cleanup contracts for the noninteractive native smoke helpers."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest


APP = Path(__file__).resolve().parents[1]
HELPERS = ("smoke_mainline", "smoke_v2")
DRIVER = """
import importlib,json,os,stat,sys
from pathlib import Path
from unittest.mock import patch

module,case,tests,cwd,pid_file=sys.argv[1:]
sys.path.insert(0,tests)
fifo=stat.S_ISFIFO(os.fstat(0).st_mode)
if case=='import':
    with patch('subprocess.Popen',side_effect=AssertionError('import started a process')):
        importlib.import_module(module)
    result={'kind':'imported'}
else:
    helper=importlib.import_module(module).bounded
    program='''import os,signal,sys,time
from pathlib import Path
Path(sys.argv[1]).write_text(str(os.getpgrp()))
signal.alarm(8)
'''
    if case=='timeout':
        program+="print('STDOUT_TIMEOUT',flush=True)\\nprint('STDERR_TIMEOUT',file=sys.stderr,flush=True)\\ntime.sleep(30)\\n"
    else:
        program+="assert sys.stdin.buffer.read()==b''\\n"
        program+=("print('STDOUT_OK')\\nprint('STDERR_OK',file=sys.stderr)\\n" if case=='success'
                  else "print('STDOUT_FAILURE')\\nprint('STDERR_FAILURE',file=sys.stderr)\\nsys.exit(7)\\n")
    command=([str(Path(cwd)/'missing-executable')] if case=='spawn error'
             else [sys.executable,'-B','-c',program,pid_file])
    try:
        value=helper(command,env=dict(os.environ),cwd=Path(cwd),timeout=1)
        result={'kind':'returned','value':value}
    except AssertionError as error:
        result={'kind':'assertion','message':str(error)}
    except FileNotFoundError:
        result={'kind':'spawn error'}
print(json.dumps({'driver_stdin_is_fifo':fifo,**result}),flush=True)
"""


class NativeStdinContractTests(unittest.TestCase):
    def drive(self, module, case):
        with tempfile.TemporaryDirectory(prefix="oc-box-native-stdin-") as temporary:
            root = Path(temporary)
            fifo = root / "driver.fifo"
            os.mkfifo(fifo, 0o600)
            pid_file = root / "owned-child-group"
            env = {
                "PATH": os.environ["PATH"],
                "HOME": str(root),
                "LANG": "C.UTF-8",
                "TMPDIR": str(root),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            # Keep the writer open through communicate: EOF cannot come from
            # the driver inheriting a conveniently closed test-runner stdin.
            descriptor = os.open(fifo, os.O_RDWR)
            process = None
            try:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-B",
                        "-c",
                        DRIVER,
                        module,
                        case,
                        str(APP / "tests"),
                        str(root),
                        str(pid_file),
                    ],
                    env=env,
                    cwd=APP,
                    stdin=descriptor,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
                output, errors = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, errors)
                result = json.loads(output)
                self.assertTrue(result["driver_stdin_is_fifo"])
                if pid_file.exists():
                    child_group = int(pid_file.read_text())
                    with self.assertRaises(ProcessLookupError):
                        os.killpg(child_group, 0)
                return result
            finally:
                # The helper's child has its own process group. On a broken
                # helper/driver, clean it separately from the outer driver.
                if pid_file.exists():
                    try:
                        os.killpg(int(pid_file.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process is not None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.communicate(timeout=5)
                os.close(descriptor)

    def test_held_open_fifo_driver_gets_eof_and_preserves_return_shape(self):
        for module in HELPERS:
            with self.subTest(helper=module):
                result = self.drive(module, "success")
                self.assertEqual(result["kind"], "returned", result)
                self.assertEqual(
                    result["value"],
                    ["STDOUT_OK\n", "STDERR_OK\n"]
                    if module == "smoke_mainline"
                    else "STDOUT_OK\n",
                )

    def test_nonzero_exit_preserves_captured_failure_output(self):
        for module in HELPERS:
            with self.subTest(helper=module):
                result = self.drive(module, "failure")
                self.assertEqual(result["kind"], "assertion", result)
                self.assertEqual(result["message"], "STDOUT_FAILURE\nSTDERR_FAILURE\n")

    def test_timeout_preserves_diagnostics_and_reaps_owned_child(self):
        for module in HELPERS:
            with self.subTest(helper=module):
                result = self.drive(module, "timeout")
                self.assertEqual(result["kind"], "assertion", result)
                self.assertEqual(
                    result["message"],
                    "Smoke test stopped after 1s. Last output:\n"
                    "STDOUT_TIMEOUT\n\nSTDERR_TIMEOUT\n"
                    if module == "smoke_mainline"
                    else "V2 smoke timeout: STDOUT_TIMEOUT\nSTDERR_TIMEOUT\n",
                )

    def test_spawn_errors_are_not_reclassified(self):
        for module in HELPERS:
            with self.subTest(helper=module):
                result = self.drive(module, "spawn error")
                self.assertEqual(result["kind"], "spawn error")

    def test_importing_smoke_modules_starts_no_native_process(self):
        for module in HELPERS:
            with self.subTest(helper=module):
                result = self.drive(module, "import")
                self.assertEqual(result["kind"], "imported")


if __name__ == "__main__":
    unittest.main()
