"""Launcher admission without a running engine or changing workspace state."""

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from oc_box import file_preview, oc_box as box, workspace_service


class PreviewAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.container_id = "a" * 64
        self.data = {
            "id": "b" * 32,
            "opencode_version": "2.3.2",
            "container": "oc-box-probe",
            "home": "/home/probe",
            "home_volume": "oc-box-home-probe",
            "project": "/synthetic/project",
            "container_project": "/home/probe/project",
            "ready": True,
        }
        self.project = box.container_project(self.data)
        self.connection = {
            "workspace": self.data["id"],
            "password": "synthetic-control-secret",
            "url": "http://127.0.0.1:12345",
        }
        self.info = {
            "Id": self.container_id,
            "Config": {"Labels": {box.LABEL: self.data["id"]}},
            "State": {"Running": True},
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": self.data["project"],
                    "Destination": self.project,
                },
                {
                    "Type": "volume",
                    "Name": self.data["home_volume"],
                    "Source": "/synthetic/volume",
                    "Destination": self.data["home"],
                },
            ],
        }

        self.volume = {
            "Name": self.data["home_volume"],
            "Driver": "local",
            "Scope": "local",
            "Options": {},
            "Mountpoint": "/synthetic/volume",
            "Labels": {box.LABEL: self.data["id"]},
        }

    def command(self, args=None, **kwargs):
        return workspace_service.client_command(
            box,
            self.data,
            self.connection,
            args
            or [
                box.CONTAINER_OPENCODE,
                self.project,
                "--server",
                self.connection["url"],
            ],
            interactive=kwargs.pop("interactive", True),
            file_preview=kwargs.pop("file_preview", True),
            **kwargs,
        )

    def test_only_canonical_stable_native_23_is_eligible(self):
        for release in ("2.3.0", "2.3.1", "2.3.2", "2.3.79"):
            self.assertTrue(file_preview.supports(release))
        for release in ("2.2.7", "2.4.0", "2.3.02", "2.3.0-beta.1", "2.3.2\n", None):
            self.assertFalse(file_preview.supports(release))

    def test_absent_opt_in_noninteractive_and_non_tui_do_not_inspect(self):
        with patch.object(box, "podman") as engine:
            with patch.dict(os.environ, {file_preview.FLAG: "0"}):
                self.assertNotIn(file_preview.ROOTS, " ".join(self.command()))
            with patch.dict(os.environ, {file_preview.FLAG: "1"}):
                for command in (
                    self.command(file_preview=False),
                    self.command(interactive=False),
                    self.command(["python3", "-I", "-S"]),
                ):
                    self.assertNotIn(file_preview.ROOTS, " ".join(command))
            engine.assert_not_called()

    def test_opted_in_client_uses_immutable_id_and_only_supplies_bound_roots(self):
        results = [
            SimpleNamespace(returncode=0, stdout=json.dumps([item]))
            for item in (self.info, self.volume)
        ]
        with (
            patch.dict(os.environ, {file_preview.FLAG: "1"}),
            patch.object(box, "podman", side_effect=results) as engine,
            patch.object(
                file_preview, "_mount_identity", side_effect=[("1", "2"), ("3", "4")]
            ),
        ):
            command = self.command()
        self.assertEqual(engine.call_count, 2)
        args, options = engine.call_args_list[0]
        self.assertEqual(args, ("container", "inspect", self.data["container"]))
        self.assertTrue(options["private"])
        self.assertLessEqual(options["timeout"], 5)
        self.assertNotIn(file_preview.FLAG, options["env"])
        self.assertIn(self.container_id, command)
        values = json.loads(command[command.index("-c") + 2])
        self.assertEqual(
            json.loads(values[file_preview.ROOTS]),
            {
                "roots": [self.project, self.data["home"]],
                "identities": [["1", "2"], ["3", "4"]],
            },
        )
        self.assertNotIn(self.connection["password"], " ".join(command))

    def test_refuses_alias_wrong_type_or_shadowed_mounts_without_failing_tui(self):
        mounts = self.info["Mounts"]
        cases = (
            [{**mounts[0], "Type": "volume"}, mounts[1]],
            [mounts[0], {**mounts[1], "Name": "other-volume"}],
            [*mounts, {**mounts[0], "Source": "/synthetic/other"}],
            [*mounts, {"Type": "bind", "Destination": self.project + "/shadow"}],
            [mounts[1]],
        )
        for entries in cases:
            with (
                self.subTest(entries=entries),
                patch.dict(os.environ, {file_preview.FLAG: "1"}),
                patch.object(
                    box,
                    "podman",
                    return_value=SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps([{**self.info, "Mounts": entries}]),
                    ),
                ),
                patch("builtins.print") as notice,
            ):
                command = self.command()
                self.assertNotIn(file_preview.ROOTS, " ".join(command))
                self.assertIn(self.data["container"], command)
                notice.assert_called_once_with(file_preview.NOTICE, flush=True)

    def test_custom_options_wrong_version_and_mismatched_workspace_skip_inspection(
        self,
    ):
        variants = (
            ({**self.data, "podman_args": ["--volume=/synthetic:ro"]}, self.connection),
            ({**self.data, "opencode_version": "2.2.6"}, self.connection),
            (self.data, {**self.connection, "workspace": "c" * 32}),
            (self.data, {**self.connection, "version": "2.3.0"}),
        )
        with (
            patch.dict(os.environ, {file_preview.FLAG: "1"}),
            patch.object(box, "podman") as engine,
            patch("builtins.print"),
        ):
            for data, connection in variants:
                self.assertIsNone(file_preview.client_binding(box, data, connection))
        engine.assert_not_called()

    def test_refuses_unowned_or_bind_backed_volume_before_stat(self):
        for field, value in (
            ("Driver", "external"),
            ("Labels", {}),
            ("Options", {"type": "none", "o": "bind"}),
            ("Mountpoint", "/other"),
        ):
            with (
                self.subTest(field=field),
                patch.object(file_preview, "_mount_identity") as observe,
            ):
                self.assertIsNone(
                    file_preview._volume_source(
                        box,
                        self.data,
                        {**self.volume, field: value},
                        self.volume["Mountpoint"],
                    )
                )
                observe.assert_not_called()
