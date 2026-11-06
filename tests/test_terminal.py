import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import select
import stat
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("oc_box_terminal", APP / "oc_box.py")
assert spec is not None and spec.loader is not None
box = importlib.util.module_from_spec(spec)
spec.loader.exec_module(box)


class MaintenanceTerminalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-terminal-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(
            os.environ, {"OC_BOX_STATE_HOME": str(self.root / "registry")}
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.project = self.root / "project with spaces"
        self.project.mkdir()
        self.path, self.data = box.create_workspace(self.project, "tumbleweed")
        self.data["home"] = str(self.root / "private-home")
        Path(self.data["home"]).mkdir()
        box.atomic_json(self.path, self.data)
        box.atomic_json(
            box.session_file(self.path), {"last": "ses_keep", "sessions": ["ses_keep"]}
        )
        self.info = {
            "State": {"Running": False},
            "Mounts": [{"Source": str(self.project), "Destination": str(self.project)}],
        }

    def call_main(self, args, *, relay_code=0):
        with (
            contextlib.redirect_stderr(io.StringIO()),
            contextlib.redirect_stdout(io.StringIO()),
            patch.object(box.sys.stdin, "isatty", return_value=True),
            patch.object(box.sys.stdout, "isatty", return_value=True),
            patch.object(box, "check_backend") as backend,
            patch.object(box, "inspect_owned", return_value=self.info),
            patch.object(box, "podman") as podman,
            patch.object(box, "stop_container") as stop,
            patch("terminal_ui.run_tui", return_value=relay_code) as relay,
            patch.object(box, "prepare_runtime") as prepare,
            patch.object(box, "start_container") as start,
            patch.object(box, "sync_sessions") as sync,
            patch.object(box, "create_container") as create,
        ):
            code = box.main(args)
        return code, backend, podman, stop, relay, prepare, start, sync, create

    def test_existing_stopped_workspace_opens_shell_and_returns_to_stopped(self):
        before = {
            path: path.read_bytes()
            for path in (
                self.path,
                box.session_file(self.path),
                self.project / box.MARKER,
            )
        }
        source = self.project / "unfinished.txt"
        source.write_text("unfinished edits")
        result = self.call_main(
            ["terminal", "--project", str(self.project)], relay_code=7
        )
        code, backend, podman, stop, relay, prepare, start, sync, create = result
        self.assertEqual(code, 7)
        self.assertEqual(podman.call_args.args, ("start", self.data["container"]))
        self.assertEqual(podman.call_args.kwargs["env"], box.maintenance_host_env())
        stop.assert_called_once_with(self.data, env=box.maintenance_host_env())
        self.assertEqual(backend.call_args.kwargs["env"], box.maintenance_host_env())
        command = relay.call_args.args[0]
        self.assertEqual(command[:4], ["podman", "exec", "-i", "-t"])
        self.assertIn(self.data["container"], command)
        self.assertIn("--noprofile --norc -i", command[-1])
        self.assertFalse(relay.call_args.kwargs["show_loading"])
        for operation in (prepare, start, sync, create):
            operation.assert_not_called()
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(source.read_text(), "unfinished edits")

    def test_existing_running_container_is_not_stopped_on_exit(self):
        self.info["State"]["Running"] = True
        result = self.call_main(["terminal", "--workspace", self.data["id"]])
        self.assertEqual(result[0], 0)
        result[2].assert_not_called()
        result[3].assert_not_called()
        result[4].assert_called_once()

    def test_no_tokens_custom_secrets_or_shell_hooks_in_host_or_shell_environment(self):
        settings = self.root / "host-config/oc-box/config.json"
        box.atomic_json(settings, {"pass_env": ["CUSTOM_SECRET"]})
        hostile = {
            "PERSONAL_GITHUB_TOKEN": "synthetic-token",
            "GITHUB_TOKEN": "synthetic-copilot",
            "GH_TOKEN": "synthetic-gh",
            "OPENAI_API_KEY": "synthetic-api-key",
            "CUSTOM_SECRET": "synthetic-custom",
            "BASH_ENV": "/bad/bash-env",
            "ENV": "/bad/env",
            "PROMPT_COMMAND": "bad hook",
            "LD_PRELOAD": "/bad/loader",
            "PYTHONPATH": "/bad/python",
            "NODE_OPTIONS": "bad options",
            "SSH_AUTH_SOCK": "/bad/ssh",
            "HTTP_PROXY": "http://secret.invalid",
            "OPENCODE_AUTH_CONTENT": '{"key":"synthetic-auth"}',
            "XDG_CONFIG_HOME": str(settings.parents[1]),
        }
        with (
            patch.dict(os.environ, hostile),
            patch.object(
                box,
                "container_env",
                side_effect=AssertionError("Must not read pass_env or OpenCode auth"),
            ),
        ):
            command = box.maintenance_command(self.data)
        for key in hostile:
            if key != "XDG_CONFIG_HOME":
                self.assertNotIn(key, command.environment)
                self.assertFalse(
                    any(argument.startswith(key + "=") for argument in command)
                )
        for value in hostile.values():
            if not value.startswith(str(self.root)):
                self.assertNotIn(value, " ".join(command))
        self.assertIn("/usr/bin/env", command)
        env_index = command.index("/usr/bin/env")
        self.assertEqual(command[env_index + 1], "-i")
        self.assertEqual(
            command.environment["XDG_CONFIG_HOME"], str(settings.parents[1])
        )
        self.assertIn("HISTFILE=/dev/null", command)
        self.assertIn("HISTSIZE=0", command)
        self.assertNotIn("--privileged", command)
        self.assertNotIn("--env-host", command)
        self.assertNotIn("--volume", command)

    def test_host_podman_storage_environment_remains_correct(self):
        host = {
            "HOME": "/host-home",
            "XDG_DATA_HOME": "/host-storage",
            "XDG_CONFIG_HOME": "/host-settings",
            "XDG_RUNTIME_DIR": "/run/user/1000",
        }
        with patch.dict(os.environ, host):
            command = box.maintenance_command(self.data)
        for key, value in host.items():
            self.assertEqual(command.environment[key], value)
        self.assertIn("HOME=" + self.data["home"], command)
        self.assertEqual(command[command.index("--workdir") + 1], self.data["project"])

    def test_invalid_terminal_name_is_replaced_without_printing_it(self):
        with patch.dict(os.environ, {"TERM": "xterm\nmalicious"}):
            command = box.maintenance_command(self.data)
        self.assertIn("TERM=xterm-256color", command)
        self.assertNotIn("malicious", " ".join(command))

    def test_shell_failure_or_interrupt_stops_container_started_by_terminal(self):
        for error in (OSError("shell missing"), KeyboardInterrupt()):
            with (
                self.subTest(error=type(error).__name__),
                patch.object(box, "inspect_owned", return_value=self.info),
                patch.object(box, "podman"),
                patch.object(box, "stop_container") as stop,
                patch("terminal_ui.run_tui", side_effect=error),
                contextlib.redirect_stderr(io.StringIO()),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                with self.assertRaises(type(error)):
                    box.maintenance_terminal(self.path, self.data)
                stop.assert_called_once()

    def test_missing_or_unrecognized_container_never_launches_or_recreates(self):
        for info in (
            None,
            {"State": {"Running": True}, "Mounts": []},
            {"State": {"Running": True, "Paused": True}, "Mounts": self.info["Mounts"]},
        ):
            with (
                self.subTest(info=info),
                patch.object(box, "inspect_owned", return_value=info),
                patch.object(box, "podman") as podman,
                patch.object(box, "create_container") as create,
                patch("terminal_ui.run_tui") as relay,
            ):
                with self.assertRaises(box.BoxError):
                    box.maintenance_terminal(self.path, self.data)
                podman.assert_not_called()
                create.assert_not_called()
                relay.assert_not_called()

    def test_no_workspace_creates_nothing(self):
        fresh = self.root / "fresh"
        fresh.mkdir()
        with (
            patch.object(box.sys.stdin, "isatty", return_value=True),
            patch.object(box.sys.stdout, "isatty", return_value=True),
            patch.object(box, "check_backend"),
            patch.object(box, "create_workspace") as create,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaisesRegex(box.BoxError, "No oc-box workspace"):
                box.main(["terminal", "--project", str(fresh)])
        create.assert_not_called()
        self.assertFalse((fresh / box.MARKER).exists())

    def test_noninteractive_or_mutating_options_rejected_before_backend(self):
        for option in (
            [],
            ["--new"],
            ["--resume"],
            ["--model=provider/model"],
            ["--cpus=8"],
            ["--memory=2g"],
            ["--distro=ubuntu"],
            ["--device=/dev/null"],
            ["--podman-arg=--privileged"],
            ["--yes"],
            ["--all"],
            ["--delete-sessions"],
            ["--json"],
            ["--import-session=ses_source"],
        ):
            with (
                self.subTest(option=option),
                patch.object(box, "check_backend") as backend,
                patch.object(box.sys.stdin, "isatty", return_value=False),
                patch.object(box.sys.stdout, "isatty", return_value=False),
            ):
                with self.assertRaises(box.BoxError):
                    box.main(["terminal", "--project", str(self.project), *option])
                backend.assert_not_called()

    def test_active_launcher_blocks_maintenance_and_lifecycle_commands(self):
        with (
            box.workspace_lock(self.path),
            patch.object(box.sys.stdin, "isatty", return_value=True),
            patch.object(box.sys.stdout, "isatty", return_value=True),
            patch.object(box, "check_backend"),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaisesRegex(box.BoxError, "active oc-box process"):
                box.main(["terminal", "--workspace", self.data["id"]])

        def verify_lock(*args, **kwargs):
            with self.assertRaisesRegex(box.BoxError, "active oc-box process"):
                with box.workspace_lock(self.path):
                    self.fail("Maintenance must hold the workspace lock")
            return 0

        with (
            patch.object(box.sys.stdin, "isatty", return_value=True),
            patch.object(box.sys.stdout, "isatty", return_value=True),
            patch.object(box, "check_backend"),
            patch.object(box, "maintenance_terminal", side_effect=verify_lock),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(box.main(["terminal", "--workspace", self.data["id"]]), 0)

    def test_changed_identity_or_other_marker_is_not_a_maintenance_bypass(self):
        original = dict(self.data)
        self.data["project_identity"] = [0, 0]
        box.atomic_json(self.path, self.data)
        with patch.object(box, "inspect_owned") as inspect:
            with self.assertRaisesRegex(box.BoxError, "identity changed"):
                box.maintenance_terminal(self.path, self.data)
            inspect.assert_not_called()
        box.atomic_json(self.path, original)
        (self.project / box.MARKER).unlink()
        box.create_workspace(self.project, "ubuntu")
        with patch.object(box, "inspect_owned") as inspect:
            with self.assertRaisesRegex(box.BoxError, "refusing maintenance"):
                box.maintenance_terminal(self.path, original)
            inspect.assert_not_called()

    def test_backend_check_uses_clean_environment(self):
        clean = box.maintenance_host_env()
        with (
            patch.object(box.dependencies, "require"),
            patch.object(box.os, "geteuid", return_value=1000),
            patch.object(
                box,
                "podman",
                return_value=subprocess.CompletedProcess([], 0, "true\n", ""),
            ) as podman,
        ):
            box.check_backend(env=clean)
        self.assertEqual(podman.call_args.kwargs["env"], clean)

    @unittest.skipUnless(
        sys.platform == "linux", "Linux PTY/interactive shell integration"
    )
    def test_real_maintenance_shell_has_no_inherited_secrets_or_startup_hooks(self):
        """Use a fake transport only; execute the actual sanitized shell in a PTY."""
        import pty

        home = Path(self.data["home"])
        marker = self.root / "startup-hook-ran"
        for name in (".bashrc", ".bash_profile", ".profile"):
            (home / name).write_text(f"touch {marker}\n")
        hist = home / ".bash_history"
        hist.write_text("previous private command\n")
        probe = self.root / "probe.py"
        result = self.root / "environment.json"
        probe.write_text(
            "import json,os,sys; open(sys.argv[1],'w').write(json.dumps(dict(os.environ))); print('PROBE_DONE',flush=True)\n"
        )
        binary = self.root / "bin"
        binary.mkdir()
        fake = binary / "podman"
        fake.write_text(
            "#!"
            + sys.executable
            + "\nimport os,sys\na=sys.argv[1:]; i=a.index('/usr/bin/env'); os.execvpe(a[i],a[i:],{**os.environ,'CONTAINER_TOKEN':'should-not-survive','BASH_ENV':"
            + repr(str(home / ".bashrc"))
            + "})\n"
        )
        fake.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        master, slave = pty.openpty()
        before = termios.tcgetattr(slave)
        env = {
            **os.environ,
            "PATH": str(binary) + ":" + os.environ["PATH"],
            "HOST_TOKEN": "must-not-survive",
            "BASH_ENV": str(home / ".bashrc"),
            "ENV": str(home / ".profile"),
            "PROMPT_COMMAND": "touch " + str(marker),
        }
        wrapper = "import json,sys; sys.path.insert(0,sys.argv[1]); import oc_box; from terminal_ui import run_tui; c=oc_box.maintenance_command(json.loads(sys.argv[2])); sys.exit(run_tui(c,env=c.environment,show_loading=False,refresh=0.03))"
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", wrapper, str(APP), json.dumps(self.data)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
        )
        output = bytearray()
        sent = False
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    output.extend(os.read(master, 65536))
                if b"[oc-box " in output and not sent:
                    os.write(
                        master, f"{sys.executable} {probe} {result}\nexit\n".encode()
                    )
                    sent = True
                if process.poll() is not None:
                    break
            self.assertIsNotNone(process.poll(), bytes(output))
            self.assertEqual(process.returncode, 0, bytes(output))
            self.assertIn(b"PROBE_DONE", output)
            self.assertNotIn(b"Loading OpenCode", output)
            actual = json.loads(result.read_text())
            self.assertNotIn("HOST_TOKEN", actual)
            self.assertNotIn("CONTAINER_TOKEN", actual)
            self.assertNotIn("BASH_ENV", actual)
            self.assertNotIn("ENV", actual)
            self.assertNotIn("PROMPT_COMMAND", actual)
            self.assertFalse(marker.exists())
            self.assertEqual(hist.read_text(), "previous private command\n")
            self.assertEqual(termios.tcgetattr(slave), before)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            os.close(master)
            os.close(slave)


if __name__ == "__main__":
    unittest.main()
