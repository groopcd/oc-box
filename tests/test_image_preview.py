"""Private CLI image-preview configuration; synthetic files and mocked launches only."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from oc_box import oc_box as box, sync_config, vault, workspace_service


class ImagePreviewTests(unittest.TestCase):
    def setUp(self):
        preferred_temp = Path("/tmp/opencode")
        temporary = tempfile.TemporaryDirectory(
            prefix="oc-box-image-preview-",
            dir=preferred_temp if preferred_temp.is_dir() else None,
        )
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "HOME": str(self.root / "host-home"),
                    "XDG_CONFIG_HOME": str(self.root / "config"),
                    "XDG_DATA_HOME": str(self.root / "data"),
                    "XDG_STATE_HOME": str(self.root / "state"),
                    "OC_BOX_STATE_HOME": str(self.root / "registry"),
                    "OC_BOX_CONTAINER": "0",
                },
                clear=True,
            )
        )
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(box, "check_backend"))
        self.stack.enter_context(patch.object(box, "check_private_output"))
        self.stack.enter_context(patch.object(box.dependencies, "require"))
        self.release = self.stack.enter_context(
            patch.object(box, "detect_opencode_release", return_value="2.3.0")
        )
        self.controller = self.stack.enter_context(
            patch.object(workspace_service, "controller_running", return_value=False)
        )
        self.source = self.root / "config/opencode"
        self.source.mkdir(parents=True)
        self.cli = self.source / "cli.json"
        self.project = self.root / "project"
        self.project.mkdir()
        self.path, self.data = box.create_workspace(self.project, "tumbleweed")
        self.data.update(
            opencode_version="2.3.0",
            ready=True,
            credentials={"mode": "env", "env": [], "vault": []},
        )
        box.atomic_json(self.path, self.data)

    def copied_cli(self, *, enabled=True, data=None):
        with tarfile.open(
            fileobj=io.BytesIO(
                box.config_archive(data or self.data, image_preview=enabled)
            ),
            mode="r:gz",
        ) as archive:
            self.assertEqual(archive.getnames().count("cli.json"), 1)
            member = archive.getmember("cli.json")
            return archive.extractfile(member).read(), member.mode

    def test_flag_is_opt_in_and_default_copy_stays_byte_exact(self):
        self.assertFalse(box.parser().parse_args([]).image_preview)
        self.assertTrue(box.parser().parse_args(["--image-preview"]).image_preview)
        original = b'{"theme":{"name":"fixture"},"prompt":{"image_preview":false}}\n'
        self.cli.write_bytes(original)
        self.assertEqual(self.copied_cli(enabled=False)[0], original)
        enabled, mode = self.copied_cli()
        self.assertEqual(enabled, original)
        self.assertEqual(mode, 0o600)
        self.assertEqual(self.cli.read_bytes(), original)
        self.assertEqual(self.copied_cli(enabled=False)[0], original)

    def test_missing_cli_is_created_only_in_the_archive(self):
        result = json.loads(self.copied_cli()[0])
        self.assertEqual(
            result,
            {
                "$schema": "https://opencode.ai/v2/cli.json",
            },
        )
        self.assertFalse(self.cli.exists())
        self.source.rmdir()
        self.assertEqual(json.loads(self.copied_cli()[0]), result)
        self.assertFalse(self.source.exists())

    def test_nested_image_preferences_and_other_cli_settings_remain_unchanged(self):
        for value in (None, False, True):
            with self.subTest(value=value):
                original = {
                    "$schema": "https://opencode.ai/v2/cli.json",
                    "prompt": {"paste": "full", "editor": False},
                    "session": {"permissions": "prompt", "thinking": "hide"},
                    "plugins": ["fixture.plugin"],
                    "keybinds": {"help.show": False},
                }
                if value is not None:
                    for key in ("prompt", "session"):
                        original[key]["image_preview"] = value
                self.cli.write_text(json.dumps(original))
                before = self.cli.stat()
                copied = json.loads(self.copied_cli()[0])
                self.assertEqual(copied, original)
                self.assertEqual(json.loads(self.cli.read_text()), original)
                after = self.cli.stat()
                self.assertEqual(
                    (after.st_ino, after.st_mtime_ns, after.st_mode, after.st_uid),
                    (before.st_ino, before.st_mtime_ns, before.st_mode, before.st_uid),
                )

    def test_jsonc_input_keeps_unrelated_values_and_host_comments_unchanged(self):
        original = """{
          // A URL and comment delimiters inside strings are not comments.
          "theme": {"name": "https://example.test/a/*b*/",},
          "prompt": {"paste": "compact", /* keep the choice */},
          "session": {"permissions": "prompt",},
        }"""
        self.cli.write_text(original)
        copied = self.copied_cli()[0]
        self.assertEqual(copied, original.encode())
        result = box.parse_jsonc(copied.decode())
        self.assertEqual(result["theme"]["name"], "https://example.test/a/*b*/")
        self.assertEqual(result["prompt"], {"paste": "compact"})
        self.assertEqual(result["session"], {"permissions": "prompt"})
        self.assertEqual(self.cli.read_text(), original)
        original_bytes = original.replace("\n", "\r\n").encode()
        self.cli.write_bytes(original_bytes)
        self.assertEqual(self.copied_cli()[0], original_bytes)
        self.assertEqual(self.cli.read_bytes(), original_bytes)

    def test_invalid_or_nonobject_cli_is_refused_without_overwriting_input(self):
        cases = [
            "{",
            "[]",
            "null",
            "false",
            '{"unrelated":"synthetic-private-excerpt",',
            '{"unrelated":NaN}',
        ]
        cases += [
            json.dumps({key: value})
            for key in ("prompt", "session")
            for value in (None, False, 1, "invalid", [])
        ]
        for original in cases:
            with self.subTest(original=original):
                self.cli.write_text(original)
                with self.assertRaisesRegex(box.BoxError, "valid cli.json") as error:
                    self.copied_cli()
                self.assertNotIn("synthetic-private-excerpt", str(error.exception))
                self.assertEqual(self.cli.read_text(), original)

    def test_unsafe_cli_inputs_are_refused_and_preserved(self):
        target = self.root / "original.json"
        target.write_text('{"theme":{"name":"untouched"}}')
        self.cli.symlink_to(target)
        with self.assertRaisesRegex(box.BoxError, "without symlinks"):
            self.copied_cli()
        self.assertTrue(self.cli.is_symlink())
        self.cli.unlink()
        os.link(target, self.cli)
        with self.assertRaises(box.BoxError):
            self.copied_cli()
        self.cli.unlink()
        self.cli.mkdir()
        with self.assertRaises(box.BoxError):
            self.copied_cli()
        self.cli.rmdir()
        os.mkfifo(self.cli)
        with self.assertRaises(box.BoxError):
            self.copied_cli()
        self.cli.unlink()
        self.cli.write_text("{}")
        self.cli.chmod(0o666)
        with self.assertRaises(box.BoxError):
            self.copied_cli()
        self.assertEqual(self.cli.stat().st_mode & 0o777, 0o666)
        self.cli.chmod(0o600)
        with patch.object(box.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(box.BoxError):
                self.copied_cli()
        self.cli.unlink()
        self.source.rmdir()
        self.source.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(box.BoxError):
            self.copied_cli()
        self.assertTrue(self.source.is_symlink())
        self.assertEqual(target.read_text(), '{"theme":{"name":"untouched"}}')

    def test_config_refresh_seeds_base_without_persisting_one_launch_preview(self):
        original = {
            "prompt": {"image_preview": False, "paste": "full"},
            "session": {"image_preview": False, "permissions": "prompt"},
        }
        self.cli.write_text(json.dumps(original))
        container_home = self.root / "container-home"
        calls = []

        def copy(command, **kwargs):
            # Run only the archive refresh in a disposable synthetic HOME, not
            # Podman, native OpenCode, auth, services, or dependency installers.
            calls.append(command)
            reused = sync_config.sync_config(
                container_home, kwargs["input"], self.data["opencode_version"]
            )
            output = (
                b"Reusing container plugin dependencies.\n"
                if reused
                else b"Plugin dependencies will be resolved by OpenCode if needed.\n"
            )
            return subprocess.CompletedProcess(command, 0, output, b"")

        with (
            patch.object(box, "ensure_opencode_version"),
            patch.object(
                box, "run", return_value=subprocess.CompletedProcess([], 0, "[]", "")
            ),
            patch.object(box.subprocess, "run", side_effect=copy),
        ):
            for enabled in (True, True, False):
                with self.subTest(enabled=enabled):
                    box.prepare_runtime(
                        self.path, self.data, "2.3.0", image_preview=enabled
                    )
                    result = json.loads(
                        (container_home / ".config/opencode/cli.json").read_text()
                    )
                    for key in ("prompt", "session"):
                        self.assertIs(result[key]["image_preview"], False)
                    self.assertEqual(result["session"]["permissions"], "prompt")
                    self.assertEqual(result["prompt"]["paste"], "full")
        self.assertEqual(len(calls), 3)
        self.assertEqual(json.loads(self.cli.read_text()), original)

    def test_start_and_reset_forward_flag_without_saving_workspace_preference(self):
        for command in ("start", "reset"):
            with (
                self.subTest(command=command),
                patch.object(box, "remove_resources"),
                patch.object(box, "check_resource_controllers"),
                patch.object(box, "start_container"),
                patch.object(box, "stop_container"),
                patch.object(box, "prepare_runtime") as prepare,
                patch.object(box, "launch_client", return_value=0),
                patch.object(
                    workspace_service, "Owner", return_value=contextlib.nullcontext()
                ) as owner,
            ):
                self.assertEqual(
                    box.main(
                        [command, "--workspace", self.data["id"], "--image-preview"]
                    ),
                    0,
                )
                self.assertTrue(prepare.call_args.kwargs["image_preview"])
                self.assertIs(owner.call_args.kwargs["image_preview"], True)
                self.assertNotIn("image_preview", box.read_json(self.path))
        with (
            patch.object(box, "launch_environment", return_value=0) as launch,
        ):
            self.assertEqual(box.main(["--workspace", self.data["id"]]), 0)
        self.assertFalse(launch.call_args.args[2].image_preview)

    def test_v1_refusal_precedes_workspace_creation_reset_and_credentials(self):
        self.release.return_value = "1.19.5"
        other = self.root / "fresh-project"
        other.mkdir()
        for arguments in (
            ["--project", str(other)],
            ["--workspace", self.data["id"]],
            ["reset", "--workspace", self.data["id"]],
        ):
            with (
                self.subTest(arguments=arguments),
                patch.object(box, "create_workspace") as create,
                patch.object(box, "remove_resources") as remove,
                patch.object(box, "launch_credentials") as credentials,
                self.assertRaisesRegex(box.BoxError, "native OpenCode 2.x"),
            ):
                box.main([*arguments, "--image-preview"])
            create.assert_not_called()
            remove.assert_not_called()
            credentials.assert_not_called()
        with self.assertRaisesRegex(box.BoxError, "native OpenCode 2.x"):
            self.copied_cli(data={**self.data, "opencode_version": "1.19.5"})

    def test_malformed_reset_config_fails_before_unlock_or_resource_removal(self):
        self.cli.write_text('{"prompt":false}')
        before = self.path.read_bytes()
        with (
            patch.object(box, "launch_credentials") as credentials,
            patch.object(box, "remove_resources") as remove,
            self.assertRaisesRegex(box.BoxError, "valid cli.json"),
        ):
            box.main(["reset", "--workspace", self.data["id"], "--image-preview"])
        credentials.assert_not_called()
        remove.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.cli.read_text(), '{"prompt":false}')

    def test_saved_preview_warns_on_v1_without_replacing_saved_choice(self):
        project = ["--workspace", self.data["id"]]
        self.assertEqual(
            box.main(["preferences", *project, "--image-preview", "on"]), 0
        )
        self.release.return_value = "1.19.5"
        for flags, warned in (([], True), (["--no-image-preview"], False)):
            with (
                self.subTest(warned=warned),
                contextlib.redirect_stdout(io.StringIO()) as output,
                patch.object(box, "launch_environment", return_value=0) as launch,
                patch.object(box, "image_preview_cli_config") as config,
            ):
                self.assertEqual(box.main(["start", *project, *flags]), 0)
                self.assertIs(
                    box.effective_image_preview(launch.call_args.args[2]), False
                )
                self.assertEqual(
                    "Image preview override is off" in output.getvalue(), warned
                )
                config.assert_not_called()
        self.assertTrue(
            box.workspace_ui_preferences(self.path, self.data)["image_preview"]
        )

    def test_credential_wait_mutations_use_exact_preflight_snapshot(self):
        cases = (
            "malformed_json",
            "unsafe_symlink",
            "unsafe_write_access",
            "unsafe_directory",
        )
        for command in ("start", "reset"):
            for case in cases:
                with self.subTest(command=command, mutation=case):
                    if self.cli.is_dir() and not self.cli.is_symlink():
                        (self.cli / "new-setting.json").unlink()
                        self.cli.rmdir()
                    else:
                        self.cli.unlink(missing_ok=True)
                    original = {
                        "theme": {"name": "snapshot-before-credential-wait"},
                        "prompt": {"paste": "full", "image_preview": False},
                        "session": {"permissions": "prompt", "image_preview": False},
                    }
                    self.cli.write_text(json.dumps(original))
                    self.cli.chmod(0o600)
                    target = self.root / "post-preflight-cli.json"
                    target.write_text('{"prompt":false}')
                    target_before = target.read_bytes()
                    container_home = self.root / (command + "-" + case)
                    snapshots, events = [], []
                    read_cli = box.image_preview_cli_config

                    def snapshot():
                        self.assertNotIn("credentials", events, "late CLI reread")
                        value = read_cli()
                        snapshots.append(value)
                        return value

                    @contextlib.contextmanager
                    def credentials(data, args):
                        if "credentials" not in events:
                            self.assertEqual(len(snapshots), 1)
                            if case == "malformed_json":
                                self.cli.write_text("{malformed-after-preflight")
                            elif case == "unsafe_symlink":
                                self.cli.unlink()
                                self.cli.symlink_to(target)
                            elif case == "unsafe_directory":
                                self.cli.unlink()
                                self.cli.mkdir()
                                (self.cli / "new-setting.json").write_text(
                                    "post-preflight-directory-entry"
                                )
                            else:
                                self.cli.chmod(0o666)
                            events.append("credentials")
                        self.assertNotIn("image_preview_snapshot", vars(args))
                        self.assertNotIn("image_preview_snapshot", data)
                        yield {}

                    def remove(*args, **kwargs):
                        self.assertIn("credentials", events)
                        self.assertEqual(len(snapshots), 1)
                        events.append("remove")

                    def copy(arguments, **kwargs):
                        self.assertIn("credentials", events)
                        if command == "reset":
                            self.assertIn("remove", events)
                        events.append("copy")
                        marker = original["theme"]["name"]
                        self.assertNotIn(marker, json.dumps(list(arguments)))
                        self.assertNotIn(marker, json.dumps(arguments.environment))
                        sync_config.sync_config(
                            container_home, kwargs["input"], "2.3.0"
                        )
                        return subprocess.CompletedProcess(
                            arguments,
                            0,
                            b"Plugin dependencies will be resolved by OpenCode if needed.\n",
                            b"",
                        )

                    with (
                        patch.object(
                            box, "image_preview_cli_config", side_effect=snapshot
                        ) as read,
                        patch.object(
                            box, "launch_credentials", side_effect=credentials
                        ),
                        patch.object(box, "remove_resources", side_effect=remove),
                        patch.object(box, "check_resource_controllers"),
                        patch.object(box, "start_container"),
                        patch.object(box, "stop_container"),
                        patch.object(box, "ensure_opencode_version"),
                        patch.object(
                            box,
                            "run",
                            return_value=subprocess.CompletedProcess([], 0, "[]", ""),
                        ),
                        patch.object(box.subprocess, "run", side_effect=copy),
                        patch.object(box, "launch_client", return_value=0),
                        patch.object(
                            workspace_service,
                            "Owner",
                            return_value=contextlib.nullcontext(),
                        ),
                    ):
                        self.assertEqual(
                            box.main(
                                [
                                    command,
                                    "--workspace",
                                    self.data["id"],
                                    "--image-preview",
                                ]
                            ),
                            0,
                        )
                    read.assert_called_once()
                    self.assertEqual(
                        (container_home / ".config/opencode/cli.json").read_bytes(),
                        snapshots[0],
                    )
                    self.assertEqual(json.loads(snapshots[0]), original)
                    self.assertEqual(events.count("copy"), 1)
                    self.assertEqual(events.count("remove"), int(command == "reset"))
                    self.assertNotIn(original["theme"]["name"], self.path.read_text())
                    self.assertEqual(target.read_bytes(), target_before)
                    if case == "malformed_json":
                        self.assertEqual(
                            self.cli.read_text(), "{malformed-after-preflight"
                        )
                    elif case == "unsafe_symlink":
                        self.assertTrue(self.cli.is_symlink())
                        self.assertEqual(self.cli.readlink(), target)
                    elif case == "unsafe_write_access":
                        self.assertEqual(self.cli.stat().st_mode & 0o777, 0o666)
                        self.assertEqual(json.loads(self.cli.read_text()), original)
                    else:
                        self.assertTrue(self.cli.is_dir())
                        self.assertEqual(
                            (self.cli / "new-setting.json").read_text(),
                            "post-preflight-directory-entry",
                        )

    def test_malformed_initial_config_fails_before_registration_or_setup(self):
        project = self.root / "fresh-project"
        project.mkdir()
        self.cli.write_text("[]")
        with (
            patch.object(box, "create_workspace") as create,
            patch.object(box, "start_container") as start,
            patch.object(box, "launch_credentials") as credentials,
            self.assertRaisesRegex(box.BoxError, "valid cli.json"),
        ):
            box.main(["--project", str(project), "--image-preview"])
        create.assert_not_called()
        start.assert_not_called()
        credentials.assert_not_called()
        self.assertFalse((project / box.MARKER).exists())

    def test_runtime_refuses_v1_or_malformed_input_before_install_or_copy(self):
        for version, content in (("1.19.5", "{}"), ("2.3.0", "[]")):
            with (
                self.subTest(version=version),
                patch.object(box, "ensure_opencode_version") as install,
                patch.object(box, "run") as run,
                patch.object(box.subprocess, "run") as copy,
                self.assertRaises(box.BoxError),
            ):
                self.cli.write_text(content)
                box.prepare_runtime(self.path, self.data, version, image_preview=True)
            install.assert_not_called()
            run.assert_not_called()
            copy.assert_not_called()

    def test_nonowning_commands_refuse_without_probing_config_or_credentials(self):
        cases = [
            ["list"],
            ["distros"],
            ["status"],
            ["--status"],
            ["--about"],
            ["--version"],
            ["--json"],
            ["sync"],
            ["stop"],
            ["repair"],
            ["terminal"],
            ["terminal", "--maintenance"],
            ["credentials"],
            ["purge"],
            ["upgrade"],
        ]
        with (
            patch.object(box, "image_preview_cli_config") as config,
            patch.object(vault, "select_all") as unlock,
        ):
            for arguments in cases:
                with self.subTest(arguments=arguments), self.assertRaises(box.BoxError):
                    box.main([*arguments, "--image-preview"])
            with contextlib.redirect_stderr(io.StringIO()):
                for command in ("web", "doctor", "completion", "backup", "secrets"):
                    with self.subTest(command=command), self.assertRaises(SystemExit):
                        box.main([command, "--image-preview"])
        self.release.assert_not_called()
        config.assert_not_called()
        unlock.assert_not_called()

    def test_active_tui_client_cannot_reconfigure_preview_or_refresh_credentials(self):
        self.controller.return_value = True
        self.cli.write_text("{invalid")
        with (
            patch.object(box, "image_preview_cli_config") as config,
            patch.object(workspace_service, "attach") as attach,
            patch.object(box, "launch_credentials") as credentials,
            self.assertRaisesRegex(box.BoxError, "active V2 workspace"),
        ):
            box.main(["--workspace", self.data["id"], "--image-preview"])
        self.release.assert_not_called()
        config.assert_not_called()
        attach.assert_not_called()
        credentials.assert_not_called()

    def client_names(self, connection, arguments=None, *, interactive=True):
        command = workspace_service.client_command(
            box,
            self.data,
            connection,
            arguments
            or [
                box.CONTAINER_OPENCODE,
                box.container_project(self.data),
                "--server",
                connection["url"],
            ],
            interactive=interactive,
        )
        self.assertNotIn("OPENCODE_CLI_CONFIG_CONTENT", command.environment)
        names = json.loads(command[command.index("-c") + 2])
        return command, names

    def test_only_two_image_booleans_reach_flagged_tui_clients(self):
        connection = {
            "url": "http://127.0.0.1:12345",
            "password": "a" * 43,
            "image_preview": True,
        }
        with patch.dict(
            os.environ,
            {
                "OPENCODE_CLI_CONFIG_CONTENT": '{"session":{"permissions":"autoaccept"}}',
                "UNRELATED_TOKEN": "synthetic-private-excerpt",
            },
        ):
            command, names = self.client_names(connection)
        self.assertEqual(
            json.loads(names.pop("OPENCODE_CLI_CONFIG_CONTENT")),
            {"prompt": {"image_preview": True}, "session": {"image_preview": True}},
        )
        self.assertNotIn("--auto", command)
        self.assertNotIn("autoaccept", " ".join(command))
        self.assertNotIn("synthetic-private-excerpt", " ".join(command))
        self.assertNotIn("OPENCODE_CLI_CONFIG_CONTENT", names)
        for enabled in (False, None):
            base = {
                key: value
                for key, value in connection.items()
                if key != "image_preview"
            }
            if enabled is not None:
                base["image_preview"] = enabled
            with self.subTest(enabled=enabled):
                _, names = self.client_names(base)
                self.assertNotIn("OPENCODE_CLI_CONFIG_CONTENT", names)

    def test_image_override_is_not_forwarded_to_api_shell_or_background_clients(self):
        connection = {
            "url": "http://127.0.0.1:12345",
            "password": "a" * 43,
            "image_preview": True,
        }
        for arguments, interactive in (
            ([box.CONTAINER_OPENCODE, "api", "get", "/api/info"], False),
            ([box.CONTAINER_OPENCODE, "export", "ses_fixture"], False),
            (["python3", "-c", "session-terminal-fixture"], True),
            ([box.CONTAINER_OPENCODE, box.container_project(self.data)], False),
        ):
            with self.subTest(arguments=arguments):
                _, names = self.client_names(
                    connection, arguments, interactive=interactive
                )
                self.assertNotIn("OPENCODE_CLI_CONFIG_CONTENT", names)

    def test_malformed_image_metadata_is_not_coerced_to_an_opt_in(self):
        connection = {"url": "http://127.0.0.1:12345", "password": "a" * 43}
        for invalid in (
            None,
            0,
            1,
            "true",
            [],
            {},
            {"session": {"permissions": "autoaccept"}},
        ):
            with self.subTest(invalid=invalid):
                value = {**connection, "image_preview": invalid}
                with self.assertRaisesRegex(ValueError, "image-preview"):
                    workspace_service.validate_endpoint(value)
                with self.assertRaisesRegex(box.BoxError, "image-preview"):
                    self.client_names(value)
                with self.assertRaisesRegex(box.BoxError, "image-preview"):
                    workspace_service.Owner(
                        box, self.path, self.data, image_preview=invalid
                    )

    def test_owner_opt_in_is_ephemeral_and_inherited_by_attached_tui(self):
        program = (
            "import json,sys;"
            "print(json.dumps({'url':'http://127.0.0.1:12345',"
            "'password':'" + "a" * 43 + "'}),flush=True);sys.stdin.read()"
        )

        def local_exec(_data, _arguments):
            return box.ContainerCommand([sys.executable, "-I", "-S", "-c", program], {})

        info = {
            "State": {"Running": True},
            "Mounts": [
                {
                    "Source": self.data["project"],
                    "Destination": box.container_project(self.data),
                }
            ],
        }
        before = self.path.read_bytes()
        for enabled in (True, False):
            with (
                self.subTest(enabled=enabled),
                patch.object(box, "exec_command", side_effect=local_exec),
                patch.object(box, "inspect_owned", return_value=info),
                patch.object(
                    workspace_service.Owner, "default_session", return_value=None
                ),
                workspace_service.Owner(
                    box, self.path, self.data, image_preview=enabled
                ) as owner,
            ):
                owner.publish(None)
                self.assertIs(owner.connection["image_preview"], enabled)
                with workspace_service.attach(box, self.path, self.data) as attached:
                    self.assertIs(attached["image_preview"], enabled)
                    for connection in (owner.connection, attached):
                        _, names = self.client_names(connection)
                        self.assertEqual(
                            "OPENCODE_CLI_CONFIG_CONTENT" in names, enabled
                        )
                self.assertEqual(self.path.read_bytes(), before)
            self.assertIsNone(workspace_service.live_connection())
            self.assertFalse(owner.address.exists())

    def test_existing_native_preferences_survive_flagged_reset_and_later_unflagged_refresh(
        self,
    ):
        self.cli.write_text(
            '{"prompt":{"image_preview":true},"session":{"permissions":"prompt"}}'
        )
        container_home = self.root / "container-home"
        preferences = container_home / ".config/opencode/cli.json"
        preferences.parent.mkdir(parents=True)
        private = b'{"prompt":{"image_preview":false},"session":{"permissions":"autoaccept"},"attention":{"notifications":true}}\n'
        preferences.write_bytes(private)
        for enabled in (True, True, False):
            with self.subTest(enabled=enabled):
                sync_config.sync_config(
                    container_home,
                    box.config_archive(self.data, image_preview=enabled),
                    "2.3.0",
                )
                self.assertEqual(preferences.read_bytes(), private)

    def test_additional_tui_inherits_owner_opt_in_without_config_or_unlock(self):
        self.controller.return_value = True
        self.cli.write_text("{invalid")
        connection = {
            "url": "http://127.0.0.1:12345",
            "password": "a" * 43,
            "image_preview": True,
            "session": None,
        }
        with (
            patch.object(box, "image_preview_cli_config") as config,
            patch.object(
                workspace_service,
                "attach",
                return_value=contextlib.nullcontext(connection),
            ),
            patch.object(box, "launch_credentials") as credentials,
            patch.object(box, "prepare_runtime") as prepare,
            patch.object(box, "run_tui", return_value=0) as tui,
        ):
            self.assertEqual(box.main(["--workspace", self.data["id"]]), 0)
        command = tui.call_args.args[0]
        names = json.loads(command[command.index("-c") + 2])
        self.assertEqual(
            json.loads(names["OPENCODE_CLI_CONFIG_CONTENT"]),
            {"prompt": {"image_preview": True}, "session": {"image_preview": True}},
        )
        self.release.assert_not_called()
        for operation in (config, credentials, prepare):
            operation.assert_not_called()

    def test_attach_warns_saved_preview_is_not_active_on_existing_owner(self):
        project = ["--workspace", self.data["id"]]
        self.assertEqual(
            box.main(["preferences", *project, "--image-preview", "on"]), 0
        )
        self.controller.return_value = True
        connection = {
            "url": "http://127.0.0.1:12345",
            "password": "a" * 43,
            "image_preview": False,
            "session": None,
        }
        with (
            patch.object(
                workspace_service,
                "attach",
                return_value=contextlib.nullcontext(connection),
            ),
            patch.object(box, "image_preview_cli_config") as config,
            patch.object(box, "launch_credentials") as credentials,
            patch.object(box, "run_tui", return_value=0) as tui,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(box.main(["start", *project]), 0)
        self.assertIn(
            "Image preview override is off for the running owner", output.getvalue()
        )
        self.assertNotIn("OPENCODE_CLI_CONFIG_CONTENT", " ".join(tui.call_args.args[0]))
        self.assertTrue(
            box.workspace_ui_preferences(self.path, self.data)["image_preview"]
        )
        config.assert_not_called()
        credentials.assert_not_called()


if __name__ == "__main__":
    unittest.main()
