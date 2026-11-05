import argparse
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("oc_box", APP / "oc_box.py")
box = importlib.util.module_from_spec(spec)
spec.loader.exec_module(box)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oc-box-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "project with spaces"
        self.project.mkdir()
        self.env = patch.dict(
            os.environ,
            {
                "OC_BOX_STATE_HOME": str(self.root / "state"),
                "XDG_CONFIG_HOME": str(self.root / "config"),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.path, self.data = box.create_workspace(self.project, "tumbleweed")

    def test_default_and_aliases(self):
        release = self.root / "os-release"
        release.write_text('ID="opensuse-tumbleweed"\n')
        self.assertEqual(box.host_default(release), "tumbleweed")
        self.assertEqual(box.distro_name("opensuse"), "tumbleweed")
        with self.assertRaises(box.BoxError):
            box.distro_name("ubuntu; touch /host")

    def test_list_shows_latest_recorded_container_session_without_podman(self):
        cases = (
            (None, "none"),
            ({"sessions": [], "last": None}, "none"),
            (
                {
                    "sessions": ["ses_old", "ses_latest"],
                    "last": "ses_latest",
                    "host_sessions": {"ses_latest": "ses_host_archive"},
                },
                "ses_latest",
            ),
        )
        for saved, expected in cases:
            with self.subTest(saved=saved):
                box.session_file(self.path).unlink(missing_ok=True)
                if saved is not None:
                    box.atomic_json(box.session_file(self.path), saved)
                output = io.StringIO()
                with patch("sys.stdout", output), patch.object(box, "podman") as podman:
                    self.assertEqual(box.main(["list"]), 0)
                self.assertIn(self.data["id"], output.getvalue())
                self.assertIn(str(self.project), output.getvalue())
                lines = output.getvalue().splitlines()
                self.assertEqual(
                    re.split(r" {2,}", lines[0].strip()),
                    ["WORKSPACE ID", "DISTRO", "PROJECT", "STATUS", "SESSION ID"],
                )
                self.assertEqual(
                    re.split(r" {2,}", lines[1].strip()),
                    [
                        self.data["id"],
                        "tumbleweed",
                        str(self.project),
                        "present",
                        expected,
                    ],
                )
                self.assertNotIn("ses_host_archive", output.getvalue())
                podman.assert_not_called()

    def test_list_aligns_columns_for_different_path_lengths_and_missing_directories(
        self,
    ):
        other = self.root / "a much longer project directory"
        other.mkdir()
        other_path, other_data = box.create_workspace(other, "ubuntu")
        (other / box.MARKER).unlink()
        other.rmdir()
        box.atomic_json(
            box.session_file(other_path),
            {"last": "ses_latest", "sessions": ["ses_latest"]},
        )
        output = io.StringIO()
        with patch("sys.stdout", output), patch.object(box, "podman") as podman:
            self.assertEqual(box.main(["list"]), 0)
        lines = output.getvalue().splitlines()
        positions = [
            lines[0].index(header)
            for header in ("WORKSPACE ID", "DISTRO", "PROJECT", "STATUS", "SESSION ID")
        ]
        expected = {
            self.data["id"]: ("tumbleweed", str(self.project), "present", "none"),
            other_data["id"]: ("ubuntu", str(other), "missing directory", "ses_latest"),
        }
        self.assertEqual(len(lines), 3)
        for line in lines[1:]:
            ident = line[: positions[1]].strip()
            values = [ident, *expected[ident]]
            for position, value in zip(positions, values):
                self.assertEqual(line[position : position + len(value)], value)
            self.assertEqual(line, line.rstrip())
        podman.assert_not_called()

    def test_empty_list_still_shows_headers(self):
        output = io.StringIO()
        with (
            patch("sys.stdout", output),
            patch.object(box, "workspace_paths", return_value=[]),
            patch.object(box, "podman") as podman,
        ):
            self.assertEqual(box.main(["list"]), 0)
        self.assertEqual(
            re.split(r" {2,}", output.getvalue().strip()),
            ["WORKSPACE ID", "DISTRO", "PROJECT", "STATUS", "SESSION ID"],
        )
        podman.assert_not_called()

    def controller_fixture(self, controllers):
        root = self.root / "cgroup"
        service = (
            root / "user.slice/user-1000.slice/user@1000.service/cgroup.controllers"
        )
        service.parent.mkdir(parents=True)
        service.write_text(controllers + "\n")
        return root

    def test_suse_missing_delegation_fails_before_container_start(self):
        root = self.controller_fixture("pids")
        checker = box.check_resource_controllers
        with (
            patch.object(
                box,
                "check_resource_controllers",
                side_effect=lambda data: checker(data, cgroup_root=root, uid=1000),
            ),
            patch.object(box, "inspect_owned") as inspect,
        ):
            with self.assertRaisesRegex(
                box.BoxError, "missing delegated cgroup controllers"
            ):
                box.start_container(self.path, self.data)
        inspect.assert_not_called()

    def test_kernel_support_does_not_substitute_for_user_delegation(self):
        root = self.controller_fixture("pids")
        (root / "cgroup.controllers").write_text("cpu memory pids\n")
        with self.assertRaisesRegex(
            box.BoxError, "missing delegated cgroup controllers: cpu, memory"
        ) as raised:
            box.check_resource_controllers(self.data, cgroup_root=root, uid=1000)
        self.assertIn("90-oc-box-delegate.conf", str(raised.exception))
        self.assertIn("no reset/purge", str(raised.exception))

    def test_delegated_controllers_allow_existing_limits(self):
        root = self.controller_fixture("cpuset cpu io memory pids")
        box.check_resource_controllers(self.data, cgroup_root=root, uid=1000)

    def test_non_systemd_layout_uses_podman_effective_controllers(self):
        result = subprocess.CompletedProcess([], 0, '["cpu", "memory", "pids"]', "")
        with patch.object(box, "podman", return_value=result) as podman:
            box.check_resource_controllers(
                self.data, cgroup_root=self.root / "missing", uid=1000
            )
        self.assertIn("{{json .Host.CgroupControllers}}", podman.call_args.args)

    def test_copied_and_replaced_directory_identity(self):
        other = self.root / "other"
        other.mkdir()
        (other / box.MARKER).write_text(self.data["id"])
        with self.assertRaisesRegex(box.BoxError, "another directory"):
            box.find_workspace(other)
        self.project.rename(self.root / "old")
        self.project.mkdir()
        self.assertEqual(box.find_workspace(self.project), (None, None))
        (self.project / box.MARKER).write_text(self.data["id"])
        with self.assertRaisesRegex(box.BoxError, "replaced"):
            box.find_workspace(self.project)

    def test_config_preserves_mcp_lsp_formatter_and_plugins(self):
        config = Path(os.environ["XDG_CONFIG_HOME"]) / "opencode"
        config.mkdir(parents=True)
        original = {
            "mcp": {
                "search": {
                    "type": "local",
                    "command": ["/usr/bin/uvx", "duckduckgo-mcp-server"],
                }
            },
            "lsp": {"custom": {"command": ["server"], "extensions": [".py"]}},
            "formatter": {
                "custom": {"command": ["format", "$FILE"], "extensions": [".py"]}
            },
            "plugin": ["opencode-sandbox", "some-auth-plugin"],
            "provider": {
                "local": {"options": {"baseURL": "http://localhost:13305/api/v1"}}
            },
        }
        (config / "opencode.jsonc").write_text(json.dumps(original))
        (config / "plugins").mkdir()
        (config / "plugins/my-plugin.ts").write_text("export default async () => ({})")
        with tarfile.open(
            fileobj=io.BytesIO(box.config_archive(self.data)), mode="r:gz"
        ) as archive:
            copied = json.load(archive.extractfile("opencode.jsonc"))
            self.assertIn("plugins/my-plugin.ts", archive.getnames())
        for key in ("mcp", "lsp", "formatter", "plugin"):
            if key == "mcp":
                self.assertEqual(
                    copied[key]["search"]["command"], original[key]["search"]["command"]
                )
                self.assertEqual(copied[key]["search"]["timeout"], 120000)
            else:
                self.assertEqual(copied[key], original[key])
        self.assertEqual(
            copied["provider"]["local"]["options"]["baseURL"],
            "http://host.containers.internal:13305/api/v1",
        )
        self.assertTrue(copied["lsp"])
        self.assertEqual(json.loads((config / "opencode.jsonc").read_text()), original)

    def test_container_env_passes_mcp_credentials_not_host_sockets(self):
        with patch.dict(
            os.environ,
            {
                "BRAVE_API_KEY": "test-key",
                "OPENCODE_AUTH_CONTENT": '{"secret":"host-auth"}',
                "SSH_AUTH_SOCK": "/host/ssh",
                "DOCKER_HOST": "unix:///host/docker",
                "HTTP_PROXY": "http://localhost:3128",
            },
        ):
            env = box.container_env(self.data)
        self.assertEqual(env["BRAVE_API_KEY"], "test-key")
        self.assertEqual(env["OPENCODE_DISABLE_SANDBOX"], "1")
        self.assertEqual(
            json.loads(env["OPENCODE_CONFIG_CONTENT"]), box.container_build_config()
        )
        for key in (
            "SSH_AUTH_SOCK",
            "DOCKER_HOST",
            "HTTP_PROXY",
            "OPENCODE_AUTH_CONTENT",
        ):
            self.assertNotIn(key, env)

    def test_container_binds_only_project_and_private_named_home(self):
        calls = []

        def fake(*args, **kwargs):
            calls.append(args)
            if args[:2] == ("volume", "exists"):
                return subprocess.CompletedProcess([], 1, "", "")
            if args[:2] == ("volume", "inspect"):
                return subprocess.CompletedProcess(
                    [], 0, json.dumps([{"Labels": {box.LABEL: self.data["id"]}}]), ""
                )
            return subprocess.CompletedProcess([], 0, "", "")

        with patch.object(box, "podman", side_effect=fake):
            box.create_container(self.path, self.data)
        command = next(call for call in calls if call[0] == "create")
        volumes = [
            command[i + 1] for i, item in enumerate(command) if item == "--volume"
        ]
        self.assertEqual(len(volumes), 2)
        self.assertIn(
            f"{self.project}:{self.project}:rw,z,rprivate,nosuid,nodev", volumes
        )
        self.assertIn(f"{self.data['home_volume']}:{self.data['home']}:rw", volumes)
        self.assertNotIn("--privileged", command)
        self.assertNotIn(f"{Path.home()}:{Path.home()}:rw", volumes)

    def test_unreadable_project_is_reported_before_opencode_starts(self):
        self.data["ready"] = True
        info = {
            "State": {"Running": True},
            "Mounts": [
                {"Source": self.data["project"], "Destination": self.data["project"]}
            ],
        }
        denied = subprocess.CompletedProcess([], 1, "", "Permission denied")
        with (
            patch.object(box, "check_resource_controllers"),
            patch.object(box, "inspect_owned", return_value=info),
            patch.object(box, "podman", return_value=denied),
        ):
            with self.assertRaisesRegex(box.BoxError, "chcon -R -t container_file_t"):
                box.start_container(self.path, self.data)

    def test_plugin_dependency_preparation_runs_only_on_cold_sync(self):
        for reused in (False, True):
            with (
                self.subTest(reused=reused),
                patch.dict(
                    os.environ, {"XDG_DATA_HOME": str(self.root / "empty-data")}
                ),
            ):
                message = (
                    b"Reusing container plugin dependencies.\n"
                    if reused
                    else b"Plugin dependencies will be resolved by OpenCode if needed.\n"
                )
                result = subprocess.CompletedProcess([], 0, message, b"")
                with (
                    patch.object(box, "ensure_opencode_version"),
                    patch.object(box, "config_archive", return_value=b"archive"),
                    patch.object(box.subprocess, "run", return_value=result),
                    patch.object(box, "run") as run,
                ):
                    box.prepare_runtime(self.path, self.data, "1.19.5")
                if reused:
                    run.assert_not_called()
                else:
                    self.assertEqual(
                        run.call_args.args[0][-4:],
                        [box.CONTAINER_OPENCODE, "debug", "agent", "build"],
                    )
                    self.assertEqual(run.call_args.kwargs["timeout"], 180)

    def test_exec_runs_native_opencode_in_container(self):
        command = box.exec_command(
            self.data, ["opencode", self.data["project"], "--session", "ses_test"]
        )
        self.assertEqual(command[:3], ["podman", "exec", "-i"])
        self.assertEqual(
            command[-5:],
            [
                self.data["container"],
                "opencode",
                self.data["project"],
                "--session",
                "ses_test",
            ],
        )

    def test_forwarded_credentials_never_appear_in_process_arguments(self):
        with patch.dict(
            os.environ, {"CODEBERG_TOKEN": "synthetic-credential-for-argv-test"}
        ):
            command = box.exec_command(self.data, ["opencode", "--version"])
        self.assertIn("CODEBERG_TOKEN", command)
        self.assertNotIn("synthetic-credential-for-argv-test", " ".join(command))
        self.assertEqual(
            command.environment["CODEBERG_TOKEN"], "synthetic-credential-for-argv-test"
        )
        self.assertIn("HOME=" + self.data["home"], command)
        self.assertEqual(
            command.environment["LEMONADE_BASE_URL"],
            "http://host.containers.internal:13305/api/v1",
        )
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(box.subprocess, "run", return_value=completed) as execute:
            box.run(command)
        self.assertEqual(
            execute.call_args.kwargs["env"]["CODEBERG_TOKEN"],
            "synthetic-credential-for-argv-test",
        )

    def test_container_settings_do_not_change_host_podman_storage_environment(self):
        host = {
            "HOME": "/host/home",
            "XDG_DATA_HOME": "/host/podman-data",
            "XDG_CONFIG_HOME": "/host/config",
        }
        with patch.dict(os.environ, host):
            command = box.exec_command(self.data, ["opencode", "--version"])
        for key, value in host.items():
            self.assertEqual(command.environment[key], value)
        self.assertIn("XDG_DATA_HOME=" + self.data["home"] + "/.local/share", command)

    def test_tui_receives_forwarded_environment_without_secret_arguments(self):
        with patch.dict(os.environ, {"CODEBERG_TOKEN": "synthetic-relay-credential"}):
            command = box.exec_command(self.data, ["opencode"])
        with patch("terminal_ui.run_tui", return_value=0) as relay:
            self.assertEqual(box.run_tui(command), 0)
        self.assertEqual(
            relay.call_args.kwargs["env"]["CODEBERG_TOKEN"],
            "synthetic-relay-credential",
        )
        self.assertNotIn(
            "synthetic-relay-credential", " ".join(relay.call_args.args[0])
        )

    def test_installer_creates_working_standalone_launcher(self):
        home = self.root / "install-home"
        with patch.object(Path, "home", return_value=home):
            runpy.run_path(str(APP / "install.py"))
        binary = home / ".local/bin/oc-box"
        self.assertTrue(os.access(binary, os.X_OK))
        result = subprocess.run(
            [str(binary), "distros"], capture_output=True, text=True, timeout=5
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("registry.opensuse.org/opensuse/tumbleweed:latest", result.stdout)

    def test_sync_replaces_only_namespaced_archive_and_keeps_source_id(self):
        source = {"info": {"id": "ses_source", "title": "Work"}, "messages": []}
        old = box.archive_id(self.data["id"], "ses_source", "ses")
        state = {
            "last": "ses_source",
            "sessions": ["ses_source"],
            "host_sessions": {"ses_source": old},
            "synced": {"ses_source": "previous"},
        }
        calls = []

        def fake(command, **kwargs):
            calls.append(command)
            exporting = "python3" in command and "ses_source" in command
            return subprocess.CompletedProcess(
                command, 0, json.dumps(source) if exporting else "", ""
            )

        with (
            patch.object(box, "refresh_sessions", return_value=state),
            patch.object(box, "run", side_effect=fake),
        ):
            box.sync_sessions(self.path, self.data)
        deletion = next(command for command in calls if "delete" in command)
        self.assertEqual(deletion[-1], old)
        self.assertNotEqual(deletion[-1], "ses_source")
        self.assertEqual(box.session_state(self.path)["last"], "ses_source")

    def test_exact_host_opencode_is_reused_without_installing(self):
        current = subprocess.CompletedProcess([], 0, "1.19.5\n", "")
        with patch.object(box, "run", return_value=current) as run:
            box.ensure_opencode_version(self.data, "1.19.5")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(
            run.call_args.args[0][-2:], [box.CONTAINER_OPENCODE, "--version"]
        )

    def test_opencode_install_uses_exact_host_version_in_both_directions(self):
        for existing, expected in (("1.19.4", "1.19.5"), ("1.19.6", "1.19.5")):
            with self.subTest(existing=existing, expected=expected):
                results = [
                    subprocess.CompletedProcess([], 0, existing, ""),
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 0, expected, ""),
                ]
                with patch.object(box, "run", side_effect=results) as run:
                    box.ensure_opencode_version(self.data, expected)
                install = run.call_args_list[1].args[0]
                self.assertEqual(install[-1], "opencode-ai@" + expected)
                self.assertIn("--prefix", install)
                self.assertEqual(
                    run.call_args_list[2].args[0][-2:],
                    [box.CONTAINER_OPENCODE, "--version"],
                )

    def test_incorrect_install_version_prevents_launch(self):
        results = [
            subprocess.CompletedProcess([], 0, "1.19.4", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "1.19.6", ""),
        ]
        with (
            patch.object(box, "run", side_effect=results),
            patch.object(box, "config_archive") as config,
        ):
            with self.assertRaisesRegex(
                box.BoxError, "Refusing to launch a different version"
            ):
                box.prepare_runtime(self.path, self.data, "1.19.5")
        config.assert_not_called()

    def test_missing_opencode_is_installed_at_the_exact_host_version(self):
        results = [
            subprocess.CompletedProcess([], 127, "", "missing"),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "1.19.5", ""),
        ]
        with patch.object(box, "run", side_effect=results) as run:
            box.ensure_opencode_version(self.data, "1.19.5")
        self.assertEqual(run.call_args_list[1].args[0][-1], "opencode-ai@1.19.5")

    def test_host_sync_profile_keeps_default_data_location(self):
        env = box.host_cli_env(self.path)
        self.assertEqual(
            env["XDG_DATA_HOME"],
            os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")),
        )
        self.assertEqual(env["OPENCODE_PURE"], "1")
        self.assertEqual(env["OPENCODE_DISABLE_PROJECT_CONFIG"], "1")
        self.assertNotIn("agent", json.loads(env["OPENCODE_CONFIG_CONTENT"]))

    def test_scoped_config_enables_native_lsp_formatter_when_unspecified(self):
        config = Path(os.environ["XDG_CONFIG_HOME"]) / "opencode"
        config.mkdir(parents=True)
        (config / "opencode.json").write_text('{"mcp": {}, "shell": "/host/zsh"}')
        with tarfile.open(
            fileobj=io.BytesIO(box.config_archive(self.data)), mode="r:gz"
        ) as archive:
            copied = json.load(archive.extractfile("oc-box-profile.json"))
        self.assertIs(copied["lsp"], True)
        self.assertIs(copied["formatter"], True)
        self.assertEqual(copied["shell"], "/bin/bash")
        self.assertEqual(copied["agent"]["build"]["permission"], "allow")
        self.assertNotIn("permission", copied)

    def test_endpoint_rewrite_leaves_oauth_callbacks_and_shell_commands_intact(self):
        original = {
            "url": "http://localhost:9000/mcp",
            "oauth": {"redirectUri": "http://127.0.0.1:19876/callback"},
            "command": ["curl", "http://localhost:3000"],
        }
        changed = box.rewrite_config(original)
        self.assertEqual(changed["url"], "http://host.containers.internal:9000/mcp")
        self.assertEqual(changed["oauth"], original["oauth"])
        self.assertEqual(changed["command"], original["command"])

    def test_namespace_preserves_message_order_and_relationships(self):
        export = {
            "info": {"id": "ses_source", "title": "Work", "permission": ["ignored"]},
            "messages": [
                {
                    "info": {
                        "id": "msg_001",
                        "sessionID": "ses_source",
                        "role": "user",
                    },
                    "parts": [
                        {
                            "id": "prt_001",
                            "sessionID": "ses_source",
                            "messageID": "msg_001",
                            "type": "text",
                            "text": "ses_source",
                        }
                    ],
                },
                {
                    "info": {
                        "id": "msg_002",
                        "sessionID": "ses_source",
                        "parentID": "msg_001",
                        "role": "assistant",
                    },
                    "parts": [],
                },
            ],
        }
        archived = box.archive_session(export, self.data)
        self.assertNotEqual(archived["info"]["id"], "ses_source")
        self.assertNotIn("permission", archived["info"])
        self.assertLess(
            archived["messages"][0]["info"]["id"], archived["messages"][1]["info"]["id"]
        )
        self.assertEqual(
            archived["messages"][1]["info"]["parentID"],
            archived["messages"][0]["info"]["id"],
        )
        self.assertEqual(archived["messages"][0]["parts"][0]["text"], "ses_source")

    def test_reset_retains_home_but_purge_removes_it(self):
        calls = []

        def fake(*args, **kwargs):
            calls.append(args)
            if "--filter" in args:
                self.assertIn(f"label={box.LABEL}={self.data['id']}", args)
                return subprocess.CompletedProcess([], 0, "owned\n", "")
            if "inspect" in args:
                return subprocess.CompletedProcess(
                    [], 0, json.dumps([{"Labels": {box.LABEL: self.data["id"]}}]), ""
                )
            return subprocess.CompletedProcess([], 0, "", "")

        with patch.object(box, "podman", side_effect=fake):
            box.remove_resources(self.data, keep_home=True)
            self.assertFalse(any(call[:2] == ("volume", "rm") for call in calls))
            box.remove_resources(self.data)
        self.assertTrue(any(call[:2] == ("volume", "rm") for call in calls))
        self.assertFalse(any("prune" in call for call in calls))

    def test_sync_failure_keeps_container_data_and_stops(self):
        args = argparse.Namespace(
            command="start", new=False, model=None, import_session=None
        )
        with (
            patch.object(
                box,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "1.19.5\n", ""),
            ),
            patch.object(box, "start_container"),
            patch.object(box, "prepare_runtime"),
            patch.object(
                box, "refresh_sessions", return_value={"last": "ses_existing"}
            ),
            patch.object(box, "sync_sessions", side_effect=box.BoxError("offline")),
            patch.object(box, "stop_container") as stop,
            patch.object(box, "run_tui", return_value=0) as tui,
        ):
            self.assertEqual(box.launch(self.path, self.data, args), 1)
        self.assertEqual(tui.call_args.args[0][-2:], ["--session", "ses_existing"])
        self.assertIn(box.CONTAINER_OPENCODE, tui.call_args.args[0])
        stop.assert_called_once()
        self.assertTrue(self.path.exists())

    def test_purge_default_keeps_project_files(self):
        source = self.project / "code.py"
        source.write_text("work")
        args = argparse.Namespace(yes=True, delete_sessions=False, all=False)
        with (
            patch.object(box, "inspect_owned", return_value=None),
            patch.object(box, "stop_container"),
            patch.object(box, "remove_resources"),
        ):
            box.purge(self.path, self.data, args)
        self.assertEqual(source.read_text(), "work")
        self.assertFalse((self.project / box.MARKER).exists())


if __name__ == "__main__":
    unittest.main()
