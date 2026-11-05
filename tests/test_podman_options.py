import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

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

        with patch.object(box, "podman", side_effect=fake):
            box.create_container(self.path, self.data)
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
