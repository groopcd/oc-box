"""Offline interactive-fixture safety contracts; never run a Podman lifecycle."""

import contextlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import ProxyHandler, Request, build_opener

from oc_box import oc_box as box, terminal_ui, vault
from tools import test_interactive_podman as ui

fixture = ui.fixture


class InteractiveHostFixtureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="interactive-unit-")
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / (fixture.PREFIX + "interactive-unit")
        self.root.mkdir(mode=0o700)
        self.runtime = self.parent / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.binary = Path(sys.executable).resolve()
        self.source = {
            "checkout": "/synthetic/checkout",
            "version": "1.6.43+g" + "a" * 7,
            "commit": "a" * 40,
            "commit_count": 72,
            "dirty": False,
        }
        self.boot = b"00000000-0000-0000-0000-000000000001\n"
        self.environment = {
            "PATH": os.defpath,
            "HOME": str(self.root / "host-home"),
            "XDG_RUNTIME_DIR": str(self.runtime),
            "OC_BOX_CONTAINER": "0",
            "PERSONAL_GITHUB_TOKEN": "unrelated-synthetic-token",
            "OPENCODE_AUTH_CONTENT": "unrelated-synthetic-auth",
            "CONTAINER_HOST": "ssh://foreign",
            "CONTAINERS_CONF_OVERRIDE": "/foreign/config",
        }
        self.enter(patch.dict(os.environ, self.environment, clear=True))
        old_umask = os.umask(0o077)
        self.addCleanup(os.umask, old_umask)
        # A hard safety net: every native/engine invocation must be explicitly
        # replaced at its host boundary. No PATH resolution can escape this.
        self.run = self.enter(
            patch.object(
                box, "run", side_effect=AssertionError("unexpected host command")
            )
        )
        self.enter(patch.object(fixture, "system_boot_id", return_value=self.boot))
        fixture.prepare_root(self.root, "tumbleweed")
        fixture.prepare_podman_cli(self.root, self.binary)  # file-only ELF mapping
        self.prepare_native(self.root, self.binary)
        ui.write_metadata(self.root, self.source, self.binary)
        fixture.write_json_new(
            self.root / "config/opencode/opencode.json",
            fixture.fixture_config(12345, self.root),
        )
        fixture.write_plugin(self.root)
        (self.root / "podman-tmp/alive").write_bytes(self.boot)
        self.output = io.StringIO()
        self.enter(contextlib.redirect_stdout(self.output))
        self.enter(contextlib.redirect_stderr(self.output))

    def enter(self, manager):
        value = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        return value

    def prepare_native(self, root, binary):
        for name in ("config", "data", "state", "cache"):
            (root / ("host-" + name + "-2")).mkdir(mode=0o700)
        fixture.write_json_new(root / "executables.json", {"2": str(binary)})
        wrapper = root / "bin/opencode"
        wrapper.write_text(fixture.native_wrapper_source())
        wrapper.chmod(0o700)

    def record(self, name="source-a"):
        env = fixture.isolated_env(self.root)
        with patch.dict(os.environ, env, clear=True):
            path, data = box.create_workspace(self.root / name, "tumbleweed")
            data.update(opencode_version=fixture.NATIVE_VERSION, ready=True)
            box.atomic_json(path, data)
        return path, data

    def args(self, mode="tui"):
        return ui.parser().parse_args([mode, "--fixture", str(self.root)])

    def owner_args(self):
        return ui.parser().parse_args(
            ["start", "--work-dir", str(self.parent), "--opencode-v2", str(self.binary)]
        )

    def client_boundaries(self, *, live=True, running=True):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(
            patch.object(ui, "source_identity", return_value=self.source)
        )
        stack.enter_context(patch.object(fixture, "validate_store"))
        stack.enter_context(
            patch.object(fixture, "check_native_version", return_value="2.2.6")
        )
        stack.enter_context(
            patch.object(
                fixture,
                "verify_owned_resources",
                return_value={"State": {"Running": running}},
            )
        )
        stack.enter_context(
            patch.object(
                fixture.workspace_service, "controller_running", return_value=live
            )
        )
        connection = {
            "url": "http://127.0.0.1:12345",
            "password": "synthetic-control-not-for-metadata",
            "session": "ses_shared",
        }
        stack.enter_context(
            patch.object(
                fixture.workspace_service,
                "attach",
                side_effect=lambda *a: contextlib.nullcontext(connection),
            )
        )
        return stack

    def reports(self, mode):
        return [
            json.loads(path.read_text())
            for path in self.root.glob("interactive-" + mode + "-*-report.json")
        ]

    def test_option_matrix_refuses_replay_selectors_and_missing_explicit_native(self):
        valid = [
            ["start", "--work-dir", "/fixtures", "--opencode-v2", "/native"],
            *[
                [mode, "--fixture", "/fixtures/exact-root"]
                for mode in ui.MODES
                if mode != "start"
            ],
        ]
        for arguments in valid:
            with self.subTest(valid=arguments):
                ui.validate_options(ui.parser().parse_args(arguments))
        invalid = [
            ["start"],
            ["start", "--work-dir", "/fixtures"],
            [
                "start",
                "--work-dir",
                "/fixtures",
                "--opencode-v2",
                "/native",
                "--fixture",
                "/old",
            ],
            *[[mode] for mode in ui.MODES if mode != "start"],
            *[
                ["tui", "--fixture", "/old", flag, value]
                for flag, value in (
                    ("--work-dir", "/x"),
                    ("--temp-dir", "/x"),
                    ("--opencode-v2", "/native"),
                    ("--distro", "tumbleweed"),
                )
            ],
        ]
        for arguments in invalid:
            with (
                self.subTest(invalid=arguments),
                self.assertRaises(fixture.FixtureError),
            ):
                ui.validate_options(ui.parser().parse_args(arguments))
        for arguments in (
            ["resume"],
            ["tui", "--fixture", "/old", "--no-vault"],
            ["terminal", "--workspace", "foreign"],
        ):
            with self.subTest(unknown=arguments), self.assertRaises(SystemExit):
                ui.parser().parse_args(arguments)

    def test_container_refuses_before_source_placement_native_or_engine(self):
        with (
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "1"}),
            patch.object(ui, "source_identity") as source,
            patch.object(ui, "own") as owner,
        ):
            self.assertEqual(
                ui.main(
                    [
                        "start",
                        "--work-dir",
                        str(self.parent),
                        "--opencode-v2",
                        str(self.binary),
                    ]
                ),
                1,
            )
        source.assert_not_called()
        owner.assert_not_called()
        self.run.assert_not_called()
        self.assertIn("host-only", self.output.getvalue())

    def test_help_is_harmless_and_invalid_arguments_never_echo_private_input(self):
        with patch.object(fixture, "host_guard") as guard:
            with self.assertRaises(SystemExit) as exited:
                ui.main(["--help"])
            self.assertEqual(exited.exception.code, 0)
            guard.assert_not_called()
        with self.assertRaises(SystemExit):
            ui.main(["start", "--unknown-private-value"])
        self.assertNotIn("unknown-private-value", self.output.getvalue())

    def test_ui_modes_require_real_terminals_before_source_or_placement(self):
        for mode in ui.MODES:
            with (
                self.subTest(mode=mode),
                patch.object(sys.stdin, "isatty", return_value=False),
            ):
                if mode == "stop":
                    ui.require_tty(mode)
                else:
                    with self.assertRaisesRegex(
                        fixture.FixtureError, "interactive-tty-required"
                    ):
                        ui.require_tty(mode)
        with (
            patch.object(fixture, "host_guard"),
            patch.object(sys.stdin, "isatty", return_value=False),
            patch.object(ui, "source_identity") as source,
        ):
            self.assertEqual(ui.main(["tui", "--fixture", str(self.root)]), 1)
            source.assert_not_called()

    def test_fresh_parent_never_reuses_existing_completed_or_nested_fixture(self):
        self.assertEqual(ui.fresh_parent(self.parent), self.parent)
        nested = self.root / "child"
        nested.mkdir()
        for path in (self.root, nested, self.parent / "missing"):
            with self.subTest(path=path), self.assertRaises(fixture.FixtureError):
                ui.fresh_parent(path)
        alias = self.parent / "alias"
        alias.symlink_to(self.parent, target_is_directory=True)
        with self.assertRaisesRegex(fixture.FixtureError, "unsafe-path"):
            ui.fresh_parent(alias)

        for name, marker in (
            ("oc-box-live-v2-unit", None),
            ("oc-box-live-backup-unit", None),
            ("renamed-ids", "fixture-ids.json"),
            ("renamed-global", "fixture.json"),
            ("renamed-interactive", "interactive.json"),
        ):
            retained = self.parent / name
            child = retained / "nested"
            child.mkdir(parents=True)
            if marker:
                (retained / marker).write_text("{}\n")
            for path in (retained, child):
                with (
                    self.subTest(retained=path),
                    self.assertRaisesRegex(
                        fixture.FixtureError, "fresh-parent-required"
                    ),
                ):
                    ui.fresh_parent(path)

    def test_both_placement_options_refuse_retained_evidence_before_any_preparation(
        self,
    ):
        paths = []
        for name, marker in (
            ("oc-box-live-v2-placement", None),
            ("oc-box-live-backup-placement", None),
            ("renamed-placement", "fixture-ids.json"),
        ):
            retained = self.parent / name
            nested = retained / "nested"
            nested.mkdir(parents=True)
            if marker:
                (retained / marker).write_text("[]\n")
            paths.extend((retained, nested))
        before = sorted(self.parent.rglob("*"))
        with contextlib.ExitStack() as stack:
            boundaries = [
                stack.enter_context(patch.object(target, name))
                for target, name in (
                    (ui, "native_binary"),
                    (ui.shutil, "which"),
                    (ui.tempfile, "mkdtemp"),
                    (fixture, "prepare_root"),
                    (fixture, "prepare_podman_cli"),
                    (fixture, "temporary_location"),
                    (fixture, "prepare_native_cli"),
                    (fixture, "validate_store"),
                    (ui, "provider"),
                    (box, "main"),
                    (fixture, "verify_owned_resources"),
                )
            ]
            for option in ("work_dir", "temp_dir"):
                for path in paths:
                    args = self.owner_args()
                    setattr(args, option, path)
                    with (
                        self.subTest(option=option, retained=path),
                        self.assertRaisesRegex(
                            fixture.FixtureError, "fresh-parent-required"
                        ),
                    ):
                        ui.own(args, self.source)
            for boundary in boundaries:
                boundary.assert_not_called()
        self.run.assert_not_called()
        self.assertEqual(sorted(self.parent.rglob("*")), before)
        self.assertEqual(
            (self.parent / "renamed-placement/fixture-ids.json").read_text(), "[]\n"
        )

    def test_source_identity_refuses_dirty_unknown_and_changed_versions(self):
        info = {k: v for k, v in self.source.items() if k != "checkout"}
        with (
            patch.object(fixture, "get_version_info", return_value=info),
            patch.object(
                ui.subprocess, "run", return_value=SimpleNamespace(stdout=b"")
            ) as git,
        ):
            identity = ui.source_identity()
            self.assertEqual(identity["commit"], info["commit"])
            self.assertIn("--untracked-files=all", git.call_args.args[0])
        for change, status in (
            ({"dirty": True}, b""),
            ({"commit": None}, b""),
            ({"version": "1.6.0+unknown"}, b""),
            ({}, b"?? extra.py"),
        ):
            with (
                self.subTest(change=change, status=status),
                patch.object(
                    fixture, "get_version_info", return_value={**info, **change}
                ),
                patch.object(
                    ui.subprocess, "run", return_value=SimpleNamespace(stdout=status)
                ),
            ):
                with self.assertRaisesRegex(
                    fixture.FixtureError, "clean-source-required"
                ):
                    ui.source_identity()
        self.record()
        for changed in (
            {**self.source, "version": "1.6.44+gbbbbbbb"},
            {**self.source, "commit": "b" * 40},
            {**self.source, "checkout": "/different/checkout"},
        ):
            with (
                self.subTest(source=changed),
                self.assertRaisesRegex(fixture.FixtureError, "source-changed"),
            ):
                ui.validate_interactive(self.root, changed)
        self.run.assert_not_called()

    def test_retained_metadata_refuses_noninteractive_foreign_and_extra_workspaces(
        self,
    ):
        self.record()
        root, binary = ui.validate_interactive(self.root, self.source)
        self.assertEqual((root, binary), (self.root, self.binary))
        fixture.write_json_new(self.root / "result.json", {"status": "passed"})
        with self.assertRaisesRegex(fixture.FixtureError, "interactive-only"):
            ui.validate_interactive(self.root, self.source)
        (self.root / "result.json").unlink()
        self.record("source-b")
        with self.assertRaisesRegex(fixture.FixtureError, "workspace-allowlist"):
            ui.validate_interactive(self.root, self.source)
        self.run.assert_not_called()

    def test_missing_interactive_marker_and_changed_native_selector_fail_closed(self):
        self.record()
        marker = self.root / "interactive.json"
        original = marker.read_text()
        marker.unlink()
        with self.assertRaises((fixture.FixtureError, FileNotFoundError)):
            ui.validate_interactive(self.root, self.source)
        fixture.write_json_new(marker, json.loads(original))
        (self.root / "bin/opencode").write_text("#!/bin/sh\nexit 0\n")
        with self.assertRaisesRegex(fixture.FixtureError, "native-selector"):
            ui.validate_interactive(self.root, self.source)
        self.run.assert_not_called()

    def test_native_entrypoint_pins_symlink_target_and_refuses_nonexecutables(self):
        entry = self.parent / "native-entry"
        entry.symlink_to(self.binary)
        self.assertEqual(ui.native_binary(entry), self.binary)
        plain = self.parent / "not-executable"
        plain.write_text("synthetic")
        plain.chmod(0o600)
        with self.assertRaisesRegex(fixture.FixtureError, "native-executable"):
            ui.native_binary(plain)
        entry.unlink()
        entry.symlink_to(self.parent / "missing")
        with self.assertRaises(FileNotFoundError):
            ui.native_binary(entry)
        self.run.assert_not_called()

    def test_stale_missing_and_invalid_boot_refuse_before_any_engine_native_or_client(
        self,
    ):
        self.record()
        marker = self.root / "podman-tmp/alive"
        for value in (b"00000000-0000-0000-0000-000000000002\n", b"invalid", None):
            if value is None:
                marker.unlink()
            else:
                marker.write_bytes(value)
            with (
                self.subTest(value=value),
                patch.object(fixture, "validate_store") as store,
                patch.object(fixture, "check_native_version") as native,
                patch.object(box, "main") as launch,
            ):
                with self.assertRaises(fixture.FixtureError):
                    ui.attach(self.args(), self.source)
                store.assert_not_called()
                native.assert_not_called()
                launch.assert_not_called()
        self.run.assert_not_called()

    def test_positive_isolation_and_metadata_never_retain_values_or_control_credentials(
        self,
    ):
        state = ui.provider_state()
        self.assertEqual(set(state["values"]), {fixture.KEY})
        self.assertEqual(
            (state["requests"], state["before"], state["errors"], state["markers"]),
            (0, 0, 0, set()),
        )
        env = fixture.isolated_env(self.root)
        for name in self.environment.keys() - {
            "PATH",
            "HOME",
            "XDG_RUNTIME_DIR",
            "OC_BOX_CONTAINER",
        }:
            self.assertNotIn(name, env)
        self.assertEqual(env["HOME"], str(self.root / "host-home"))
        self.assertEqual(env["OC_BOX_STATE_HOME"], str(self.root / "registry"))
        self.assertTrue(env["PATH"].startswith(str(self.root / "bin") + ":"))
        result = ui.report_value(
            "start", self.source, "returned", "launcher-return", 0, state
        )
        encoded = json.dumps(result) + (self.root / "interactive.json").read_text()
        for value in (
            *state["values"].values(),
            "synthetic-control-not-for-metadata",
            "unrelated-synthetic-token",
            "unrelated-synthetic-auth",
        ):
            self.assertNotIn(value, encoded)
        self.assertNotIn("passed", result.get("outcome", ""))
        self.assertEqual(result["manual_observations"], "NOT_AUTOMATICALLY_PASSED")

    def test_clients_refuse_without_live_owner_and_never_own_or_unlock(self):
        self.record()
        for live, running in ((False, True), (True, False)):
            with self.subTest(live=live, running=running):
                stack = self.client_boundaries(live=live, running=running)
                with (
                    patch.object(box, "main") as launch,
                    patch.object(vault, "hidden") as unlock,
                ):
                    self.assertEqual(ui.attach(self.args(), self.source), 1)
                    launch.assert_not_called()
                    unlock.assert_not_called()
                stack.close()
        self.assertTrue(
            all(
                report["category"] == "live-owner-required"
                for report in self.reports("tui")
            )
        )

    def test_attach_validation_order_journals_inflight_owner_and_holds_lease(self):
        self.record()
        self.assertEqual(fixture.private_json(self.root, "fixture-ids.json"), {})
        order = []
        real_boot = fixture.require_runtime_boot
        real_record = fixture.record_ids

        @contextlib.contextmanager
        def lease(*args):
            order.append("lease-enter")
            yield {
                "url": "http://127.0.0.1:12345",
                "password": "synthetic-control-not-for-metadata",
                "session": "ses_shared",
            }
            order.append("lease-exit")

        def relay(command):
            order.append("relay")
            self.assertIn("--server", command)
            self.assertNotIn("--no-vault", command)
            self.assertNotIn("PERSONAL_GITHUB_TOKEN", os.environ)
            return 0

        with (
            patch.object(
                fixture,
                "require_runtime_boot",
                side_effect=lambda root: (order.append("boot"), real_boot(root))[1],
            ),
            patch.object(
                fixture,
                "validate_store",
                side_effect=lambda *a, **k: order.append("store"),
            ) as store,
            patch.object(
                fixture,
                "check_native_version",
                side_effect=lambda *a: order.append("native"),
            ),
            patch.object(
                fixture,
                "verify_owned_resources",
                side_effect=lambda *a, **k: (
                    order.append("resources"),
                    {"State": {"Running": True}},
                )[1],
            ),
            patch.object(
                fixture.workspace_service,
                "controller_running",
                side_effect=lambda *a: (order.append("live"), True)[1],
            ),
            patch.object(
                fixture,
                "record_ids",
                side_effect=lambda root: (order.append("journal"), real_record(root))[
                    1
                ],
            ),
            patch.object(fixture.workspace_service, "attach", side_effect=lease),
            patch.object(box, "run_tui", side_effect=relay),
            patch.object(ui, "source_identity", return_value=self.source),
            patch.object(
                box, "main", side_effect=AssertionError("owner-capable dispatch")
            ) as launch,
        ):
            self.assertEqual(ui.attach(self.args(), self.source), 0)
        self.assertEqual(
            order,
            [
                "boot",
                "store",
                "native",
                "resources",
                "live",
                "journal",
                "lease-enter",
                "relay",
                "lease-exit",
            ],
        )
        store.assert_called_once_with(self.root, allow_unrecorded=True)
        launch.assert_not_called()
        self.assertEqual(
            set(fixture.private_json(self.root, "fixture-ids.json")), {"source-a"}
        )

    def test_clients_use_production_authenticated_commands_and_unmodified_relays_without_main(
        self,
    ):
        _, data = self.record()
        self.client_boundaries()
        original_run_tui = box.run_tui
        private_depth = [0]
        original_private = fixture.private_output

        @contextlib.contextmanager
        def private():
            private_depth[0] += 1
            try:
                with original_private():
                    yield
            finally:
                private_depth[0] -= 1

        def relay(command, **kwargs):
            self.assertEqual(private_depth[0], 0)
            self.assertIs(box.run_tui, original_run_tui)
            self.assertNotIn("mini", command)
            self.assertNotIn("--auto", command)
            self.assertNotIn("--standalone", command)
            self.assertEqual(kwargs["env"], command.environment)
            self.assertEqual(
                command.environment["OPENCODE_PASSWORD"],
                "synthetic-control-not-for-metadata",
            )
            self.assertNotIn("PERSONAL_GITHUB_TOKEN", command.environment)
            self.assertNotIn("OPENCODE_AUTH_CONTENT", command.environment)
            return 0

        with (
            patch.object(fixture, "private_output", side_effect=private),
            patch.object(sys.stdin, "isatty", return_value=True),
            patch.object(sys.stdout, "isatty", return_value=True),
            patch.object(terminal_ui, "run_tui", side_effect=relay) as native_relay,
            patch.object(
                fixture.workspace_service,
                "client_command",
                wraps=fixture.workspace_service.client_command,
            ) as client,
            patch.object(
                fixture.workspace_service, "web_session", return_value=0
            ) as web,
            patch.object(vault, "hidden") as unlock,
            patch.object(
                box, "main", side_effect=AssertionError("owner-capable dispatch")
            ) as launch,
            patch.object(
                box, "launch_credentials", side_effect=AssertionError("attach unlocked")
            ) as credentials,
            patch.object(
                box, "prepare_runtime", side_effect=AssertionError("attach refreshed")
            ),
        ):
            for mode in ("tui", "terminal", "web"):
                self.assertEqual(ui.attach(self.args(mode), self.source), 0)
            self.assertEqual(native_relay.call_count, 2)
            self.assertTrue(web.call_args.args[3].no_open)
            self.assertEqual(
                web.call_args.args[3].port,
                fixture.workspace_service.web_parser().get_default("port"),
            )
            self.assertEqual(
                client.call_args_list[0].args[3],
                [
                    box.CONTAINER_OPENCODE,
                    box.container_project(data),
                    "--server",
                    "http://127.0.0.1:12345",
                    "--session",
                    "ses_shared",
                ],
            )
            self.assertEqual(
                client.call_args_list[1].args[3],
                [
                    "python3",
                    "-c",
                    (box.PACKAGE / "session_terminal.py").read_text(),
                    "http://127.0.0.1:12345",
                    "ses_shared",
                    box.container_project(data),
                ],
            )
            self.assertTrue(
                all(
                    call.kwargs == {"interactive": True}
                    for call in client.call_args_list
                )
            )
            self.assertNotIn("show_loading", native_relay.call_args_list[0].kwargs)
            self.assertFalse(native_relay.call_args_list[1].kwargs["show_loading"])
            unlock.assert_not_called()
            launch.assert_not_called()
            credentials.assert_not_called()

    def test_stop_only_targets_verified_source_and_records_actual_stopped_outcome(self):
        _, data = self.record()
        self.client_boundaries()
        with (
            patch.object(box, "stop_container") as stop,
            patch.object(
                box, "main", side_effect=AssertionError("owner-capable dispatch")
            ) as launch,
            patch.object(
                fixture,
                "verify_owned_resources",
                return_value={"State": {"Running": True}},
            ) as verify,
        ):
            self.assertEqual(ui.attach(self.args("stop"), self.source), 0)
        stop.assert_called_once_with(data)
        launch.assert_not_called()
        self.assertTrue(verify.call_args.kwargs["stopped"])
        report = self.reports("stop")[0]
        self.assertEqual(
            (report["source"], report["outcome"], report["scope"]),
            (self.source, "returned", "interactive-only"),
        )

    def test_stale_unix_controller_socket_refuses_without_client_or_owner_fallback(
        self,
    ):
        _, data = self.record()
        service = fixture.workspace_service
        real_controller = service.controller_running
        self.client_boundaries()
        with patch.dict(os.environ, fixture.isolated_env(self.root), clear=True):
            address = service.socket_path(box, data)
            with socket.socket(socket.AF_UNIX) as stale:
                stale.bind(str(address))
        self.assertTrue(address.is_socket())
        with (
            patch.object(service, "controller_running", side_effect=real_controller),
            patch.object(
                service,
                "attach",
                side_effect=AssertionError("stale controller accepted"),
            ) as lease,
            patch.object(terminal_ui, "run_tui") as relay,
            patch.object(box, "main") as launch,
            patch.object(box, "launch_credentials") as credentials,
            patch.object(box, "prepare_runtime") as prepare,
        ):
            self.assertEqual(ui.attach(self.args(), self.source), 1)
        for operation in (lease, relay, launch, credentials, prepare):
            operation.assert_not_called()
        self.assertEqual(self.reports("tui")[0]["category"], "live-owner-required")
        self.assertTrue(address.is_socket())  # No repair/removal of retained state.

    def test_controller_loss_after_authenticated_attach_fails_tui_and_web_without_owner_fallback(
        self,
    ):
        path, data = self.record()
        service = fixture.workspace_service
        real_controller, real_attach = service.controller_running, service.attach
        isolated = fixture.isolated_env
        self.client_boundaries()
        info = {
            "State": {"Running": True},
            "Mounts": [
                {"Source": data["project"], "Destination": box.container_project(data)}
            ],
        }
        for mode in ("tui", "web"):
            with (
                self.subTest(mode=mode),
                # Socket names have a kernel pathname cap: do not nest this
                # runtime below the fixture's already-nested metadata tree.
                tempfile.TemporaryDirectory(prefix="loss-") as runtime,
                socket.socket() as unavailable,
            ):
                # Bound but not listening: a reserved, genuinely disconnected
                # local endpoint, never a real provider or Podman resource.
                unavailable.bind(("127.0.0.1", 0))
                endpoint = "http://127.0.0.1:" + str(unavailable.getsockname()[1])
                connection = {
                    "url": endpoint,
                    "password": "s" * 40,
                    "session": "ses_shared",
                    "workspace": data["id"],
                    "version": "2.2.6",
                }
                owner_lost = threading.Event()
                errors = []
                with patch.dict(
                    os.environ,
                    {**fixture.isolated_env(self.root), "XDG_RUNTIME_DIR": runtime},
                    clear=True,
                ):
                    address = service.socket_path(box, data)
                listener = socket.socket(socket.AF_UNIX)
                listener.settimeout(3)
                listener.bind(str(address))
                listener.listen(2)

                def controller():
                    try:
                        # The liveness probe connects and immediately closes.
                        with listener.accept()[0] as probe:
                            probe.settimeout(3)
                            self.assertEqual(probe.recv(4096), b"")
                        with listener.accept()[0] as client:
                            client.settimeout(3)
                            request = bytearray()
                            while b"\n" not in request and len(request) < 4096:
                                chunk = client.recv(4096)
                                self.assertTrue(chunk)
                                request.extend(chunk)
                            self.assertEqual(
                                json.loads(request),
                                {"action": "attach", "workspace": data["id"]},
                            )
                            client.sendall((json.dumps(connection) + "\n").encode())
                        # Like explicit stop/service failure, close the actual
                        # controller despite the client's authenticated lease.
                    except BaseException as error:
                        errors.append(error)
                    finally:
                        listener.close()
                        owner_lost.set()

                worker = threading.Thread(target=controller, daemon=True)
                worker.start()
                observed = []

                def disconnected():
                    self.assertTrue(owner_lost.wait(3))
                    self.assertFalse(service.controller_running(box, data))
                    observed.append("disconnected")
                    return build_opener(ProxyHandler({})).open(
                        endpoint + "/api/info", timeout=1
                    )

                def failed_relay(command, **kwargs):
                    self.assertIn(endpoint, command)
                    self.assertEqual(
                        kwargs["env"]["OPENCODE_PASSWORD"], connection["password"]
                    )
                    try:
                        with disconnected():
                            self.fail(
                                "disconnected native endpoint unexpectedly responded"
                            )
                    except OSError:
                        return 23  # Actual relay-boundary connection failure.

                def failed_api(
                    actual_box, actual_data, authenticated, method, target, value=None
                ):
                    self.assertIs(actual_box, box)
                    self.assertEqual(actual_data, data)
                    self.assertEqual(authenticated, connection)
                    self.assertEqual((method, target), ("POST", "/api/pair"))
                    return disconnected()  # Propagates the real connection refusal.

                try:
                    with (
                        patch.object(
                            fixture,
                            "isolated_env",
                            side_effect=lambda root: {
                                **isolated(root),
                                "XDG_RUNTIME_DIR": runtime,
                            },
                        ),
                        patch.object(box, "inspect_owned", return_value=info),
                        patch.object(
                            service, "controller_running", side_effect=real_controller
                        ),
                        patch.object(
                            service, "attach", side_effect=real_attach
                        ) as authenticated_attach,
                        patch.object(
                            terminal_ui, "run_tui", side_effect=failed_relay
                        ) as relay,
                        patch.object(
                            service,
                            "Gateway",
                            return_value=contextlib.nullcontext(
                                SimpleNamespace(url=endpoint)
                            ),
                        ),
                        patch.object(service, "api", side_effect=failed_api),
                        patch.object(
                            box,
                            "main",
                            side_effect=AssertionError("owner-capable dispatch"),
                        ) as launch,
                        patch.object(
                            box,
                            "launch_credentials",
                            side_effect=AssertionError("attach unlocked"),
                        ) as credentials,
                        patch.object(
                            box,
                            "prepare_runtime",
                            side_effect=AssertionError("attach restarted"),
                        ) as prepare,
                    ):
                        self.assertEqual(
                            ui.attach(self.args(mode), self.source),
                            23 if mode == "tui" else 1,
                        )
                        authenticated_attach.assert_called_once_with(box, path, data)
                        self.assertEqual(relay.call_count, 1 if mode == "tui" else 0)
                        for operation in (launch, credentials, prepare):
                            operation.assert_not_called()
                finally:
                    listener.close()
                    worker.join(timeout=3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(observed, ["disconnected"])
                report = self.reports(mode)[0]
                self.assertEqual(report["outcome"], "failed")
                self.assertEqual(
                    report["category"], "client-exit" if mode == "tui" else "os-error"
                )
                self.assertEqual(report["exit_code"], 23 if mode == "tui" else 1)
                self.assertEqual(
                    report["manual_observations"], "NOT_AUTOMATICALLY_PASSED"
                )
                self.assertNotIn(connection["password"], json.dumps(report))

    def test_provider_real_local_contract_only_key_and_bounded_thread_cleanup(self):
        state = ui.provider_state()
        worker_seen = []
        real_provider = fixture.make_provider

        def remember(state):
            server, worker = real_provider(state)
            worker_seen.append(worker)
            return server, worker

        with patch.object(fixture, "make_provider", side_effect=remember):
            with ui.provider(state) as port:
                request = Request(
                    "http://127.0.0.1:" + str(port) + "/v1/chat/completions",
                    data=json.dumps(
                        {"messages": [{"role": "user", "content": "synthetic"}]}
                    ).encode(),
                    headers={
                        "Authorization": "Bearer " + state["values"][fixture.KEY],
                        "Content-Type": "application/json",
                    },
                )
                with build_opener(ProxyHandler({})).open(
                    request, timeout=3
                ) as response:
                    self.assertIn(fixture.DONE_MARKER, response.read().decode())
                self.assertTrue(worker_seen[0].is_alive())
        self.assertFalse(worker_seen[0].is_alive())
        self.assertEqual((state["requests"], state["errors"]), (1, 0))

    def test_provider_cleanup_failure_is_reported_not_silently_ignored(self):
        server = SimpleNamespace(
            server_port=1, shutdown=lambda: None, server_close=lambda: None
        )
        worker = SimpleNamespace(
            join=lambda **kwargs: self.assertEqual(kwargs, {"timeout": 3}),
            is_alive=lambda: True,
        )
        with patch.object(fixture, "make_provider", return_value=(server, worker)):
            with self.assertRaisesRegex(
                fixture.FixtureError, "provider-cleanup-timeout"
            ):
                with ui.provider(ui.provider_state()):
                    pass

    def test_owner_store_refusal_precedes_native_provider_and_ui_without_private_output(
        self,
    ):
        def refuse(root):
            print("synthetic-private-preparation-output")
            raise subprocess.CalledProcessError(
                17, ["synthetic-private-argv"], stderr="synthetic-private-stderr"
            )

        with (
            patch.object(ui.shutil, "which", return_value=str(self.binary)),
            patch.object(fixture, "validate_store", side_effect=refuse),
            patch.object(fixture, "prepare_native_cli") as native,
            patch.object(fixture, "make_provider") as provider,
            patch.object(box, "main") as launch,
        ):
            self.assertEqual(ui.own(self.owner_args(), self.source), 1)
        for operation in (native, provider, launch):
            operation.assert_not_called()
        reports = list(
            self.parent.glob(
                fixture.PREFIX + "interactive-*/interactive-start-*-report.json"
            )
        )
        self.assertEqual(len(reports), 1)
        report = json.loads(reports[0].read_text())
        self.assertEqual(
            (report["outcome"], report["category"]), ("failed", "subprocess-exit")
        )
        self.assertNotIn(
            "synthetic-private", json.dumps(report) + self.output.getvalue()
        )

    def test_owner_preparation_order_foreground_provider_and_no_vault_only_owner(self):
        order = []
        live = [False]
        private_depth = [0]
        real_prepare = fixture.prepare_root
        original_private = fixture.private_output
        original_run_tui = box.run_tui
        roots = []

        @contextlib.contextmanager
        def private():
            private_depth[0] += 1
            try:
                with original_private():
                    yield
            finally:
                private_depth[0] -= 1

        @contextlib.contextmanager
        def synthetic_provider(state):
            order.append("provider")
            live[0] = True
            try:
                yield 12345
            finally:
                live[0] = False
                order.append("provider-exit")
                if launch_failure[0]:
                    raise fixture.FixtureError("provider-cleanup-timeout")

        def prepare(root, distro):
            roots.append(root)
            order.append("root")
            real_prepare(root, distro)

        real_podman = fixture.prepare_podman_cli

        def podman(root, binary):
            order.append("podman")
            return real_podman(root, binary)

        def native(root, binary):
            order.append("native")
            self.prepare_native(root, binary)

        def store(root):
            order.append("store")
            self.assertEqual(os.environ["OC_BOX_STATE_HOME"], str(root / "registry"))
            self.assertEqual(Path(tempfile.gettempdir()), root / "temporary")

        launch_failure = [False]

        def launch(arguments):
            order.append("launch")
            self.assertTrue(live[0])
            self.assertEqual(private_depth[0], 0)
            self.assertIs(box.run_tui, original_run_tui)
            self.assertEqual(
                arguments,
                ["start", "--project", str(roots[0] / "source-a"), "--no-vault"],
            )
            self.assertIn(fixture.KEY, os.environ)
            for name in (
                fixture.SIDE,
                fixture.ADDED,
                "PERSONAL_GITHUB_TOKEN",
                "OPENCODE_AUTH_CONTENT",
            ):
                self.assertNotIn(name, os.environ)
            path, data = box.create_workspace(roots[0] / "source-a", "tumbleweed")
            data["opencode_version"] = "2.2.6"
            box.atomic_json(path, data)
            if launch_failure[0]:
                raise KeyboardInterrupt()
            return 0

        with (
            patch.object(fixture, "private_output", side_effect=private),
            patch.object(ui.shutil, "which", return_value=str(self.binary)),
            patch.object(fixture, "prepare_root", side_effect=prepare),
            patch.object(fixture, "prepare_podman_cli", side_effect=podman),
            patch.object(fixture, "validate_store", side_effect=store),
            patch.object(fixture, "prepare_native_cli", side_effect=native),
            patch.object(ui, "provider", side_effect=synthetic_provider),
            patch.object(box, "main", side_effect=launch),
            patch.object(fixture, "verify_owned_resources"),
            patch.object(ui, "source_identity", return_value=self.source),
        ):
            self.assertEqual(ui.own(self.owner_args(), self.source), 0)
        self.assertEqual(
            order,
            [
                "root",
                "podman",
                "store",
                "native",
                "provider",
                "launch",
                "provider-exit",
            ],
        )
        self.assertFalse(live[0])
        self.assertNotEqual(roots[0], self.root)
        reports = list(roots[0].glob("interactive-start-*-report.json"))
        self.assertEqual(json.loads(reports[0].read_text())["outcome"], "returned")
        self.assertFalse((roots[0] / "result.json").exists())
        self.assertNotIn(fixture.KEY, os.environ)
        self.assertIn("--fixture", self.output.getvalue())

        # A separate fresh owner records the first UI interruption even when
        # provider shutdown also fails; it still journals verified resource IDs.
        launch_failure[0] = True
        roots.clear()
        order.clear()
        with (
            patch.object(fixture, "private_output", side_effect=private),
            patch.object(ui.shutil, "which", return_value=str(self.binary)),
            patch.object(fixture, "prepare_root", side_effect=prepare),
            patch.object(fixture, "prepare_podman_cli", side_effect=podman),
            patch.object(fixture, "validate_store", side_effect=store),
            patch.object(fixture, "prepare_native_cli", side_effect=native),
            patch.object(ui, "provider", side_effect=synthetic_provider),
            patch.object(box, "main", side_effect=launch),
            patch.object(fixture, "verify_owned_resources"),
        ):
            self.assertEqual(ui.own(self.owner_args(), self.source), 130)
        report = json.loads(
            next(roots[0].glob("interactive-start-*-report.json")).read_text()
        )
        self.assertEqual(
            (report["outcome"], report["category"]), ("interrupted", "interrupted")
        )
        self.assertEqual(report["secondary_errors"], ["provider-cleanup-timeout"])
        self.assertEqual(
            set(fixture.private_json(roots[0], "fixture-ids.json")), {"source-a"}
        )
        self.assertFalse(live[0])

    def test_reports_keep_actual_failure_scope_and_never_exception_argv_or_values(self):
        self.record()
        self.client_boundaries()
        error = subprocess.CalledProcessError(
            19,
            ["/private", "synthetic-secret-argv"],
            output="synthetic-secret-output",
            stderr="synthetic-secret-errors",
        )
        with patch.object(box, "run_tui", side_effect=error):
            self.assertEqual(ui.attach(self.args(), self.source), 1)
        report = self.reports("tui")[0]
        self.assertEqual(
            (report["outcome"], report["category"]), ("failed", "subprocess-exit")
        )
        self.assertEqual(report["manual_observations"], "NOT_AUTOMATICALLY_PASSED")
        self.assertNotIn(
            "synthetic-secret", json.dumps(report) + self.output.getvalue()
        )

    def test_owner_source_change_after_ui_and_provider_drain_refuses_success_without_relabeling(
        self,
    ):
        for changed in (
            {**self.source, "commit": "b" * 40},
            fixture.FixtureError("clean-source-required"),
        ):
            live = [False]
            root_seen = []

            @contextlib.contextmanager
            def provider(state):
                live[0] = True
                try:
                    yield 12345
                finally:
                    live[0] = False

            def launch(arguments):
                self.assertTrue(live[0])
                root = Path(arguments[arguments.index("--project") + 1]).parent
                root_seen.append(root)
                path, data = box.create_workspace(root / "source-a", "tumbleweed")
                data["opencode_version"] = "2.2.6"
                box.atomic_json(path, data)
                return 0

            def current_source():
                self.assertFalse(live[0])
                self.assertEqual(
                    set(fixture.private_json(root_seen[0], "fixture-ids.json")),
                    {"source-a"},
                )
                if isinstance(changed, BaseException):
                    raise changed
                return changed

            with (
                self.subTest(changed=type(changed).__name__),
                patch.object(ui.shutil, "which", return_value=str(self.binary)),
                patch.object(fixture, "validate_store"),
                patch.object(
                    fixture, "prepare_native_cli", side_effect=self.prepare_native
                ),
                patch.object(ui, "provider", side_effect=provider),
                patch.object(box, "main", side_effect=launch),
                patch.object(fixture, "verify_owned_resources"),
                patch.object(
                    ui, "source_identity", side_effect=current_source
                ) as identity,
            ):
                self.assertEqual(ui.own(self.owner_args(), self.source), 1)
            identity.assert_called_once_with()
            report = json.loads(
                next(root_seen[0].glob("interactive-start-*-report.json")).read_text()
            )
            self.assertEqual(
                (report["outcome"], report["category"]), ("failed", "source-changed")
            )
            self.assertEqual(report["source"], self.source)
            self.assertEqual(
                fixture.private_json(root_seen[0], "interactive.json")["source"],
                self.source,
            )
            self.assertEqual(report["manual_observations"], "NOT_AUTOMATICALLY_PASSED")

    def test_client_source_change_after_lease_drain_refuses_success_without_relabeling(
        self,
    ):
        self.record()
        self.client_boundaries()
        lease_live = [False]

        @contextlib.contextmanager
        def lease(*args):
            lease_live[0] = True
            try:
                yield {
                    "url": "http://127.0.0.1:12345",
                    "password": "synthetic-control-not-for-metadata",
                    "session": "ses_shared",
                }
            finally:
                lease_live[0] = False

        def changed_source():
            self.assertFalse(lease_live[0])
            raise fixture.FixtureError("clean-source-required")

        with (
            patch.object(fixture.workspace_service, "attach", side_effect=lease),
            patch.object(box, "run_tui", return_value=0),
            patch.object(ui, "source_identity", side_effect=changed_source),
            patch.object(box, "main") as launch,
        ):
            self.assertEqual(ui.attach(self.args(), self.source), 1)
        launch.assert_not_called()
        report = self.reports("tui")[0]
        self.assertEqual(
            (report["outcome"], report["category"]), ("failed", "source-changed")
        )
        self.assertEqual(report["source"], self.source)


if __name__ == "__main__":
    unittest.main()
