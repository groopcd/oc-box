"""Launcher admission without a running engine or changing workspace state."""

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from oc_box import file_preview, file_preview_host, oc_box as box, workspace_service

RETIRED_SWITCH = "OC_BOX_EXPERIMENTAL_FILE_OPEN"


class PreviewAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.container_id = "a" * 64
        self.data = {
            "id": "b" * 32,
            "opencode_version": "2.4.1",
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

    def test_cli_flags_are_mutually_exclusive_and_native_launch_only(self):
        for prefix in ([], ["start"], ["reset"], ["--project", "/synthetic", "start"]):
            with self.subTest(prefix=prefix):
                parser = box.parser()
                self.assertIsNone(parser.parse_args(prefix).host_file_open)
                self.assertIs(
                    parser.parse_args([*prefix, "--host-file-open"]).host_file_open,
                    True,
                )
                self.assertIs(
                    parser.parse_args([*prefix, "--no-host-file-open"]).host_file_open,
                    False,
                )
        with patch("sys.stderr"), self.assertRaises(SystemExit) as conflict:
            box.parser().parse_args(["--host-file-open", "--no-host-file-open"])
        self.assertEqual(conflict.exception.code, 2)
        with patch.object(box, "check_backend") as backend:
            for command in (
                "stop",
                "sync",
                "terminal",
                "status",
                "--status",
                "--version",
            ):
                with (
                    self.subTest(command=command),
                    self.assertRaisesRegex(box.BoxError, "only to native start/reset"),
                ):
                    box.main([command, "--host-file-open"])
            backend.assert_not_called()

    def test_explicit_enable_rejects_noninteractive_launch_before_backend(self):
        with (
            patch.object(box.sys.stdin, "isatty", return_value=False),
            patch.object(box, "detect_opencode_release") as native,
            patch.object(box, "check_backend") as backend,
            self.assertRaisesRegex(box.BoxError, "not-interactive"),
        ):
            box.main(["start", "--host-file-open"])
        native.assert_not_called()
        backend.assert_not_called()

    def test_explicit_enable_rejects_non_v2_and_incompatible_arguments(self):
        args = box.parser().parse_args(["reset", "--host-file-open"])
        with (
            patch.object(box.sys.stdin, "isatty", return_value=True),
            patch.object(box.sys.stdout, "isatty", return_value=True),
            patch("oc_box.host_drop.preflight", return_value="konsole"),
        ):
            with self.assertRaisesRegex(box.BoxError, "native-version-unsupported"):
                box.validate_host_file_open(args, "1.19.5")
            with self.assertRaisesRegex(box.BoxError, "custom Podman arguments"):
                box.validate_host_file_open(
                    args, "2.4.1", {"podman_args": ["synthetic-option"]}
                )
            args.clear_podman_args = True
            box.validate_host_file_open(
                args, "2.4.1", {"podman_args": ["synthetic-option"]}
            )

    def test_automatic_installation_default_does_not_authorize_restored_workspace(self):
        with (
            patch("oc_box.host_file_open_policy.enabled", return_value=True) as default,
            patch.dict(os.environ, {RETIRED_SWITCH: "0"}),
        ):
            args = box.parser().parse_args([])
            self.assertTrue(box.effective_host_file_open(args, self.data))
            self.assertFalse(
                box.effective_host_file_open(args, {**self.data, "restored_from": {}})
            )
            self.assertFalse(
                box.effective_host_file_open(
                    box.parser().parse_args(["--no-host-file-open"]), self.data
                )
            )
            self.assertEqual(default.call_count, 1)

    def test_canonical_stable_native_v2_is_eligible_without_a_minor_pin(self):
        for release in ("2.2.6", "2.3.2", "2.4.1", "2.79.42"):
            with self.subTest(release=release):
                self.assertTrue(file_preview.supports(release))
        for release in (
            "1.19.5",
            "3.0.0",
            "2.04.1",
            "2.4.01",
            "2.4.1-beta.1",
            "2.4.1+local",
            "2.4.1\n",
            None,
        ):
            with self.subTest(release=release):
                self.assertFalse(file_preview.supports(release))

    def test_retired_switch_and_private_marker_cannot_activate_off_clients(self):
        with (
            patch.dict(
                os.environ, {RETIRED_SWITCH: "1", file_preview.ROOTS: "synthetic-roots"}
            ),
            patch.object(box, "podman") as engine,
            patch.object(box.sys.stdin, "isatty", return_value=False),
        ):
            for command in (
                self.command(file_preview=False),
                self.command(interactive=False),
                self.command(),
                self.command(["python3", "-I", "-S"]),
            ):
                self.assertNotIn(file_preview.ROOTS, " ".join(command))
                self.assertNotIn(RETIRED_SWITCH, command.environment)
                self.assertNotIn(file_preview.ROOTS, command.environment)
                self.assertFalse(hasattr(command, "preview_host_open"))
            engine.assert_not_called()

    def test_opted_in_client_uses_immutable_id_and_only_supplies_bound_roots(self):
        results = [
            SimpleNamespace(returncode=0, stdout=json.dumps([item]))
            for item in (self.info, self.volume)
        ]
        with (
            patch.dict(os.environ, {RETIRED_SWITCH: "0"}),
            patch.object(box, "podman", side_effect=results) as engine,
            patch.object(
                file_preview, "_mount_identity", side_effect=[("1", "2"), ("3", "4")]
            ),
            patch.object(
                file_preview, "host_export_mounts", return_value=("/initial",)
            ),
            patch.object(file_preview_host, "readiness_reason", return_value=None),
            patch("oc_box.host_drop.preflight", return_value="konsole"),
            patch.object(box.sys.stdin, "isatty", return_value=True),
            patch.object(box.sys.stdout, "isatty", return_value=True),
        ):
            command = self.command()
        self.assertEqual(engine.call_count, 2)
        args, options = engine.call_args_list[0]
        self.assertEqual(args, ("container", "inspect", self.data["container"]))
        self.assertTrue(options["private"])
        self.assertLessEqual(options["timeout"], 5)
        self.assertNotIn(RETIRED_SWITCH, options["env"])
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
                patch("oc_box.host_drop.preflight", return_value="konsole"),
                patch.object(box.sys.stdin, "isatty", return_value=True),
                patch.object(box.sys.stdout, "isatty", return_value=True),
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
            ({**self.data, "opencode_version": "2.4.1-beta.1"}, self.connection),
            (self.data, {**self.connection, "workspace": "c" * 32}),
            (self.data, {**self.connection, "version": "2.3.0"}),
        )
        with (
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

    def test_host_open_disabled_if_staging_is_in_another_mount(self):
        verified = (self.container_id, {"roots": [self.project, self.data["home"]]})
        with (
            patch.object(file_preview, "client_binding", return_value=verified),
            patch.object(
                file_preview, "host_export_mounts", return_value=("/run/user",)
            ),
            patch.object(
                file_preview_host,
                "readiness_reason",
                side_effect=[None, "runtime-staging-overlaps-mount"],
            ),
            patch("oc_box.host_drop.preflight", return_value="konsole"),
            patch.object(box.sys.stdin, "isatty", return_value=True),
            patch.object(box.sys.stdout, "isatty", return_value=True),
            patch("builtins.print") as notice,
        ):
            command = self.command()
        values = json.loads(command[command.index("-c") + 2])
        self.assertNotIn(file_preview.ROOTS, values)
        self.assertFalse(hasattr(command, "preview_host_open"))
        notice.assert_called_once_with(
            "oc-box: Experimental host opening is off (runtime-staging-overlaps-mount).",
            flush=True,
        )

    def test_requested_host_open_reports_early_refusals_without_mount_inspection(self):
        for release, tty, profile, reason in (
            ("1.19.5", True, "konsole", "native-version-unsupported"),
            (None, True, "konsole", "native-version-unverified"),
            ("2.4.1", False, "konsole", "not-interactive"),
            ("2.4.1", True, "kitty", "terminal-profile-unsupported"),
        ):
            self.data["opencode_version"] = release
            with (
                self.subTest(reason=reason),
                patch.object(file_preview, "client_binding") as binding,
                patch.object(file_preview, "host_export_mounts") as inventory,
                patch.object(file_preview_host, "readiness_reason") as desktop,
                patch("oc_box.host_drop.preflight", return_value=profile),
                patch.object(box.sys.stdin, "isatty", return_value=tty),
                patch.object(box.sys.stdout, "isatty", return_value=tty),
                patch("builtins.print") as notice,
            ):
                command = self.command()
                self.assertNotIn(file_preview.ROOTS, " ".join(command))
                self.assertFalse(hasattr(command, "preview_host_open"))
                binding.assert_not_called()
                inventory.assert_not_called()
                desktop.assert_not_called()
                notice.assert_called_once_with(
                    f"oc-box: Experimental host opening is off ({reason}).", flush=True
                )

    def test_saved_opt_in_warns_on_v1_but_does_not_prevent_native_launch(self):
        args = box.parser().parse_args(["start"])
        args._saved_host_file_open = True
        self.data["distro"] = "synthetic"
        self.data["opencode_version"] = "1.19.5"
        with (
            patch.object(box, "refresh_sessions", return_value={}),
            patch.object(box, "exec_command"),
            patch.object(box, "run_native_tui", return_value=0) as tui,
            patch.object(box, "sync_sessions"),
            patch.object(file_preview, "report_unavailable") as notice,
        ):
            self.assertEqual(
                box.launch_client(None, self.data, args, "1.19.5", None), 0
            )
            tui.assert_called_once()
            notice.assert_called_once_with("native-version-unsupported")

    def test_reports_desktop_and_engine_refusals_without_swallowing_native_tui(self):
        verified = (self.container_id, {"roots": [self.project, self.data["home"]]})

        def missing_inventory(*_args, refusals):
            refusals.append("container-mount-layout-unsupported")

        for desktop_reason, expected in (
            ("xdg-mime-unavailable-or-untrusted", "xdg-mime-unavailable-or-untrusted"),
            (None, "container-mount-layout-unsupported"),
        ):
            with (
                self.subTest(reason=expected),
                patch.object(file_preview, "client_binding", return_value=verified),
                patch.object(
                    file_preview, "host_export_mounts", side_effect=missing_inventory
                ) as inventory,
                patch.object(
                    file_preview_host, "readiness_reason", return_value=desktop_reason
                ),
                patch("oc_box.host_drop.preflight", return_value="konsole"),
                patch.object(box.sys.stdin, "isatty", return_value=True),
                patch.object(box.sys.stdout, "isatty", return_value=True),
                patch("builtins.print") as notice,
            ):
                command = self.command()
            notice.assert_called_once_with(
                f"oc-box: Experimental host opening is off ({expected}).", flush=True
            )
            self.assertIn(self.data["container"], command)
            self.assertNotIn(file_preview.ROOTS, " ".join(command))
            self.assertEqual(inventory.call_count, int(desktop_reason is None))

    def test_host_opener_rechecks_private_inventory_after_click(self):
        verified = (self.container_id, {"roots": [self.project, self.data["home"]]})
        with (
            patch.object(file_preview, "client_binding", return_value=verified),
            patch.object(
                file_preview,
                "host_export_mounts",
                side_effect=[("/initial",), None, ("/latest",)],
            ) as mounts,
            patch.object(file_preview_host, "readiness_reason", return_value=None),
            patch("oc_box.host_drop.preflight", return_value="konsole"),
            patch.object(file_preview_host, "open_artifact") as open_artifact,
            patch.object(box.sys.stdin, "isatty", return_value=True),
            patch.object(box.sys.stdout, "isatty", return_value=True),
        ):
            command = self.command()
            self.assertIn(file_preview.ROOTS, " ".join(command))
            open_artifact.assert_not_called()
            with self.assertRaisesRegex(ValueError, "host-mounts-unverified"):
                command.preview_host_open("txt", b"synthetic")
            open_artifact.assert_not_called()
            command.preview_host_open("txt", b"synthetic")
            open_artifact.assert_called_once_with(
                "txt", b"synthetic", host_mounts=("/latest",)
            )
            self.assertEqual(mounts.call_count, 3)

    def test_inventory_lists_stopped_mounts_and_refuses_churn_or_external_volume(self):
        other = "e" * 64
        info2 = {
            "Id": other,
            "Config": {"Labels": {}},
            "State": {"Status": "exited", "Running": False, "Paused": False},
            "Mounts": [{"Type": "bind", "Source": "/other/workspace"}],
        }
        ids = self.container_id + "\n" + other + "\n"
        for replaced, stable, expected, reason in (
            (
                {},
                True,
                (
                    "/synthetic/project",
                    "/synthetic/volume",
                    file_preview_host.InactiveMountSource("/other/workspace"),
                ),
                [],
            ),
            ({}, False, None, ["container-list-changed"]),
            (
                {"Options": {"type": "none", "o": "bind"}},
                True,
                None,
                ["volume-backing-unverified"],
            ),
        ):
            replies = (
                ids,
                json.dumps([info2, self.info]),
                json.dumps([{**self.volume, **replaced}]),
                ids if stable else self.container_id + "\n",
            )
            with (
                self.subTest(replaced=replaced, stable=stable),
                patch.object(box, "workspace_paths", return_value=[]),
                patch.object(
                    box,
                    "podman",
                    side_effect=[
                        SimpleNamespace(returncode=0, stdout=reply) for reply in replies
                    ],
                ) as engine,
            ):
                refusals = []
                self.assertEqual(
                    file_preview.host_export_mounts(
                        box, self.data, self.container_id, refusals=refusals
                    ),
                    expected,
                )
                self.assertEqual(refusals, reason)
                self.assertEqual(
                    engine.call_args_list[0].args,
                    ("--remote=false", "ps", "--all", "--quiet", "--no-trunc"),
                )

    def test_inactive_exception_never_applies_to_paused_unknown_or_shared_sources(self):
        source = "/other/workspace"

        def inventory(state, *, shared=False):
            ids = [self.container_id, "e" * 64]
            entries = [
                self.info,
                {
                    "Id": ids[1],
                    "State": state,
                    "Mounts": [{"Type": "bind", "Source": source}],
                },
            ]
            if shared:
                ids.append("d" * 64)
                entries.append(
                    {
                        "Id": ids[2],
                        "State": {
                            "Status": "running",
                            "Running": True,
                            "Paused": False,
                        },
                        "Mounts": [{"Type": "bind", "Source": source}],
                    }
                )
            listing = "\n".join(ids)
            with (
                patch.object(box, "workspace_paths", return_value=[]),
                patch.object(
                    box,
                    "podman",
                    side_effect=[
                        SimpleNamespace(returncode=0, stdout=reply)
                        for reply in (
                            listing,
                            json.dumps(entries),
                            json.dumps([self.volume]),
                            listing,
                        )
                    ],
                ),
            ):
                return file_preview.host_export_mounts(
                    box, self.data, self.container_id
                )

        stopped = {"Status": "exited", "Running": False, "Paused": False}
        self.assertIn(file_preview_host.InactiveMountSource(source), inventory(stopped))
        for state in (
            {"Status": "paused", "Running": True, "Paused": True},
            {"Status": "restarting", "Running": False, "Paused": False},
            {"Status": "exited", "Running": False},
            {**stopped, "Restarting": True},
        ):
            with self.subTest(state=state):
                self.assertIn(source, inventory(state))
        self.assertIn(source, inventory(stopped, shared=True))
