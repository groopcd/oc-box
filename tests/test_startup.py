import base64
import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest import mock
from oc_box import lifecycle_output
from oc_box import oc_box as box, sync_config as sync, terminal_ui as ui

APP = Path(__file__).resolve().parents[1]


def archive(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as tar:
        for name, value in files.items():
            data = value.encode()
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o600
            tar.addfile(member, io.BytesIO(data))
    return output.getvalue()


class ConfigCacheTests(unittest.TestCase):
    def test_cold_sync_uses_runtime_sdk_and_drops_incompatible_host_lock(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            host = {
                "package.json": '{"dependencies":{"@opencode-ai/plugin":"1.15.11","custom":"2"}}',
                "package-lock.json": "old host resolution",
            }
            payload = archive(host)
            self.assertFalse(sync.sync_config(root, payload, "1.19.5"))
            config = root / ".config/opencode"
            effective = json.loads((config / "package.json").read_text())
            self.assertEqual(
                effective["dependencies"],
                {"@opencode-ai/plugin": "1.19.5", "custom": "2"},
            )
            self.assertFalse((config / "package-lock.json").exists())
            self.assertIn("1.15.11", host["package.json"])

    def test_cold_sync_keeps_lock_for_matching_sdk(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            host = {
                "package.json": '{"dependencies":{"@opencode-ai/plugin":"1.19.5"}}',
                "package-lock.json": "matching resolution",
            }
            sync.sync_config(root, archive(host), "1.19.5")
            self.assertEqual(
                (root / ".config/opencode/package-lock.json").read_text(),
                "matching resolution",
            )

    def test_unchanged_dependencies_survive_but_stale_plugins_are_removed(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = root / ".config/opencode"
            modules = config / "node_modules"
            modules.mkdir(parents=True)
            manifest = json.dumps({"dependencies": {"@opencode-ai/plugin": "1.19.5"}})
            (config / "package.json").write_text(manifest)
            (modules / "cached").write_text("already installed")
            (config / "stale-plugin.ts").write_text("do not load again")
            sync.sync_config(
                root,
                archive(
                    {"package.json": manifest, "plugins/current.ts": "current plugin"}
                ),
            )
            self.assertEqual((modules / "cached").read_text(), "already installed")
            self.assertFalse((config / "stale-plugin.ts").exists())
            self.assertTrue((config / "plugins/current.ts").is_file())

    def test_manifest_lock_and_runtime_changes_invalidate_cold_and_warm_caches(self):
        manifest = '{"dependencies":{"example":"^1"}}'
        sdk = '{"dependencies":{"@opencode-ai/plugin":"1.15.11"}}'
        cases = (
            (
                "cold manifest",
                {"package.json": '{"dependencies":{"old":"1"}}'},
                {"package.json": '{"dependencies":{"new":"2"}}'},
                None,
                None,
                False,
            ),
            (
                "cold lock",
                {"package.json": manifest, "package-lock.json": "old resolution"},
                {"package.json": manifest, "package-lock.json": "new resolution"},
                None,
                None,
                False,
            ),
            (
                "recorded lock",
                {"package.json": manifest, "package-lock.json": "old resolution"},
                {"package.json": manifest, "package-lock.json": "new resolution"},
                "1.19.5",
                "1.19.5",
                True,
            ),
            (
                "recorded runtime",
                {"package.json": sdk},
                {"package.json": sdk},
                "1.19.5",
                "1.19.6",
                True,
            ),
        )
        for label, before, after, runtime, replacement, recorded in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = root / ".config/opencode"
                if recorded:
                    sync.sync_config(root, archive(before), runtime)
                else:
                    config.mkdir(parents=True)
                    for name, content in before.items():
                        (config / name).write_text(content)
                modules = config / "node_modules"
                modules.mkdir()
                (modules / "cached").write_text("old")
                self.assertFalse(sync.sync_config(root, archive(after), replacement))
                self.assertFalse(modules.exists())

    def test_unsafe_archive_does_not_remove_existing_config(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = root / ".config/opencode"
            config.mkdir(parents=True)
            (config / "keep").write_text("original")
            with self.assertRaisesRegex(ValueError, "Unsafe"):
                sync.sync_config(root, archive({"../../escaped": "bad"}))
            self.assertEqual((config / "keep").read_text(), "original")

    def test_copied_config_directory_is_private(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            sync.sync_config(root, archive({"opencode.json": "{}"}))
            self.assertEqual((root / ".config/opencode").stat().st_mode & 0o777, 0o700)

    def test_warm_sync_preserves_runtime_generated_sdk_and_lock(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = root / ".config/opencode"
            host = {
                "package.json": '{"dependencies":{"@opencode-ai/plugin":"1.15.11"}}',
                "package-lock.json": '{"host":"old sdk lock"}',
            }
            self.assertFalse(sync.sync_config(root, archive(host), "1.19.5"))
            # Mainline upgrades the SDK, manifest, and lock after the first sync.
            runtime = '{"dependencies":{"@opencode-ai/plugin":"1.19.5"}}'
            lock = '{"container":"runtime sdk lock"}'
            (config / "package.json").write_text(runtime)
            (config / "package-lock.json").write_text(lock)
            (config / "node_modules").mkdir()
            (config / "node_modules/cached").write_text("already installed")
            self.assertTrue(sync.sync_config(root, archive(host), "1.19.5"))
            self.assertEqual((config / "package.json").read_text(), runtime)
            self.assertEqual((config / "package-lock.json").read_text(), lock)
            self.assertEqual(
                (config / "node_modules/cached").read_text(), "already installed"
            )
            # Warm sync must remain idempotent over many launches.
            self.assertTrue(sync.sync_config(root, archive(host), "1.19.5"))

    def test_sdk_only_existing_workspace_adopts_installed_runtime_cache(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = root / ".config/opencode"
            plugin = config / "node_modules/@opencode-ai/plugin"
            plugin.mkdir(parents=True)
            (plugin / "package.json").write_text('{"version":"1.19.5"}')
            (config / "package.json").write_text(
                '{"dependencies":{"@opencode-ai/plugin":"1.19.5"}}'
            )
            (config / "package-lock.json").write_text('{"container":"generated"}')
            host = {
                "package.json": '{"dependencies":{"@opencode-ai/plugin":"1.15.11"}}'
            }
            self.assertTrue(sync.sync_config(root, archive(host), "1.19.5"))
            self.assertTrue(plugin.is_dir())
            self.assertEqual(
                json.loads((config / "package.json").read_text())["dependencies"][
                    "@opencode-ai/plugin"
                ],
                "1.19.5",
            )

    def test_arbitrary_custom_dependencies_are_not_adopted_on_sdk_match(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = root / ".config/opencode"
            plugin = config / "node_modules/@opencode-ai/plugin"
            plugin.mkdir(parents=True)
            (plugin / "package.json").write_text('{"version":"1.19.5"}')
            (config / "package.json").write_text(
                '{"dependencies":{"@opencode-ai/plugin":"1.19.5","custom":"1"}}'
            )
            host = {
                "package.json": '{"dependencies":{"@opencode-ai/plugin":"1.15.11","custom":"2"}}'
            }
            self.assertFalse(sync.sync_config(root, archive(host), "1.19.5"))
        self.assertFalse(plugin.exists())


class StartupOutputTests(unittest.TestCase):
    def test_scoped_runtime_keeps_plugin_warning_and_marks_installer_calls(self):
        for config_message, install in (
            (b"Reusing container plugin dependencies.\n", False),
            (b"Plugin dependencies will be resolved by OpenCode if needed.\n", True),
            (b"new configuration warning\n", True),
        ):
            with (
                self.subTest(message=config_message),
                tempfile.TemporaryDirectory() as name,
            ):
                root = Path(name)
                out, errors = io.StringIO(), io.StringIO()

                def run(command, **_options):
                    if "--version" in command:
                        output = "2.2.6"
                    elif "auth" in command and "export" in command:
                        output = "[]"
                    elif "plugins" in command:
                        output = "oc-box: OpenCode 2.x plugin unavailable: example\n"
                    else:
                        output = ""
                    return subprocess.CompletedProcess(command, 0, output, "")

                with (
                    lifecycle_output.invocation("start", root),
                    contextlib.redirect_stdout(out),
                    contextlib.redirect_stderr(errors),
                    mock.patch.dict(os.environ, {"OPENCODE_AUTH_CONTENT": ""}),
                    mock.patch.object(lifecycle_output, "record"),
                    mock.patch.object(box, "config_archive", return_value=b"archive"),
                    mock.patch.object(box, "private_host_env", return_value={}),
                    mock.patch.object(
                        box,
                        "exec_command",
                        side_effect=lambda _data, command: box.ContainerCommand(
                            command, {}
                        ),
                    ),
                    mock.patch.object(
                        box, "host_command", side_effect=lambda _, *args: args
                    ),
                    mock.patch.object(box, "run", side_effect=run) as execute,
                    mock.patch.object(
                        box.subprocess,
                        "run",
                        return_value=subprocess.CompletedProcess(
                            [], 0, config_message, b""
                        ),
                    ),
                ):
                    box.prepare_runtime(
                        root / "workspace.json", {"home": "/root"}, "2.2.6"
                    )
                calls = [
                    call for call in execute.call_args_list if call.args[0][0] == "npm"
                ]
                self.assertEqual(len(calls), int(install))
                if install:
                    self.assertEqual(
                        calls[0].kwargs,
                        {"timeout": 600, "preparation": "plugin-dependencies"},
                    )
                self.assertEqual(out.getvalue(), "")
                self.assertEqual(
                    errors.getvalue(),
                    (
                        "oc-box: Configuration copy returned an unrecognized status; checking plugin dependencies.\n"
                        if config_message.startswith(b"new")
                        else ""
                    )
                    + "oc-box: OpenCode 2.x plugin unavailable: example\n",
                )

    def test_cold_bootstrap_changes_only_its_routine_completion_banner(self):
        for verbose in (False, True):
            with (
                self.subTest(verbose=verbose),
                tempfile.TemporaryDirectory() as name,
            ):
                root = Path(name)
                data = {
                    "project": str(root),
                    "container": "box-example",
                    "distro": "alpine",
                }
                path = root / "workspace.json"
                info = {
                    "State": {"Running": True},
                    "Mounts": [{"Source": str(root), "Destination": str(root)}],
                }
                with (
                    lifecycle_output.invocation("start", root, verbose=verbose),
                    mock.patch.object(lifecycle_output, "record"),
                    mock.patch.object(box, "validate_project", return_value=root),
                    mock.patch.object(box, "find_workspace", return_value=(path, data)),
                    mock.patch.object(
                        box.workspace_identity, "same_project", return_value=True
                    ),
                    mock.patch.object(box, "check_resource_controllers"),
                    mock.patch.object(box, "inspect_owned", return_value=info),
                    mock.patch.object(
                        box, "podman", return_value=subprocess.CompletedProcess([], 0)
                    ) as podman,
                ):
                    self.assertIs(box.start_container(path, data), info)
                installer, bootstrap, _ = podman.call_args_list
                self.assertEqual(
                    installer,
                    mock.call(
                        "exec",
                        data["container"],
                        "/bin/sh",
                        "-c",
                        box.DISTROS[data["distro"]][1],
                        timeout=1800,
                        preparation="baseline-packages",
                    ),
                )
                self.assertEqual(
                    bootstrap.args,
                    ("exec", "-i", data["container"], "python3", "-")
                    + (() if verbose else ("--quiet-ready",)),
                )
                self.assertEqual(
                    bootstrap.kwargs.keys(), {"input", "timeout", "preparation"}
                )
                self.assertEqual(bootstrap.kwargs["preparation"], "baseline-tools")
                self.assertEqual(bootstrap.kwargs["timeout"], 600)
                self.assertTrue(data["ready"])

    def test_routine_pre_client_banner_is_quiet_without_changing_native_output(self):
        out, errors = io.StringIO(), io.StringIO()

        def native(*_args, **_kwargs):
            self.assertEqual(out.getvalue(), "")
            self.assertEqual(errors.getvalue(), "")
            print("native stdout")
            print("native stderr", file=sys.stderr)
            return 7

        with (
            tempfile.TemporaryDirectory() as name,
            lifecycle_output.invocation("start"),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(errors),
            mock.patch.object(lifecycle_output, "record"),
            mock.patch.object(
                box, "refresh_sessions", return_value={"last": "ses_demo"}
            ),
            mock.patch.object(
                box, "exec_command", side_effect=lambda _d, command, **_: command
            ),
            mock.patch.object(box, "run_native_tui", side_effect=native) as client,
            mock.patch.object(box, "sync_sessions"),
        ):
            project = Path(name)
            data = {"distro": "alpine", "project": str(project)}
            args = box.parser().parse_args(["start"])
            self.assertEqual(
                box.launch_client(
                    project / "workspace.json", data, args, "1.19.5", None
                ),
                7,
            )
        self.assertIn("ses_demo", client.call_args.args[1])
        self.assertEqual(out.getvalue(), "native stdout\n")
        # History owns later console output; only the handoff above is in scope.
        self.assertTrue(errors.getvalue().startswith("native stderr\n"))


class StartupProgressTests(unittest.TestCase):
    class TTY(io.StringIO):
        def __init__(self, encoding="utf-8"):
            super().__init__()
            self._encoding = encoding
            self.updated = threading.Event()
            self.draws = 0

        @property
        def encoding(self):
            return self._encoding

        def isatty(self):
            return True

        def fileno(self):
            return 123

        def write(self, text):
            count = super().write(text)
            if "\r" in text and "elapsed" in text:
                self.draws += 1
                if self.draws >= 2:
                    self.updated.set()
            return count

    @contextlib.contextmanager
    def terminal(self, columns=80):
        with (
            mock.patch.dict(os.environ, {"TERM": "xterm", "COLUMNS": "999"}),
            mock.patch.object(
                box.os,
                "get_terminal_size",
                return_value=os.terminal_size((columns, 24)),
            ) as size,
        ):
            yield size

    @contextlib.contextmanager
    def workers(self):
        original = threading.Thread
        workers = []

        def create(**kwargs):
            worker = original(**kwargs)
            workers.append(worker)
            return worker

        with mock.patch.object(box.threading, "Thread", side_effect=create):
            yield workers
        self.assertTrue(all(not worker.is_alive() for worker in workers))

    def test_heartbeat_stops_after_stage_finishes(self):
        output = self.TTY()
        clock = [10.0]
        with (
            self.terminal(),
            self.workers() as workers,
            mock.patch.object(box.time, "monotonic", side_effect=lambda: clock[0]),
        ):
            with box.loading_stage("Starting test", interval=0.01, stream=output):
                clock[0] = 16.0
                self.assertTrue(output.updated.wait(timeout=1), output.getvalue())
                self.assertNotIn("\n", output.getvalue())
            snapshot = output.getvalue()
            self.assertEqual(len(workers), 1)
            self.assertFalse(workers[0].is_alive())
        self.assertIn("6s elapsed", snapshot)
        self.assertTrue(snapshot.endswith("Starting test done (6.0s)\n"), snapshot)
        self.assertEqual(snapshot.count("\n"), 1)
        self.assertEqual(snapshot, output.getvalue())

    def test_spinner_rotates_with_unicode_and_ascii_fallback(self):
        for encoding, frames in (
            ("utf-8", "⠋⠙⠹"),
            ("ascii", "|/-"),
            (None, "|/-"),
            ("unknown-encoding", "|/-"),
        ):
            with self.subTest(encoding=encoding):
                output = self.TTY(encoding)
                with (
                    self.terminal(),
                    mock.patch.object(box.threading, "Thread") as worker,
                    mock.patch.object(box.threading, "Event") as event,
                    mock.patch.object(box.time, "monotonic", return_value=0.0),
                ):
                    with box.loading_stage("Spinning", stream=output):
                        heartbeat = worker.call_args.kwargs["target"]
                        for _ in range(2):
                            event.return_value.wait.side_effect = [False, True]
                            heartbeat()
                for frame in frames:
                    self.assertIn(
                        f"\roc-box: {frame} Spinning",
                        output.getvalue().replace("\x1b[K", ""),
                    )
                event.return_value.wait.assert_called_with(0.1)
                self.assertEqual(output.getvalue().count("\n"), 1)
                self.assertTrue(
                    output.getvalue().endswith("oc-box: Spinning done (0.0s)\n")
                )
                self.assertNotIn("%", output.getvalue())

    def test_plain_output_has_start_and_final_lines_without_heartbeat(self):
        for kind in ("redirected", "dumb", "unknown width", "zero width"):
            with self.subTest(kind=kind):
                output = io.StringIO() if kind == "redirected" else self.TTY()
                with (
                    self.terminal() as size,
                    mock.patch.object(box.threading, "Thread") as worker,
                    mock.patch.object(box.time, "monotonic", side_effect=[10.0, 31.0]),
                    mock.patch.dict(
                        os.environ, {"TERM": "dumb" if kind == "dumb" else "xterm"}
                    ),
                ):
                    if kind == "unknown width":
                        size.side_effect = OSError("no terminal size")
                    elif kind == "zero width":
                        size.return_value = os.terminal_size((0, 0))
                    with box.loading_stage("Syncing session history", stream=output):
                        pass
                worker.assert_not_called()
                self.assertEqual(
                    output.getvalue(),
                    "oc-box: Syncing session history...\n"
                    "oc-box: Syncing session history done (21.0s)\n",
                )
                self.assertNotIn("\r", output.getvalue())
                self.assertNotIn("\x1b", output.getvalue())

    def test_stage_failure_is_not_reported_as_success(self):
        output = io.StringIO()
        failure = ValueError("failure")
        with (
            self.assertRaises(ValueError) as raised,
            box.loading_stage("Failing", stream=output),
        ):
            raise failure
        self.assertIs(raised.exception, failure)
        self.assertEqual(output.getvalue().count("\n"), 2)
        self.assertIn("failed", output.getvalue())
        self.assertNotIn("done", output.getvalue())

    def test_tty_failure_and_interrupt_preserve_identity_and_restore_streams(self):
        for failure in (ValueError("failure"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                output = self.TTY()
                with (
                    self.terminal(),
                    self.workers() as workers,
                    mock.patch.object(sys, "stderr", output),
                ):
                    with self.assertRaises(type(failure)) as raised:
                        with box.loading_stage("Failing"):
                            self.assertIsNot(sys.stderr, output)
                            raise failure
                    self.assertIs(sys.stderr, output)
                    self.assertEqual(len(workers), 1)
                    self.assertFalse(workers[0].is_alive())
                self.assertIs(raised.exception, failure)
                self.assertEqual(output.getvalue().count("\n"), 1)
                self.assertIn("Failing failed after ", output.getvalue())
                self.assertNotIn("done", output.getvalue())

    def test_tty_frames_use_stream_fd_and_fit_resized_terminal_cells(self):
        for columns in (2, 12, 24, 80):
            with self.subTest(columns=columns):
                output = self.TTY()
                with self.terminal() as size, self.workers():
                    with box.loading_stage(
                        "界e\u0301" * 50 + "\nunsafe", stream=output
                    ):
                        size.return_value = os.terminal_size((columns, 24))
                frames = output.getvalue().replace("\x1b[K", "").split("\r")[1:]
                self.assertEqual(len(frames), 2)
                for frame, limit in zip(frames, (79, columns - 1)):
                    cells = len(
                        frame.rstrip("\n").replace("界", "xx").replace("\u0301", "")
                    )
                    self.assertLessEqual(cells, limit, frame)
                self.assertIn("界", frames[0])
                self.assertIn("0s elapsed", frames[0])
                if columns >= 24:
                    self.assertIn("done (", frames[-1])
                self.assertEqual(output.getvalue().count("\n"), 1)
                self.assertTrue(output.getvalue().endswith("\n"))
                self.assertTrue(
                    all(call == mock.call(123) for call in size.call_args_list)
                )

    def test_python_output_is_separated_and_partial_lines_suspend_redraws(self):
        output = self.TTY()
        with (
            self.terminal(),
            mock.patch.object(sys, "stdout", output),
            mock.patch.object(sys, "stderr", output),
            mock.patch.object(box.threading, "Thread") as worker,
            mock.patch.object(box.threading, "Event") as event,
            mock.patch.object(box.time, "monotonic", return_value=0.0),
        ):
            with box.loading_stage("Syncing"):
                heartbeat = worker.call_args.kwargs["target"]
                sys.stderr.write("warning: retained ")
                snapshot = output.getvalue()
                event.return_value.wait.side_effect = [False, True]
                heartbeat()
                self.assertEqual(snapshot, output.getvalue())
                sys.stderr.writelines(["original", " history\n"])
                print("actual stdout output", flush=True)
                event.return_value.wait.side_effect = [False, True]
                heartbeat()
                self.assertGreater(output.draws, 1)
            self.assertIs(sys.stdout, output)
            self.assertIs(sys.stderr, output)
        self.assertIn("\r\x1b[Kwarning: retained original history\n", output.getvalue())
        self.assertIn("actual stdout output\n", output.getvalue())
        self.assertEqual(output.getvalue().count("\n"), 3)
        self.assertTrue(output.getvalue().endswith("Syncing done (0.0s)\n"))
        event.return_value.set.assert_called_once_with()
        worker.return_value.join.assert_called_once_with()

    def test_partial_python_output_is_terminated_before_final_status(self):
        output = self.TTY()
        with self.terminal(), mock.patch.object(sys, "stderr", output), self.workers():
            with box.loading_stage("Syncing"):
                sys.stderr.write("actual output without a newline")
        self.assertIn("actual output without a newline\n\r\x1b[K", output.getvalue())
        self.assertEqual(output.getvalue().count("\n"), 2)

    def test_nested_stages_restore_output_and_keep_both_results(self):
        output = self.TTY()
        with self.terminal(), mock.patch.object(sys, "stderr", output), self.workers():
            with box.loading_stage("Outer"):
                outer = sys.stderr
                with box.loading_stage("Inner"):
                    print("actual inner output", file=sys.stderr)
                self.assertIs(sys.stderr, outer)
            self.assertIs(sys.stderr, output)
        self.assertIn("actual inner output\n", output.getvalue())
        self.assertIn("Inner done (", output.getvalue())
        self.assertIn("Outer done (", output.getvalue())
        self.assertEqual(output.getvalue().count("\n"), 3)

    def test_progress_output_failure_does_not_replace_operation_failure(self):
        for interactive in (False, True):
            with self.subTest(interactive=interactive):
                output = self.TTY() if interactive else io.StringIO()
                failure = KeyboardInterrupt()
                with self.terminal(), self.workers() as workers:
                    with self.assertRaises(KeyboardInterrupt) as raised:
                        with box.loading_stage("Failing", stream=output):
                            output.close()
                            raise failure
                    self.assertTrue(all(not worker.is_alive() for worker in workers))
                self.assertIs(raised.exception, failure)

    def test_concise_tty_keeps_one_heartbeat_clears_and_keeps_warnings(self):
        output = self.TTY()
        with (
            self.terminal(),
            lifecycle_output.invocation("start"),
            mock.patch.object(lifecycle_output, "record"),
            mock.patch.object(sys, "stdout", output),
            mock.patch.object(sys, "stderr", output),
            mock.patch.object(box.threading, "Thread") as worker,
            mock.patch.object(box.threading, "Event") as event,
        ):
            with box.loading_stage("Preparing configuration", stage="configuration"):
                heartbeat = worker.call_args.kwargs["target"]

                def tick():
                    event.return_value.wait.side_effect = [False, True]
                    heartbeat()

                with box.loading_stage("Checking dependencies", stage="dependencies"):
                    tick()
                    self.assertEqual(
                        output.getvalue().count("Checking dependencies"), 2
                    )
                    print("plugin refused to load", file=sys.stderr)
                    with box._startup_tool_output():
                        snapshot = output.getvalue()
                        tick()
                        self.assertEqual(snapshot, output.getvalue())
                        # The mock child writes to the original (unproxied) stream.
                        output.write("native installer warning\n")
                tick()
                self.assertEqual(output.getvalue().count("Preparing configuration"), 2)
                self.assertIsNot(sys.stderr, output)
            self.assertIs(sys.stdout, output)
            self.assertIs(sys.stderr, output)
            worker.assert_called_once()
            worker.return_value.join.assert_called_once()
        rendered = output.getvalue()
        self.assertIn("\r\x1b[Kplugin refused to load\n", rendered)
        self.assertIn("native installer warning\n", rendered)
        self.assertTrue(rendered.endswith("\r\x1b[K"), rendered)
        self.assertEqual(rendered.count("\n"), 2)

    def test_concise_pipes_and_dumb_terminals_log_only_fixed_metadata(self):
        for stage, term, failure, outcome, level in (
            ("version", "xterm", None, "success", "debug"),
            ("version", "xterm", ValueError("private error"), "failure", "error"),
            ("history-wait", "xterm", ValueError("private error"), "failure", "debug"),
            ("version", "dumb", KeyboardInterrupt(), "cancelled", "debug"),
        ):
            output = self.TTY() if term == "dumb" else io.StringIO()
            with (
                self.subTest(stage=stage, outcome=outcome),
                mock.patch.dict(os.environ, {"TERM": term}),
                lifecycle_output.invocation("start"),
                mock.patch.object(lifecycle_output, "record") as record,
                mock.patch.object(box.time, "monotonic", side_effect=[10.0, 12.5]),
                mock.patch.object(box.threading, "Thread") as worker,
            ):
                if failure is None:
                    with box.loading_stage("private label", stage=stage, stream=output):
                        pass
                else:
                    with self.assertRaises(type(failure)) as caught:
                        with box.loading_stage(
                            "private label", stage=stage, stream=output
                        ):
                            raise failure
                    self.assertIs(caught.exception, failure)
                self.assertEqual(output.getvalue(), "")
                worker.assert_not_called()
                record.assert_called_once_with(
                    "lifecycle.stage",
                    level=level,
                    stage=stage,
                    reason="startup-failed" if level == "error" else None,
                    outcome=outcome,
                    duration_ms=2500,
                )

    def test_concise_tty_cancellation_clears_without_losing_partial_output(self):
        for warning in ("", "existing partial warning"):
            output = self.TTY()
            failure = KeyboardInterrupt()
            with (
                self.subTest(warning=warning),
                self.terminal(),
                self.workers() as workers,
                lifecycle_output.invocation("start"),
                mock.patch.object(lifecycle_output, "record"),
                mock.patch.object(sys, "stderr", output),
                self.assertRaises(KeyboardInterrupt) as caught,
            ):
                try:
                    with box.loading_stage(
                        "Preparing configuration", stage="configuration"
                    ):
                        if warning:
                            sys.stderr.write(warning)
                        raise failure
                finally:
                    self.assertIs(sys.stderr, output)
            self.assertIs(caught.exception, failure)
            self.assertEqual(len(workers), 1)
            self.assertTrue(output.getvalue().endswith("\r\x1b[K" + warning))
            self.assertNotIn("\n", output.getvalue())

    def test_untagged_and_verbose_stages_keep_diagnostic_console_lines(self):
        for verbose, stage in ((False, None), (True, "configuration")):
            output = io.StringIO()
            with (
                lifecycle_output.invocation("start", verbose=verbose),
                mock.patch.object(lifecycle_output, "record") as record,
                box.loading_stage(
                    "Preparing configuration", stage=stage, stream=output
                ),
            ):
                pass
            self.assertIn("Preparing configuration...\n", output.getvalue())
            self.assertIn("Preparing configuration done", output.getvalue())
            self.assertEqual(record.call_count, int(stage is not None))

    @unittest.skipUnless(sys.platform == "linux", "Linux PTY/termios integration")
    def test_loading_stage_pty_resize_has_one_final_newline(self):
        import fcntl
        import pty
        import select
        import struct
        import termios

        master, slave = pty.openpty()
        before = termios.tcgetattr(slave)

        def read_frame():
            self.assertTrue(select.select([master], [], [], 1)[0])
            return os.read(master, 65536)

        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 48, 0, 0))
            with (
                os.fdopen(os.dup(slave), "w", encoding="utf-8") as output,
                mock.patch.dict(os.environ, {"TERM": "xterm", "COLUMNS": "999"}),
            ):
                with box.loading_stage(
                    "Syncing " + "history " * 20, interval=60, stream=output
                ):
                    initial = read_frame()
                    self.assertNotIn(b"\n", initial)
                    fcntl.ioctl(
                        slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 32, 0, 0)
                    )
                final = read_frame()
            self.assertLessEqual(len(initial.decode().replace("\r\x1b[K", "")), 47)
            self.assertLessEqual(
                len(final.replace(b"\r\x1b[K", b"").rstrip(b"\r\n")), 31
            )
            self.assertIn(b"done (", final)
            self.assertEqual((initial + final).count(b"\n"), 1)
            self.assertTrue(final.endswith(b"\r\n"))
            self.assertEqual(termios.tcgetattr(slave), before)
        finally:
            os.close(master)
            os.close(slave)

    def test_terminal_setup_is_not_mistaken_for_rendered_ui(self):
        detector = ui.VisibleOutput()
        for chunk in (
            b"\x1b[?1049h\x1b[",
            b"2J\x1b[38;2;0;0;0m  \r\n",
            b"\x1b]0;OpenCode",
            b"\x07",
            b"\x1bP+q544e\x1b",
            b"\\",
        ):
            self.assertFalse(detector.feed(chunk))
        self.assertTrue(detector.feed("OpenCode ▀".encode()))

    def test_loading_indicator_never_interrupts_partial_terminal_sequences(self):
        detector = ui.VisibleOutput()
        detector.feed(b"\x1b]0;Title")
        self.assertFalse(detector.safe_to_draw())
        detector.feed(b"\x07\x1b[38;2;")
        self.assertFalse(detector.safe_to_draw())
        detector.feed(b"0;0;0m")
        self.assertTrue(detector.safe_to_draw())
        detector.feed(b"\xe2\x96")
        self.assertFalse(detector.safe_to_draw())
        self.assertTrue(detector.feed(b"\x80"))
        self.assertTrue(detector.safe_to_draw())

    @unittest.skipUnless(sys.platform == "linux", "Linux PTY/termios integration")
    def test_tui_progress_handoff_input_resize_and_terminal_restore(self):
        for mode in ("ascii", "kitty", "konsole", "dumb"):
            with self.subTest(mode=mode):
                self._check_tui_handoff(mode)

    def test_graphics_mark_requires_matching_direct_terminal_and_asset(self):
        supported = {
            "TERM": "xterm-kitty",
            "KITTY_WINDOW_ID": "42",
        }
        for overrides in (
            {"TERM": "xterm-256color"},
            {"KITTY_WINDOW_ID": "0"},
            *(
                {key: "present"}
                for key in (
                    "KONSOLE_VERSION",
                    "TMUX",
                    "STY",
                    "ZELLIJ",
                    "SSH_CONNECTION",
                    "SSH_CLIENT",
                    "SSH_TTY",
                    "XTERM_VERSION",
                    "VTE_VERSION",
                    "WEZTERM_PANE",
                    "ALACRITTY_WINDOW_ID",
                )
            ),
        ):
            with self.subTest(overrides=overrides):
                with mock.patch.object(ui.os, "ttyname") as ttyname:
                    self.assertIsNone(ui._graphics_mark(0, 1, supported | overrides))
                    ttyname.assert_not_called()
        with mock.patch.object(
            ui.os, "ttyname", side_effect=["/dev/pts/1", "/dev/pts/2"]
        ):
            self.assertIsNone(ui._graphics_mark(0, 1, supported))
        with (
            mock.patch.object(ui.os, "ttyname", return_value="/dev/pts/1"),
            mock.patch.object(ui.Path, "open", side_effect=FileNotFoundError),
        ):
            self.assertIsNone(ui._graphics_mark(0, 1, supported))
        for version in ("220399", "261303", "2608.03", "abc", "2608030"):
            self.assertIsNone(
                ui._graphics_profile(
                    {"TERM": "xterm-256color", "KONSOLE_VERSION": version}
                )
            )
        self.assertEqual(
            ui._graphics_profile(
                {"TERM": "xterm-256color", "KONSOLE_VERSION": "220400"}
            ),
            "konsole",
        )

    def _check_tui_handoff(self, mode):
        import fcntl
        import pty
        import select
        import signal
        import struct
        import termios

        master, slave = pty.openpty()
        before = termios.tcgetattr(slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
        fixture = (
            "import os,sys,time,tty; assert os.environ['OC_BOX_RELAY_CHECK']=='present'; tty.setraw(0); "
            "os.write(1,b'\\x1b[?1049h\\x1b[2J\\x1b]0;Boot'); "
            "time.sleep(0.2); os.write(1,b'ing\\x07UI_READY'); "
            "key=os.read(0,1); size=os.get_terminal_size(0); "
            "os.write(1,b'KEY='+key+(' SIZE=%dx%d'%(size.columns,size.lines)).encode()); "
            "time.sleep(0.15); os.write(1,b'\\x1b[?1049l')"
        )
        relay = "import os,sys; sys.path.insert(0,sys.argv[1]+'/src'); from oc_box.terminal_ui import run_tui; sys.exit(run_tui([sys.executable,'-c',sys.argv[2]],env={**os.environ,'OC_BOX_RELAY_CHECK':'present'},refresh=0.03,startup_logo=sys.argv[3]))"
        style = "ascii" if mode == "ascii" else "graphics"
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", relay, str(APP), fixture, style],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env={
                **{
                    key: value
                    for key, value in os.environ.items()
                    if key not in ui._GRAPHICS_BLOCKERS
                },
                "TERM": {
                    "ascii": "xterm-kitty",
                    "kitty": "xterm-kitty",
                    "konsole": "xterm-256color",
                    "dumb": "dumb",
                }[mode],
                "KITTY_WINDOW_ID": "42" if mode != "konsole" else "",
                "KONSOLE_VERSION": "260803" if mode == "konsole" else "",
            },
        )
        output = bytearray()
        sent = False
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    output.extend(os.read(master, 65536))
                if b"UI_READY" in output and not sent:
                    fcntl.ioctl(
                        slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 120, 0, 0)
                    )
                    os.kill(process.pid, signal.SIGWINCH)
                    time.sleep(0.05)
                    os.write(master, b"z")
                    sent = True
                if process.poll() is not None:
                    while select.select([master], [], [], 0)[0]:
                        output.extend(os.read(master, 65536))
                    break
            self.assertIsNotNone(process.poll(), bytes(output))
            self.assertEqual(process.returncode, 0, bytes(output))
            self.assertIn(b"Booting\x07UI_READY", output)
            if mode == "dumb":
                self.assertNotIn(b"Loading OpenCode", output)
            else:
                self.assertIn(b"Loading OpenCode, plugins and MCPs", output)
                clear = b"\x1b7\x1b[24;1H\x1b[0m\x1b[2K\x1b8"
                self.assertLess(output.index(clear), output.index(b"\x1b[?1049h"))
            if mode in ("kitty", "konsole"):
                upload = b"I=" if mode == "kitty" else b"i=0,p="
                delete = b"d=N,I=" if mode == "kitty" else b"d=i,i=0,p="
                match = re.search(
                    rb"\x1b_Ga=T,t=d,f=100,"
                    + upload
                    + rb"([1-9][0-9]*),q=2,c=2,r=1,C=1;([A-Za-z0-9+/=]+)\x1b\\",
                    output,
                )
                self.assertIsNotNone(match, bytes(output))
                self.assertEqual(
                    base64.b64decode(match[2], validate=True),
                    Path(ui.__file__).with_name("oc-box-mark.png").read_bytes(),
                )
                deletion = b"\x1b_Ga=d," + delete + match[1] + b",q=2;\x1b\\"
                self.assertEqual(output.count(b"\x1b_G"), 2)
                self.assertLess(match.end(), output.index(deletion))
                self.assertLess(output.index(deletion), output.index(b"\x1b[?1049h"))
            else:
                self.assertNotIn(b"\x1b_G", output)
            self.assertIn(b"KEY=z SIZE=120x32", output)
            self.assertNotIn(b"oc-box:", bytes(output).split(b"UI_READY", 1)[1])
            self.assertEqual(termios.tcgetattr(slave), before)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            os.close(master)
            os.close(slave)


if __name__ == "__main__":
    unittest.main()
