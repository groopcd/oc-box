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
from oc_box import (
    file_preview,
    history_recovery,
    host_drop,
    lifecycle_output,
    startup_output,
    workspace_preferences,
)
from oc_box import opencode_v2_io as v2
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
    def test_sdk_remediation_scopes_affected_ranges_to_the_selected_v2_sdk(self):
        for release in ("2.0.0", "2.4.2", "2.58.9"):
            with self.subTest(release=release):
                self.assertEqual(
                    sync.sdk_security_overrides(release),
                    {
                        f"@opencode/plugin@{release}": {
                            "glob@>=9.0.0 <14.0.2": "14.0.2",
                            "minimatch@>=7.0.0 <11.0.1": "11.0.1",
                        }
                    },
                )
        self.assertEqual(sync.sdk_security_overrides("1.19.5"), {})

    def test_sdk_security_fixes_invalidate_old_cache_without_rewriting_host(self):
        manifest = {
            "dependencies": {"@opencode/plugin": "2.4.0", "custom": "1"},
            "overrides": {"custom": {"glob": "10.5.0"}},
        }
        payload = archive(
            {"package.json": json.dumps(manifest), "package-lock.json": "{}"}
        )
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            # Simulate the prior launcher preparing exactly the same host input.
            with mock.patch.object(sync, "sdk_security_overrides", return_value={}):
                sync.sync_config(root, payload, "2.4.0")
            config = root / ".config/opencode"
            modules = config / "node_modules"
            modules.mkdir()
            self.assertFalse(sync.sync_config(root, payload, "2.4.0"))
            self.assertFalse(modules.exists())
            self.assertFalse((config / "package-lock.json").exists())
            installed = json.loads((config / "package.json").read_text())
            self.assertEqual(installed["dependencies"], manifest["dependencies"])
            self.assertEqual(
                installed["overrides"],
                {"custom": {"glob": "10.5.0"}, **sync.sdk_security_overrides("2.4.0")},
            )
            modules.mkdir()
            (modules / "cached").touch()
            lock = json.dumps(
                {
                    "lockfileVersion": 3,
                    "packages": {"node_modules/@opencode/plugin": {"version": "2.4.0"}},
                }
            )
            (config / "package-lock.json").write_text(lock)
            (modules / ".package-lock.json").write_text(lock)
            sdk = modules / "@opencode/plugin"
            sdk.mkdir(parents=True)
            (sdk / "package.json").write_text('{"version":"2.4.0"}')
            self.assertTrue(sync.mark_sdk_installed(root, "2.4.0"))
            self.assertTrue(sync.sync_config(root, payload, "2.4.0"))
            self.assertTrue((modules / "cached").exists())
            (sdk / "package.json").write_text('{"version":"2.3.0"}')
            self.assertFalse(sync.sync_config(root, payload, "2.4.0"))
            self.assertFalse((modules / "cached").exists())

    def test_sdk_lock_refuses_nested_and_aliased_affected_packages(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "package-lock.json"
            for entry, version, safe in (
                ("node_modules/rimraf/node_modules/glob", "10.5.0", False),
                ("node_modules/glob", "14.0.2", True),
                ("node_modules/renamed", "10.5.0", False),
                ("node_modules/renamed", "11.0.1", True),
            ):
                with self.subTest(entry=entry, version=version):
                    path.write_text(
                        json.dumps(
                            {
                                "lockfileVersion": 3,
                                "packages": {
                                    "node_modules/@opencode/plugin": {
                                        "version": "2.4.0"
                                    },
                                    entry: {
                                        "version": version,
                                        **(
                                            {"name": "minimatch"}
                                            if entry.endswith("renamed")
                                            else {}
                                        ),
                                    },
                                },
                            }
                        )
                    )
                    self.assertIs(sync.sdk_lock_safe(path, "2.4.0"), safe)

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


class V2PluginReadinessTests(unittest.TestCase):
    def test_empty_early_catalog_can_settle_but_never_counts_as_ready(self):
        active = {"id": "oc-box.build-permissions", "state": {"status": "active"}}
        request = mock.Mock(side_effect=[{}, {"data": []}, {"data": [active]}])
        with mock.patch.object(v2.time, "sleep") as pause:
            self.assertEqual(v2.ready_plugins(request, "/demo project"), [active])
        self.assertEqual(request.call_count, 3)
        self.assertEqual(
            request.call_args.args[1], "/api/plugin?directory=%2Fdemo%20project"
        )
        self.assertEqual(
            request.call_args_list[0].kwargs["deadline"],
            request.call_args.kwargs["deadline"],
        )
        pause.assert_called_once()

    def test_missing_failed_malformed_or_ambiguous_build_plugin_refuses(self):
        active = {"id": "oc-box.build-permissions", "state": {"status": "active"}}
        failed = {"id": active["id"], "state": {"status": "failed", "error": "secret"}}
        for value, expected, clock in (
            ({"data": []}, v2.BUILD_PLUGIN_TIMEOUT, [0, 0, 60, 60]),
            ({"data": [failed]}, v2.BUILD_PLUGIN_FAILED, [0, 0]),
            ({"data": [{}]}, v2.READINESS, [0, 0]),
            ({"data": [active, active]}, v2.READINESS, [0, 0]),
        ):
            request = mock.Mock(side_effect=[{}, value])
            with (
                self.subTest(expected=expected),
                mock.patch.object(v2.time, "monotonic", side_effect=clock),
                mock.patch.object(v2.time, "sleep"),
                self.assertRaises(v2.FixedError) as raised,
            ):
                v2.ready_plugins(request, "/project")
            self.assertEqual(str(raised.exception), expected)
            self.assertEqual(request.call_count, 2)


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
                self.assertEqual(len(calls), 2 * int(install))
                if install:
                    self.assertIn("--package-lock-only", calls[0].args[0])
                    self.assertIn("--ignore-scripts", calls[0].args[0])
                    self.assertIn("ci", calls[1].args[0])
                    self.assertIn("--install-strategy=shallow", calls[0].args[0])
                    self.assertIn("--install-strategy=shallow", calls[1].args[0])
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

    def test_interruption_before_stage_entry_erases_badge_and_stops_worker(self):
        image = "\x1b_Ga=T,q=2;TEST\x1b\\"
        erase = "\x1b_Ga=d,q=2;TEST\x1b\\"
        thread_factory = threading.Thread
        for fault in ("first-flush", "before-thread-start", "after-thread-start"):
            output = self.TTY()
            spawned = []
            failure = KeyboardInterrupt(fault)

            def create(**kwargs):
                thread = thread_factory(**kwargs)
                spawned.append(thread)
                start = thread.start

                def interrupt_start():
                    if fault == "after-thread-start":
                        start()
                    raise failure

                thread.start = interrupt_start
                return thread

            with (
                self.subTest(fault=fault),
                self.terminal(),
                mock.patch.object(sys, "stderr", output),
                mock.patch.object(
                    startup_output,
                    "badge_for",
                    return_value=startup_output.Badge(image, 2, erase),
                ),
                mock.patch.object(box.threading, "Thread", side_effect=create),
                contextlib.ExitStack() as patches,
            ):
                if fault == "first-flush":
                    patches.enter_context(
                        mock.patch.object(output, "flush", side_effect=[failure, None])
                    )
                with self.assertRaises(KeyboardInterrupt) as caught:
                    with box.loading_stage("Interrupted setup", interval=60):
                        self.fail("An interrupted stage must never run its body")
                self.assertIs(caught.exception, failure)
                self.assertIs(sys.stderr, output)
                self.assertEqual(box._TTY_PROGRESS.get(), ())
            self.assertEqual(len(spawned), int(fault != "first-flush"))
            self.assertTrue(all(not thread.is_alive() for thread in spawned))
            self.assertIn(image, output.getvalue())
            self.assertTrue(output.getvalue().endswith(erase + "\r\x1b[K"))
            self.assertNotIn("\n", output.getvalue())

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
                self.assertEqual(output.getvalue().count("Preparing configuration"), 1)
                self.assertIsNot(sys.stderr, output)
            self.assertIs(sys.stdout, output)
            self.assertIs(sys.stderr, output)
            worker.assert_called_once()
            worker.return_value.join.assert_called_once()
        rendered = output.getvalue()
        self.assertIn("\r\x1b[Kplugin refused to load\n", rendered)
        self.assertTrue(rendered.endswith("native installer warning\n\n"), rendered)
        self.assertEqual(rendered.count("\n"), 3)

    def test_verbose_live_preparation_retires_all_painters_without_erasing_child(self):
        for nested in (False, True):
            for newline in ("", "\n"):
                with self.subTest(nested=nested, child_ends_in_lf=bool(newline)):
                    self._check_verbose_live_preparation(nested, newline)

    def _check_verbose_live_preparation(self, nested, newline):
        output = self.TTY()
        child_text = "unmodified tool warning" + newline
        image = "\x1b_Ga=T,q=2;TEST\x1b\\"
        erase = "\x1b_Ga=d,q=2;TEST\x1b\\"
        with (
            self.terminal(),
            lifecycle_output.invocation("start", verbose=True, console_level="all"),
            mock.patch.object(lifecycle_output, "record"),
            mock.patch.object(lifecycle_output._activity_log(), "record_event"),
            mock.patch.object(sys, "stdout", output),
            mock.patch.object(sys, "stderr", output),
            mock.patch.object(
                startup_output,
                "badge_for",
                return_value=startup_output.Badge(image, 2, erase),
            ),
            mock.patch.object(box.threading, "Thread") as workers,
            mock.patch.object(box.threading, "Event") as event,
        ):

            def tick(heartbeat):
                event.return_value.wait.side_effect = [False, True]
                heartbeat()

            def live_child(command, **kwargs):
                self.assertEqual(kwargs, {"text": True})
                self.assertTrue(output.getvalue().endswith(erase + "\r\x1b[K"))
                for text in ("\x1b#", "8", child_text):
                    # Inherited FDs bypass the launcher Python streams.
                    output.write(text)
                    before = output.getvalue()
                    for call in workers.call_args_list:
                        tick(call.kwargs["target"])
                    self.assertEqual(output.getvalue(), before)
                return subprocess.CompletedProcess(command, 0)

            with box.loading_stage("Preparing configuration", stage="configuration"):
                with (
                    box.loading_stage("Installing packages", stage="dependencies")
                    if nested
                    else contextlib.nullcontext()
                ):
                    with mock.patch.object(
                        box.subprocess, "run", side_effect=live_child
                    ):
                        box.run(["synthetic-tool"], preparation="container-image")
                    before = output.getvalue()
                    for call in workers.call_args_list:
                        tick(call.kwargs["target"])
                    self.assertEqual(output.getvalue(), before)
            self.assertEqual(workers.call_count, 2 if nested else 1)
            self.assertIs(sys.stderr, output)
        before, separator, after = output.getvalue().partition("\x1b#8" + child_text)
        self.assertTrue(before.endswith(erase + "\r\x1b[K"))
        self.assertTrue(separator)
        self.assertNotIn("\r", after)
        self.assertNotIn("\x1b", after)
        self.assertRegex(after, r"^\noc-box: ")
        self.assertEqual("Installing packages done" in after, nested)
        self.assertIn("Preparing configuration done", after)

    def test_fixed_fallback_notices_only_carry_badges_in_native_launch_scope(self):
        output = self.TTY()
        with (
            self.terminal(),
            mock.patch.object(sys, "stdin", output),
            mock.patch.object(sys, "stderr", output),
            mock.patch.object(sys, "stdout", output),
            mock.patch.object(startup_output.os, "ttyname", return_value="/dev/pts/1"),
        ):

            def notices():
                self.assertIsNone(file_preview.client_binding(box, {}, {}))
                history_recovery.report_skipped(["not-a-fixed-reason"])

            notices()
            with startup_output.invocation():
                startup_output.configure(lambda: "ascii")
                notices()
            notices()
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 6)
        self.assertEqual(lines[0], file_preview.NOTICE)
        self.assertEqual(
            lines[1],
            "oc-box: Automatic persisted-history snapshot not attempted (unavailable).",
        )
        self.assertEqual(lines[2:4], ["[/] " + row for row in lines[:2]])
        self.assertEqual(lines[4:], lines[:2])

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
        for prefix, final in ((b"\x1b#", b"8"), (b"\x1b%", b"G"), (b"\x1b$(", b"B")):
            with self.subTest(prefix=prefix):
                for char in prefix:
                    self.assertFalse(detector.feed(bytes((char,))))
                    self.assertFalse(detector.safe_to_draw())
                self.assertFalse(detector.feed(final))
                self.assertTrue(detector.safe_to_draw())

    def test_tui_designations_are_transparent_and_do_not_end_loading(self):
        for profile in ("konsole", "kitty", "kitty-with-drops"):
            for prefix, final in ((b"\x1b#", b"8"), (b"\x1b%", b"G")):
                with self.subTest(profile=profile, prefix=prefix):
                    self._check_tui_split_designation(profile, prefix, final)

    def _check_tui_split_designation(self, profile, prefix, final):
        environ = (
            {"TERM": "xterm-256color", "KONSOLE_VERSION": "260803"}
            if profile == "konsole"
            else {"TERM": "xterm-kitty", "KITTY_WINDOW_ID": "42"}
        )
        with (
            self.terminal(),
            mock.patch.dict(os.environ, environ, clear=True),
            mock.patch.object(host_drop.sharing, "protected_paths", return_value=[]),
        ):
            drops = (
                host_drop.Relay(box, {}, {}, profile="kitty")
                if profile == "kitty-with-drops"
                else None
            )
            with (
                mock.patch.object(drops, "start")
                if drops is not None
                else contextlib.nullcontext(),
                mock.patch.object(ui, "write_all") as output,
                mock.patch.object(ui.os, "isatty", return_value=True),
                mock.patch.object(ui.os, "ttyname", return_value="/dev/pts/1"),
                mock.patch.object(ui.os, "close"),
                mock.patch.object(
                    ui.os, "read", side_effect=[prefix, final, b"UI_READY", b""]
                ),
                mock.patch.object(ui.pty, "openpty", return_value=(20, 21)),
                mock.patch.object(ui.fcntl, "ioctl"),
                mock.patch.object(ui.termios, "tcgetattr"),
                mock.patch.object(ui.termios, "tcsetattr"),
                mock.patch.object(ui.tty, "setraw"),
                mock.patch.object(ui.signal, "signal"),
                mock.patch.object(ui.subprocess, "Popen") as child,
                mock.patch.object(
                    ui.select,
                    "select",
                    side_effect=[
                        ([20], [], []),
                        ([], [], []),
                        ([], [], []),
                        ([20], [], []),
                        ([20], [], []),
                        ([20], [], []),
                    ],
                ),
            ):
                child.return_value.poll.return_value = None
                child.return_value.wait.return_value = 0
                self.assertEqual(
                    ui.run_tui(
                        ["synthetic"],
                        input_fd=10,
                        output_fd=11,
                        refresh=0,
                        startup_logo="graphics",
                        file_drops=drops,
                    ),
                    0,
                )
        wire = b"".join(call.args[1] for call in output.call_args_list)
        before, designation, after = wire.partition(prefix + final)
        self.assertEqual(designation, prefix + final)
        self.assertEqual(before.count(b"\x1b_Ga=T"), 1)
        self.assertEqual(before.count(b"\x1b_Ga=d"), 1)
        self.assertEqual(after.count(b"\x1b_Ga=T"), 1)
        self.assertLess(after.index(b"\x1b_Ga=d"), after.index(b"UI_READY"))
        self.assertTrue(after.endswith(b"UI_READY"))

    @unittest.skipUnless(sys.platform == "linux", "Linux PTY/termios integration")
    def test_tui_progress_handoff_input_resize_and_terminal_restore(self):
        for mode in ("ascii", "fallback", "kitty", "konsole", "dumb"):
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
                self.assertIsNone(
                    startup_output._graphics_profile(supported | overrides)
                )
        with (
            mock.patch.object(
                startup_output.os, "ttyname", side_effect=lambda fd: f"/dev/pts/{fd}"
            ),
            mock.patch.dict(os.environ, supported, clear=True),
        ):
            self.assertEqual(startup_output.relay_badge(0, 1, "graphics").cells, 0)
        with (
            mock.patch.object(startup_output.os, "ttyname", return_value="/dev/pts/1"),
            mock.patch.object(
                startup_output.Path, "open", side_effect=FileNotFoundError
            ),
            mock.patch.dict(os.environ, supported, clear=True),
        ):
            self.assertEqual(startup_output.relay_badge(0, 1, "graphics").wire, "[/] ")
        for version in ("220399", "261303", "2608.03", "abc", "2608030"):
            self.assertIsNone(
                startup_output._graphics_profile(
                    {"TERM": "xterm-256color", "KONSOLE_VERSION": version}
                )
            )
        self.assertEqual(
            startup_output._graphics_profile(
                {"TERM": "xterm-256color", "KONSOLE_VERSION": "220400"}
            ),
            "konsole",
        )

    @unittest.skipUnless(sys.platform == "linux", "Linux PTY/termios integration")
    def test_saved_inline_badges_precede_backend_status_and_do_not_decorate_pipes(self):
        import fcntl
        import pty
        import select
        import struct
        import termios

        with tempfile.TemporaryDirectory() as name:
            project = Path(name) / "project"
            project.mkdir()
            with mock.patch.dict(
                os.environ,
                {
                    "OC_BOX_STATE_HOME": str(Path(name) / "registry"),
                    "OC_BOX_CONTAINER": "1",
                    "KONSOLE_VERSION": "260803",
                    "TERM": "xterm-256color",
                },
                clear=True,
            ):
                path, data = box.create_workspace(project, "debian")
                for style, arguments in (
                    ("graphics", ["--project", str(project)]),
                    ("ascii", ["reset", "--workspace", data["id"]]),
                    ("malformed-registry", ["--project", str(project)]),
                ):
                    with self.subTest(style=style):
                        if style == "malformed-registry":
                            # A parsed nonmapping must not abort the display-only
                            # lookup, before the authoritative backend preflight.
                            path.write_text("[]")
                        else:
                            workspace_preferences.set_preferences(
                                path, data["id"], startup_logo=style
                            )
                        master, slave = pty.openpty()
                        try:
                            fcntl.ioctl(
                                slave,
                                termios.TIOCSWINSZ,
                                struct.pack("HHHH", 24, 80, 0, 0),
                            )

                            def backend(**_):
                                startup_output.line(
                                    "first launcher line\nsecond launcher line",
                                    file=sys.stderr,
                                    flush=True,
                                )
                                startup_output.line("redirected launcher line")
                                os.write(slave, b"unmodified tool output\n")
                                raise box.BoxError("synthetic backend stopped")

                            with (
                                os.fdopen(os.dup(slave), "r") as stdin,
                                os.fdopen(os.dup(slave), "w") as stderr,
                                mock.patch.object(sys, "stdin", stdin),
                                contextlib.redirect_stderr(stderr),
                                contextlib.redirect_stdout(io.StringIO()) as pipe,
                                mock.patch.object(
                                    box, "check_backend", side_effect=backend
                                ),
                                self.assertRaisesRegex(
                                    box.BoxError, "synthetic backend"
                                ),
                            ):
                                box.main(arguments)
                            self.assertEqual(
                                pipe.getvalue(), "redirected launcher line\n"
                            )
                            output = bytearray()
                            while select.select([master], [], [], 0)[0]:
                                output.extend(os.read(master, 8192))
                        finally:
                            os.close(master)
                            os.close(slave)
                        prefix = b"\x1b\\\x1b[2C" if style == "graphics" else b"[/] "
                        for message in (
                            b"oc-box:",
                            b"first launcher line",
                            b"second launcher line",
                        ):
                            self.assertIn(prefix + message, output)
                        self.assertIn(b"\r\nunmodified tool output\r\n", output)
                        if style == "graphics":
                            self.assertIn(b"q=2,c=1,r=1,C=1;", output)
                            self.assertRegex(output, rb"a=d,d=i,i=0,p=[1-9][0-9]*,q=2;")
                            self.assertEqual(output.count(b"a=T,t=d,f=100,q=2,"), 3)
                        else:
                            self.assertNotIn(b"\x1b_G", output)

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
            "os.write(1,b'\\x1b[?1049h\\x1b[2J'); assert os.read(0,1)==b'g'; "
            "os.write(1,b'\\x1b]0;Boot'); "
            "time.sleep(0.2); os.write(1,b'ing\\x07UI_READY'); "
            "key=os.read(0,1); size=os.get_terminal_size(0); "
            "os.write(1,b'KEY='+key+(' SIZE=%dx%d'%(size.columns,size.lines)).encode()); "
            "time.sleep(0.15); os.write(1,b'\\x1b[?1049l')"
        )
        relay = (
            "import os,sys; sys.path.insert(0,sys.argv[1]+'/src'); "
            "from oc_box.terminal_ui import run_tui; "
            "sys.exit(run_tui([sys.executable,'-c',sys.argv[2]],"
            "env={**os.environ,'OC_BOX_RELAY_CHECK':'present'},refresh=0.03,"
            "startup_logo=sys.argv[3]))"
        )
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
                    if key not in startup_output._GRAPHICS_BLOCKERS
                },
                "TERM": {
                    "ascii": "xterm-kitty",
                    "fallback": "xterm-256color",
                    "kitty": "xterm-kitty",
                    "konsole": "xterm-256color",
                    "dumb": "dumb",
                }[mode],
                "KITTY_WINDOW_ID": "42" if mode != "konsole" else "",
                "KONSOLE_VERSION": "260803" if mode == "konsole" else "",
            },
        )
        output = bytearray()
        resumed_fixture = sent = False
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    output.extend(os.read(master, 65536))
                if not resumed_fixture and b"\x1b[?1049h" in output:
                    following = output.split(b"\x1b[?1049h", 1)[1]
                    if mode == "dumb" or b"Loading OpenCode" in following:
                        os.write(master, b"g")
                        resumed_fixture = True
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
                clear = b"\x1b[24;1H\x1b[2K\x1b8"
                self.assertLess(output.index(clear), output.index(b"\x1b[?1049h"))
            if mode in ("kitty", "konsole"):
                upload = b"I=" if mode == "kitty" else b"i=0,p="
                delete = b"d=N,I=" if mode == "kitty" else b"d=i,i=0,p="
                pattern = re.compile(
                    rb"\x1b_Ga=T,t=d,f=100,"
                    + upload
                    + rb"([1-9][0-9]*),q=2,c=1,r=1,C=1;([A-Za-z0-9+/=]+)\x1b\\\x1b\[2C"
                )
                matches = list(pattern.finditer(output))
                self.assertGreaterEqual(len(matches), 2)
                self.assertEqual({match[1] for match in matches}, {matches[0][1]})
                for match in matches:
                    self.assertEqual(
                        base64.b64decode(match[2], validate=True),
                        Path(ui.__file__).with_name("oc-box-mark.png").read_bytes(),
                    )
                deletion = b"\x1b_Ga=d," + delete + matches[0][1] + b",q=2;\x1b\\"
                self.assertEqual(output.count(deletion), len(matches))
                self.assertLess(matches[0].end(), output.index(deletion))
                self.assertLess(output.index(deletion), output.index(b"\x1b[?1049h"))
                self.assertGreater(matches[-1].start(), output.index(b"\x1b[?1049h"))
                self.assertLess(output.rindex(deletion), output.index(b"\x1b]0;Boot"))
            else:
                self.assertNotIn(b"\x1b_G", output)
            first_frame = output.split(b"\x1b[?1049h", 1)[0]
            self.assertEqual(
                b"[/] oc-box:" in first_frame, mode in ("ascii", "fallback")
            )
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
