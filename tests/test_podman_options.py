import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import call, patch

APP = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("oc_box_options", APP / "oc_box.py")
assert spec is not None and spec.loader is not None
box = importlib.util.module_from_spec(spec)
spec.loader.exec_module(box)


class PodmanOptionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oc-box-options-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(
            os.environ, {"OC_BOX_STATE_HOME": str(self.root / "registry")}
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.project = self.root / "project with spaces"
        self.project.mkdir()
        self.path, self.data = box.create_workspace(self.project, "tumbleweed")

    def test_device_and_extra_option_are_passed_literally_before_image(self):
        self.data["devices"] = [
            "/dev/accel/accel0",
            "/dev/dri/renderD128:/dev/render:rwm",
        ]
        literal = "--annotation=example=value with spaces;$(touch /tmp/not-executed)"
        self.data["podman_args"] = ["--group-add=keep-groups", literal]
        calls = []

        def fake(*args, **kwargs):
            calls.append(args)
            if args[:2] == ("volume", "exists"):
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[:2] == ("volume", "inspect"):
                return subprocess.CompletedProcess(
                    args, 0, json.dumps([{"Labels": {box.LABEL: self.data["id"]}}]), ""
                )
            return subprocess.CompletedProcess(args, 0, "", "")

        with (
            patch.object(box, "podman", side_effect=fake),
            patch.object(box, "warn_device_access") as preflight,
        ):
            box.create_container(self.path, self.data)
        preflight.assert_called_once_with(self.data)
        command = next(command for command in calls if command[0] == "create")
        for value in (
            "--device=/dev/accel/accel0",
            "--device=/dev/dri/renderD128:/dev/render:rwm",
            "--group-add=keep-groups",
            literal,
        ):
            self.assertIn(value, command)
            self.assertLess(command.index(value), command.index(self.data["image"]))
        self.assertIn("--name", command)
        self.assertIn("--label", command)

    def test_saved_settings_append_deduplicate_and_survive_plain_launches(self):
        args = box.parser().parse_args(
            [
                "reset",
                "--device",
                "/dev/accel/accel0",
                "--device",
                "/dev/accel/accel0",
                "--podman-arg=--group-add=keep-groups",
            ]
        )
        box.configure_podman_options(self.data, args)
        self.assertEqual(self.data["devices"], ["/dev/accel/accel0"])
        self.assertEqual(self.data["podman_args"], ["--group-add=keep-groups"])
        with patch.object(box, "inspect_owned") as inspect:
            box.configure_podman_options(self.data, box.parser().parse_args([]))
        inspect.assert_not_called()
        self.assertEqual(self.data["devices"], ["/dev/accel/accel0"])
        args = box.parser().parse_args(
            [
                "reset",
                "--clear-devices",
                "--clear-podman-args",
                "--device",
                "/dev/accel/accel1",
            ]
        )
        box.configure_podman_options(self.data, args)
        self.assertEqual(self.data["devices"], ["/dev/accel/accel1"])
        self.assertEqual(self.data["podman_args"], [])

    def test_running_or_stopped_container_requires_explicit_reset_to_change_options(
        self,
    ):
        args = box.parser().parse_args(["--device", "/dev/accel/accel0"])
        with patch.object(
            box, "inspect_owned", return_value={"State": {"Running": False}}
        ):
            with self.assertRaisesRegex(box.BoxError, "requires 'oc-box reset'"):
                box.configure_podman_options(self.data, args)
        self.assertNotIn("devices", self.data)
        with patch.object(box, "inspect_owned", return_value=None):
            box.configure_podman_options(self.data, args)
        self.assertEqual(self.data["devices"], ["/dev/accel/accel0"])

    def test_managed_identity_and_nonoptions_are_rejected(self):
        for value in (
            "alpine",
            "-v=/home:/host",
            "--",
            "--name=other",
            "--label=io.oc-box.workspace=other",
            "--entrypoint=bash",
            "--user=1000",
            "--rm",
            "--replace",
            "--workdir=/",
            "--memory=2g",
            "--cpus=8",
            "--pids-limit=100",
            "--group-add\n--privileged",
        ):
            with self.subTest(value=value), self.assertRaises(box.BoxError):
                box.validate_podman_arg(value)
        for value in (
            "--group-add=keep-groups",
            "--runtime=crun",
            "--security-opt=label=disable",
            "--shm-size=1g",
            "--ipc=private",
        ):
            box.validate_podman_arg(value)

    def test_device_syntax(self):
        for value in (
            "/dev/accel/accel0",
            "/dev/accel/accel0:/dev/npu:rwm",
            "/dev/accel/accel0:rw",
            "nvidia.com/gpu=all",
        ):
            box.validate_device(value)
        for value in (
            "",
            "relative/device",
            "/dev/accel/accel0:",
            "/dev/a:/dev/b:bad",
            "/dev/a:bad",
            "/dev/a\n--privileged",
        ):
            with self.subTest(value=value), self.assertRaises(box.BoxError):
                box.validate_device(value)

    def test_device_warning_reports_enforcing_policy_off_without_modifying_host(self):
        self.data["devices"] = ["/dev/accel/accel0"]
        output = io.StringIO()
        with (
            patch.object(box.os, "access", return_value=True),
            patch.object(
                box,
                "selinux_query",
                side_effect=["Enforcing", "container_use_devices --> off"],
            ) as query,
            contextlib.redirect_stderr(output),
            patch.object(box, "podman") as podman,
        ):
            box.warn_device_access(self.data)
        self.assertEqual(
            query.call_args_list,
            [
                call("getenforce"),
                call("getsebool", "container_use_devices"),
            ],
        )
        text = output.getvalue()
        self.assertIn("warning: SELinux", text)
        self.assertIn("sudo /usr/sbin/setsebool -P container_use_devices on", text)
        self.assertIn("host-wide", text)
        self.assertIn("without a reset or reboot", text)
        podman.assert_not_called()

    def test_no_selinux_warning_with_enabled_policy_or_non_enforcing_host(self):
        self.data["devices"] = ["/dev/accel/accel0"]
        for responses in (
            ["Enforcing", "container_use_devices --> on"],
            ["Permissive"],
            ["Disabled"],
            [None],
        ):
            with (
                self.subTest(responses=responses),
                patch.object(box.os, "access", return_value=True),
                patch.object(box, "selinux_query", side_effect=responses),
                contextlib.redirect_stderr(io.StringIO()) as output,
            ):
                box.warn_device_access(self.data)
                self.assertEqual(output.getvalue(), "")

    def test_device_warning_covers_cdi_and_podman_arg_but_not_unrelated_containers(
        self,
    ):
        for settings in (
            {"devices": ["vendor.example/npu=all"]},
            {"podman_args": ["--device=/dev/accel/accel0"]},
        ):
            with (
                self.subTest(settings=settings),
                patch.object(box.os, "access", return_value=True),
                patch.object(
                    box,
                    "selinux_query",
                    side_effect=["Enforcing", "container_use_devices --> off"],
                ),
                contextlib.redirect_stderr(io.StringIO()) as output,
            ):
                box.warn_device_access(settings)
                self.assertIn("SELinux", output.getvalue())
        with (
            patch.object(box, "selinux_query") as query,
            patch.object(box.os, "access") as access,
        ):
            box.warn_device_access({"podman_args": ["--shm-size=1g"]})
        query.assert_not_called()
        access.assert_not_called()

    def test_missing_policy_query_is_reported_as_unverified_not_off(self):
        self.data["devices"] = ["/dev/accel/accel0"]
        with (
            patch.object(box.os, "access", return_value=True),
            patch.object(box, "selinux_query", side_effect=["Enforcing", None]),
            contextlib.redirect_stderr(io.StringIO()) as output,
        ):
            box.warn_device_access(self.data)
        self.assertIn("could not be verified", output.getvalue())
        self.assertNotIn("is off", output.getvalue())

    def test_requested_device_permissions_are_checked_without_opening_device(self):
        settings = {
            "devices": [
                "/dev/accel/accel0:r",
                "/dev/other:/dev/mapped:w",
                "/dev/mknod:m",
            ]
        }
        with (
            patch.object(box.os, "access", return_value=False) as access,
            patch.object(box, "selinux_query", return_value="Disabled"),
            contextlib.redirect_stderr(io.StringIO()) as output,
        ):
            box.warn_device_access(settings)
        self.assertEqual(
            access.call_args_list,
            [
                call("/dev/accel/accel0", os.R_OK),
                call("/dev/other", os.W_OK),
            ],
        )
        self.assertIn("Host device", output.getvalue())
        self.assertIn("keep-groups", output.getvalue())

    def test_explicit_label_bypass_suppresses_selinux_warning_not_unix_access_check(
        self,
    ):
        for option in (
            "--security-opt=label=disable",
            "--privileged",
            "--privileged=true",
        ):
            with (
                self.subTest(option=option),
                patch.object(box.os, "access", return_value=False) as access,
                patch.object(box, "selinux_query") as query,
                contextlib.redirect_stderr(io.StringIO()) as output,
            ):
                box.warn_device_access(
                    {"devices": ["/dev/accel/accel0"], "podman_args": [option]}
                )
                self.assertIn("Host device", output.getvalue())
                self.assertNotIn("SELinux", output.getvalue())
                access.assert_called_once()
                query.assert_not_called()

    def test_selinux_helpers_are_found_outside_path_and_queries_are_bounded(self):
        result = subprocess.CompletedProcess([], 0, "Enforcing\n", "")
        with (
            patch.object(
                box.shutil, "which", side_effect=[None, "/usr/sbin/getenforce"]
            ) as which,
            patch.object(box, "run", return_value=result) as run,
        ):
            self.assertEqual(box.selinux_query("getenforce"), "Enforcing")
        self.assertEqual(which.call_args_list[1].kwargs["path"], "/usr/sbin:/sbin")
        self.assertEqual(run.call_args.args[0], ["/usr/sbin/getenforce"])
        self.assertEqual(run.call_args.kwargs["timeout"], 5)
        self.assertFalse(run.call_args.kwargs["check"])

    def test_failed_or_unavailable_selinux_tools_do_not_block_creation(self):
        with (
            patch.object(box.shutil, "which", return_value=None),
            patch.object(box, "run") as run,
        ):
            self.assertIsNone(box.selinux_query("getenforce"))
        run.assert_not_called()
        for failure in (
            box.BoxError("timed out"),
            PermissionError("access denied"),
        ):
            with (
                self.subTest(failure=failure),
                patch.object(box.shutil, "which", return_value="/usr/sbin/getsebool"),
            ):
                with patch.object(box, "run", side_effect=failure):
                    self.assertIsNone(
                        box.selinux_query("getsebool", "container_use_devices")
                    )
        with (
            patch.object(box.shutil, "which", return_value="/usr/sbin/getsebool"),
            patch.object(
                box,
                "run",
                return_value=subprocess.CompletedProcess([], 1, "", "unavailable"),
            ),
        ):
            self.assertIsNone(box.selinux_query("getsebool", "container_use_devices"))

    def test_device_flags_are_not_silently_ignored_by_other_commands(self):
        for args in (
            ["list", "--device", "/dev/null"],
            ["--status", "--podman-arg=--shm-size=1g"],
            ["stop", "--clear-devices"],
            ["sync", "--device", "/dev/null"],
        ):
            with self.subTest(args=args), patch.object(box, "check_backend") as backend:
                with self.assertRaises(box.BoxError):
                    box.main(args)
                backend.assert_not_called()

    def test_reset_preserves_session_and_project_while_persisting_device_settings(self):
        work = self.project / "unfinished.txt"
        work.write_text("unfinished edits")
        saved = {"last": "ses_keep", "sessions": ["ses_keep"]}
        box.atomic_json(box.session_file(self.path), saved)
        home_volume = self.data["home_volume"]
        with (
            patch.object(box, "check_backend"),
            patch.object(box, "check_resource_controllers"),
            patch.object(box, "remove_resources") as remove,
            patch.object(box, "launch", return_value=0) as launch,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                box.main(
                    [
                        "reset",
                        "--project",
                        str(self.project),
                        "--device",
                        "/dev/accel/accel0",
                        "--podman-arg=--group-add=keep-groups",
                    ]
                ),
                0,
            )
        remove.assert_called_once()
        self.assertTrue(remove.call_args.kwargs["keep_home"])
        data = box.read_json(self.path)
        self.assertEqual(data["devices"], ["/dev/accel/accel0"])
        self.assertEqual(data["podman_args"], ["--group-add=keep-groups"])
        self.assertEqual(data["home_volume"], home_volume)
        self.assertEqual(box.session_state(self.path), saved)
        self.assertEqual(work.read_text(), "unfinished edits")
        self.assertEqual(launch.call_args.args[1]["devices"], ["/dev/accel/accel0"])


if __name__ == "__main__":
    unittest.main()
