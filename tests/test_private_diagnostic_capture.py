"""Private diagnostic transport through real runtime capture; no native tools."""

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oc_box import oc_box as box


class PrivateDiagnosticCaptureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="private-diagnostic-api-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = {
            "PATH": os.defpath,
            "HOME": str(self.root / "home"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "OC_BOX_STATE_HOME": str(self.root / "registry"),
            "OC_BOX_CONTAINER": "0",
            "LANG": "C.UTF-8",
        }
        isolated = patch.dict(os.environ, environment, clear=True)
        isolated.start()
        self.addCleanup(isolated.stop)
        self.project = self.root / "project"
        self.project.mkdir()
        self.data = {
            "id": "a" * 32,
            "container": "synthetic-private-diagnostic",
            "home": "/home/opencode",
            "project": str(self.project),
            "opencode_version": "1.19.5",
            "credentials": {"mode": "env", "env": [], "vault": []},
            "env_file": None,
        }
        self.session = "ses_private_capture_fixture"
        self.sentinel = "SYNTHETIC_PRIVATE_NATIVE_CAPTURE"
        self.environment = {"PATH": os.defpath, "CAPTURE_TEST": "synthetic"}
        self.calls = []

    def exported(self, version):
        if version.startswith("2."):
            return {
                "info": {
                    "id": self.session,
                    "location": {"directory": str(self.project)},
                },
                "messages": [
                    {
                        "id": "msg_private_capture_fixture",
                        "type": "assistant",
                        "content": [{"type": "text", "text": self.sentinel}],
                    }
                ],
            }
        return {
            "info": {"id": self.session, "directory": str(self.project)},
            "messages": [],
            "synthetic_transcript": self.sentinel,
        }

    def native(self, *, failure=None, running=True, missing=False, foreign=False):
        """Only substitute native process IO, retaining real capture/dispatch."""

        def execute(command, *, stdout, stderr, **kwargs):
            self.assertEqual(command[0], "podman")
            for stream in (stdout, stderr):
                target = os.readlink(f"/proc/self/fd/{stream.fileno()}")
                self.assertIn("memfd:oc-box-private-output", target)
            self.calls.append((command, kwargs))
            code, output = 0, ""
            if command[1:3] == ["container", "inspect"]:
                if missing or failure == "inspect":
                    code = 125
                output = json.dumps(
                    [
                        {
                            "State": {"Running": running},
                            "Config": {
                                "Labels": {
                                    box.LABEL: "b" * 32 if foreign else self.data["id"]
                                },
                                "Env": ["SYNTHETIC_CAPTURE_KEY=" + self.sentinel],
                            },
                        }
                    ]
                )
            elif command[1:3] == ["container", "exists"]:
                code = 1 if missing else 0
            elif command[1] == "stop":
                code = 125 if failure == "stop" else 0
            elif command[1] == "exec":
                code = 125 if failure == "export" else 0
                output = json.dumps(self.exported(self.data["opencode_version"]))
            else:
                self.fail("unexpected native operation")
            stdout.write(output.encode())
            stderr.write(self.sentinel.encode())
            return subprocess.CompletedProcess(command, code)

        return execute

    @contextlib.contextmanager
    def transport(self, **settings):
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.object(box.credentials, "has_unlocked_values", return_value=False),
            patch.object(
                box.tempfile,
                "TemporaryFile",
                side_effect=AssertionError("plaintext host capture requested"),
            ),
            patch.object(box.subprocess, "run", side_effect=self.native(**settings)),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            yield
        self.assertNotIn(self.sentinel, output.getvalue() + errors.getvalue())

    def test_private_inspection_and_stop_capture_all_nested_dispatches(self):
        for operation, running, stages in (
            ("inspect", True, ["container"]),
            ("stop", True, ["container", "stop"]),
            ("stop", False, ["container"]),
        ):
            with self.subTest(operation=operation, running=running):
                self.calls.clear()
                with self.transport(running=running):
                    if operation == "inspect":
                        result = box.inspect_owned(
                            self.data, env=self.environment, private=True
                        )
                        self.assertIs(result["State"]["Running"], running)
                    else:
                        self.assertIsNone(
                            box.stop_container(
                                self.data, env=self.environment, private=True
                            )
                        )
                self.assertEqual([call[0][1] for call in self.calls], stages)
                self.assertTrue(
                    all(call[1]["env"] == self.environment for call in self.calls)
                )

    def test_private_export_both_majors_uses_real_dispatch_and_memory_capture(self):
        for version in ("1.19.5", "2.2.6"):
            with self.subTest(version=version):
                self.calls.clear()
                self.data["opencode_version"] = version
                with self.transport():
                    result = box.export_container_session(
                        self.data, self.session, private=True
                    )
                self.assertEqual(result, self.exported(version))
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(self.calls[0][0][1], "exec")
                self.assertNotIn(self.sentinel, self.calls[0][0])

    def test_private_native_failures_do_not_expose_native_output(self):
        for operation, version in (
            ("inspect", "1.19.5"),
            ("stop", "1.19.5"),
            ("export", "1.19.5"),
            ("export", "2.2.6"),
        ):
            with self.subTest(operation=operation, version=version):
                self.calls.clear()
                self.data["opencode_version"] = version
                with (
                    self.transport(failure=operation),
                    self.assertRaises(box.BoxError) as raised,
                ):
                    if operation == "inspect":
                        box.inspect_owned(self.data, private=True)
                    elif operation == "stop":
                        box.stop_container(self.data, private=True)
                    else:
                        box.export_container_session(
                            self.data, self.session, private=True
                        )
                self.assertNotIn(self.sentinel, str(raised.exception))

    def test_private_missing_or_foreign_container_keeps_ownership_refusals(self):
        with self.transport(missing=True):
            self.assertIsNone(box.inspect_owned(self.data, private=True))
        with (
            self.transport(foreign=True),
            self.assertRaisesRegex(box.BoxError, "ownership label mismatch"),
        ):
            box.stop_container(self.data, private=True)

    def test_default_inspection_and_stop_keep_existing_dispatch_options(self):
        inspected = {
            "State": {"Running": True},
            "Config": {"Labels": {box.LABEL: self.data["id"]}},
        }
        with patch.object(
            box,
            "podman",
            side_effect=(
                subprocess.CompletedProcess([], 0, json.dumps([inspected])),
                subprocess.CompletedProcess([], 0),
            ),
        ) as native:
            self.assertIsNone(box.stop_container(self.data, env=self.environment))
        self.assertEqual(len(native.call_args_list), 2)
        for call in native.call_args_list:
            self.assertIs(call.kwargs["capture"], True)
            self.assertNotIn("private", call.kwargs)
            self.assertEqual(call.kwargs["env"], self.environment)

    def test_default_v1_export_keeps_existing_capture_options(self):
        with patch.object(
            box,
            "run",
            return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(self.exported("1.19.5"))
            ),
        ) as native:
            self.assertEqual(
                box.export_container_session(self.data, self.session),
                self.exported("1.19.5"),
            )
        self.assertIs(native.call_args.kwargs["capture"], True)
        self.assertEqual(native.call_args.kwargs["timeout"], 60)
        self.assertNotIn("private", native.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
