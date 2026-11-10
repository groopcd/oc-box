"""Restore's opt-in private creation transport; no actual Podman processes."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from oc_box import oc_box as box


class PrivateContainerCreationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-private-create-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        environment = patch.dict(
            os.environ,
            {
                "HOME": str(self.root / "home"),
                "OC_BOX_STATE_HOME": str(self.root / "state"),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_DATA_HOME": str(self.root / "data"),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.path, self.data = box.create_workspace(self.project, "tumbleweed")
        self.data["image"] = "private-image-sentinel"
        self.sentinel = "native-private-output-sentinel"
        self.ident = "c" * 64

    def native(self, failure=None, inspection=None):
        """Use real run/capture/error handling; replace only subprocess transport."""

        def execute(command, *, stdout=None, stderr=None, **kwargs):
            stage = tuple(command[1:3]) if command[1] == "volume" else (command[1],)
            code = 0
            output = self.sentinel
            if stage == ("volume", "exists"):
                code = 2 if failure == "exists" else 1
            elif stage == ("volume", "create"):
                code = 1 if failure == "volume" else 0
            elif stage == ("volume", "inspect"):
                code = 1 if failure == "inspect" else 0
                output = (
                    inspection
                    if inspection is not None
                    else json.dumps([{"Labels": {box.LABEL: self.data["id"]}}])
                )
            elif stage == ("create",):
                code = 1 if failure == "create" else 0
                output = self.ident + "\n"
            else:
                self.fail("unexpected native operation")
            if stdout is None:
                print(output, end="")
            else:
                stdout.write(output.encode())
            if stderr is None:
                print(self.sentinel, file=__import__("sys").stderr)
            else:
                stderr.write(self.sentinel.encode())
            return subprocess.CompletedProcess(command, code)

        return execute

    def test_private_creation_captures_every_dispatch_and_returns_native_result(self):
        output, errors = io.StringIO(), io.StringIO()
        environment = {"HOME": str(self.root / "runtime-home")}
        with (
            patch.object(box.subprocess, "run", side_effect=self.native()),
            patch.object(box, "podman", wraps=box.podman) as dispatch,
            patch.object(box, "captured_output", wraps=box.captured_output) as captures,
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            result = box.create_container(
                self.path, self.data, environment=environment, private=True
            )
        self.assertIsInstance(result, subprocess.CompletedProcess)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, self.ident + "\n")
        self.assertEqual(result.stderr, self.sentinel)
        self.assertEqual(
            [call.args[:2] for call in dispatch.call_args_list],
            [
                ("volume", "exists"),
                ("volume", "create"),
                ("volume", "inspect"),
                ("create", "--name"),
            ],
        )
        for call in dispatch.call_args_list:
            self.assertIs(call.kwargs["capture"], True)
            self.assertIs(call.kwargs["private"], True)
            self.assertEqual(call.kwargs["env"], environment)
        self.assertTrue(captures.call_args_list)
        self.assertTrue(all(call.kwargs["private"] for call in captures.call_args_list))
        self.assertNotIn(self.sentinel, output.getvalue() + errors.getvalue())
        self.assertNotIn(self.ident, output.getvalue() + errors.getvalue())
        self.assertNotIn(self.data["image"], output.getvalue() + errors.getvalue())
        self.assertIs(box.read_json(self.path)["ready"], False)

    def test_private_native_failures_do_not_disclose_output_or_persist_ready(self):
        before = self.path.read_bytes()
        for stage in ("exists", "volume", "inspect", "create"):
            with self.subTest(stage=stage):
                output, errors = io.StringIO(), io.StringIO()
                with (
                    patch.object(box.subprocess, "run", side_effect=self.native(stage)),
                    patch.object(box, "podman", wraps=box.podman) as dispatch,
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(errors),
                    self.assertRaises(box.BoxError) as raised,
                ):
                    box.create_container(self.path, self.data, private=True)
                text = output.getvalue() + errors.getvalue() + str(raised.exception)
                self.assertNotIn(self.sentinel, text)
                self.assertNotIn(self.ident, text)
                self.assertNotIn(self.data["image"], text)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertNotIn("ready", self.data)
                for call in dispatch.call_args_list:
                    self.assertIs(call.kwargs["capture"], True)
                    self.assertIs(call.kwargs["private"], True)

    def test_volume_inspection_requires_exactly_one_object_before_use(self):
        cases = {
            "invalid JSON": self.sentinel,
            "null": "null",
            "object": "{}",
            "empty": "[]",
            "multiple": json.dumps(
                [{"Labels": {box.LABEL: self.data["id"]}}, {"private": self.sentinel}]
            ),
            "nonobject entry": json.dumps([self.sentinel]),
            "invalid labels": json.dumps([{"Labels": self.sentinel}]),
        }
        before = self.path.read_bytes()
        for private in (False, True):
            for name, response in cases.items():
                with self.subTest(private=private, response=name):
                    with (
                        patch.object(
                            box.subprocess,
                            "run",
                            side_effect=self.native(inspection=response),
                        ),
                        patch.object(box, "podman", wraps=box.podman) as dispatch,
                        contextlib.redirect_stdout(io.StringIO()),
                        contextlib.redirect_stderr(io.StringIO()),
                        self.assertRaisesRegex(
                            box.BoxError, "Invalid private home volume inspection"
                        ) as raised,
                    ):
                        box.create_container(
                            self.path,
                            self.data,
                            **({"private": True} if private else {}),
                        )
                    self.assertNotIn(self.sentinel, str(raised.exception))
                    self.assertFalse(
                        any(
                            call.args[0] == "create" for call in dispatch.call_args_list
                        )
                    )
                    self.assertEqual(self.path.read_bytes(), before)

    def test_default_creation_keeps_normal_output_and_none_return(self):
        output = io.StringIO()
        with (
            patch.object(box.subprocess, "run", side_effect=self.native()),
            patch.object(box, "podman", wraps=box.podman) as dispatch,
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertIsNone(box.create_container(self.path, self.data))
        self.assertIn(self.data["image"], output.getvalue())
        self.assertIn(self.ident, output.getvalue())
        final = dispatch.call_args_list[-1]
        self.assertEqual(final.args[0], "create")
        self.assertNotIn("capture", final.kwargs)
        self.assertNotIn("private", final.kwargs)
