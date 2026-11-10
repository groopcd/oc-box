"""Offline assertion-guard contracts; NEVER live host/Podman acceptance."""

import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "tools/test_backup_podman.py"
REFUSAL = "backup fixture: category=optimization-refused\n"

# Execute the real entrypoint, not a copied guard. All subprocesses have private
# HOME/XDG/temp roots, explicit stdin EOF and a finite timeout. These tripwires
# prevent application imports and downstream allocation/engine/probe/credential
# activity even when the production optimization guard is absent or regresses.
CHILD = r"""
import builtins
import getpass
import os
import runpy
import shutil
import socket
import subprocess
import sys
import tempfile

mode, script, *arguments = sys.argv[1:]

def tripwire(category):
    print("backup test tripwire: category=" + category, file=sys.stderr)
    raise SystemExit(90)

original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if mode == "import-blocked" and (name == "oc_box" or name.startswith("oc_box.")):
        tripwire("application-import")
    return original_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
tempfile.mkdtemp = lambda *a, **k: tripwire("allocation")
subprocess.run = lambda *a, **k: tripwire("engine")
subprocess.Popen = lambda *a, **k: tripwire("engine")
shutil.which = lambda *a, **k: tripwire("executable-probe")
os.geteuid = lambda: tripwire("host-probe")
getpass.getpass = lambda *a, **k: tripwire("credentials")
builtins.input = lambda *a, **k: tripwire("credentials")
sys.addaudithook(
    lambda event, args: tripwire("network") if event.startswith("socket.") else None
)
if mode == "allocation-control":
    os.geteuid = lambda: 1000
sys.argv = [script, *arguments]
runpy.run_path(script, run_name="__main__")
"""


class BackupHostFixtureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="backup-guard-unit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.environment = {
            "PATH": os.defpath,
            "HOME": str(self.root / "home"),
            "TMPDIR": str(self.root / "tmp"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "OC_BOX_STATE_HOME": str(self.root / "registry"),
            "OC_BOX_CONTAINER": "0",
            "LANG": "C.UTF-8",
        }
        for key, value in self.environment.items():
            if key in ("HOME", "TMPDIR") or key.startswith("XDG_"):
                Path(value).mkdir(mode=0o700)

    def child(self, optimization, arguments, *, mode="import-blocked"):
        before = sorted(path.relative_to(self.root) for path in self.root.rglob("*"))
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                *([optimization] if optimization else []),
                "-c",
                CHILD,
                mode,
                str(SCRIPT),
                *arguments,
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            cwd=self.root,
            env=self.environment,
        )
        self.assertEqual(
            sorted(path.relative_to(self.root) for path in self.root.rglob("*")),
            before,
        )
        return result

    def test_actual_optimized_entrypoints_refuse_before_imports_or_host_effects(self):
        for optimization in ("-O", "-OO"):
            for name, arguments in (
                ("help", ["--help"]),
                ("default-run", []),
                ("image-run", ["--image", "synthetic-image"]),
                ("cleanup", ["--cleanup", str(self.root / "retained")]),
            ):
                with self.subTest(optimization=optimization, entrypoint=name):
                    result = self.child(optimization, arguments)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(result.stderr, REFUSAL)

    def test_unoptimized_entrypoint_controls_reach_real_import_and_allocation(self):
        for mode, category in (
            ("import-blocked", "application-import"),
            ("allocation-control", "allocation"),
        ):
            with self.subTest(mode=mode):
                result = self.child(None, [], mode=mode)
                self.assertEqual(result.returncode, 90, result.stderr)
                self.assertEqual(
                    result.stderr,
                    "backup test tripwire: category=" + category + "\n",
                )
                self.assertEqual(result.stdout, "")

    def test_unoptimized_help_keeps_cli_without_probes_or_allocation(self):
        result = self.child(None, ["--help"], mode="help-control")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("--image", result.stdout)
        self.assertIn("--cleanup", result.stdout)

    def test_unoptimized_guard_calls_are_effect_free(self):
        from tools import test_backup_podman as runner

        with contextlib.ExitStack() as stack:
            boundaries = [
                stack.enter_context(
                    patch.object(
                        owner,
                        name,
                        side_effect=AssertionError("unexpected host effect"),
                    )
                )
                for owner, name in (
                    (runner.box, "check_backend"),
                    (runner.box, "run"),
                    (runner.backups, "command"),
                    (runner.crypto, "environment"),
                    (runner.crypto, "tools"),
                    (runner.crypto, "small"),
                    (runner.tempfile, "mkdtemp"),
                    (runner.os, "geteuid"),
                )
            ]
            self.assertIsNone(runner.require_assertion_oracles())
            self.assertIsNone(runner.require_assertion_oracles())
            for boundary in boundaries:
                boundary.assert_not_called()

    def test_unoptimized_cli_dispatch_keeps_default_image_override_and_cleanup(self):
        from tools import test_backup_podman as runner

        for name, arguments, image, cleanup in (
            ("default", [], "docker.io/library/alpine:latest", None),
            ("image", ["--image", "synthetic-image"], "synthetic-image", None),
            ("cleanup", ["--cleanup", str(self.root)], None, self.root),
        ):
            with (
                self.subTest(name=name),
                patch.dict(os.environ, self.environment, clear=True),
                patch.object(sys, "argv", [str(SCRIPT), *arguments]),
                patch.object(runner.os, "geteuid", return_value=1000),
                patch.object(
                    runner.tempfile, "mkdtemp", return_value=str(self.root)
                ) as allocate,
                patch.object(runner, "run") as run,
                patch.object(runner, "cleanup") as clean,
                patch.object(
                    runner.box, "run", side_effect=AssertionError("engine tripwire")
                ) as engine,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertIsNone(runner.main())
                if cleanup is None:
                    allocate.assert_called_once_with(prefix="oc-box-live-backup-")
                    run.assert_called_once_with(self.root, image)
                    clean.assert_not_called()
                else:
                    allocate.assert_not_called()
                    run.assert_not_called()
                    clean.assert_called_once_with(cleanup)
                engine.assert_not_called()


if __name__ == "__main__":
    unittest.main()
