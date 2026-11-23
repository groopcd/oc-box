"""Private native host-port selector contracts; no native service or Podman run."""

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from oc_box import tui
from tools import test_interactive_podman as ui

fixture, box = ui.fixture, ui.box


class InteractiveHostServicePortTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="interactive-port-unit-")
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / (fixture.PREFIX + "interactive-port-unit")
        self.root.mkdir(mode=0o700)
        self.runtime = self.parent / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.enter(
            patch.dict(
                os.environ,
                {
                    "PATH": os.defpath,
                    "HOME": str(self.root / "host-home"),
                    "XDG_RUNTIME_DIR": str(self.runtime),
                    "OC_BOX_CONTAINER": "0",
                    "PERSONAL_GITHUB_TOKEN": "synthetic-unrelated-private-token",
                    "OPENCODE_AUTH_CONTENT": "synthetic-unrelated-private-auth",
                },
                clear=True,
            )
        )
        old_umask = os.umask(0o077)
        self.addCleanup(os.umask, old_umask)
        self.run = self.enter(
            patch.object(
                box, "run", side_effect=AssertionError("unexpected host command")
            )
        )
        self.source = {
            "checkout": "/synthetic/checkout",
            "version": "1.6.43+gaaaaaaa",
            "commit": "a" * 40,
            "commit_count": 72,
            "dirty": False,
        }
        self.binary = Path(sys.executable).resolve()
        self.port = 40003
        self.boot = b"00000000-0000-0000-0000-000000000001\n"
        self.enter(patch.object(fixture, "system_boot_id", return_value=self.boot))
        fixture.prepare_root(self.root, "tumbleweed")
        fixture.prepare_podman_cli(self.root, self.binary)  # file-only ELF mapping
        for name in ("config", "data", "state", "cache"):
            (self.root / ("host-" + name + "-2")).mkdir(mode=0o700)
        fixture.write_json_new(self.root / "executables.json", {"2": str(self.binary)})
        self.wrapper = self.root / "bin/opencode"
        self.wrapper.write_text(fixture.native_wrapper_source())
        self.wrapper.chmod(0o700)
        ui.write_metadata(self.root, self.source, self.binary)
        fixture.write_json_new(
            self.root / "config/opencode/opencode.json",
            fixture.fixture_config(12345, self.root),
        )
        fixture.write_plugin(self.root)
        self.env = fixture.isolated_env(self.root)
        with patch.dict(os.environ, self.env, clear=True):
            path, data = box.create_workspace(self.root / "source-a", "tumbleweed")
            data.update(opencode_version=fixture.NATIVE_VERSION, ready=True)
            box.atomic_json(path, data)
        (self.root / "podman-tmp/alive").write_bytes(self.boot)
        self.reservation = MagicMock()
        self.reservation.__enter__.return_value = self.reservation
        self.reservation.getsockname.return_value = ("127.0.0.1", self.port)
        self.socket = self.enter(
            patch.object(ui.socket, "socket", return_value=self.reservation)
        )
        self.output = io.StringIO()
        self.enter(contextlib.redirect_stdout(self.output))
        self.enter(contextlib.redirect_stderr(self.output))

    def enter(self, manager):
        result = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        return result

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if path.exists():
            path.write_text(json.dumps(value))
        else:
            fixture.write_json_new(path, value)

    def native_setter(self, command, **kwargs):
        self.assertEqual(
            command, [str(self.wrapper), "service", "set", "port", str(self.port)]
        )
        self.assertEqual(self.reservation.__exit__.call_count, 0)
        self.write(ui.HOST_SERVICE_CONFIG, {"port": self.port})
        return subprocess.CompletedProcess(
            command, 0, "synthetic-private-output", "synthetic-private-errors"
        )

    def prepare(self):
        self.run.side_effect = self.native_setter
        with patch.dict(os.environ, self.env, clear=True):
            return ui.prepare_host_service_port(self.root, self.source, self.binary)

    def validate(self):
        return ui.validate_host_service_port(self.root, self.source, self.binary)

    def test_ephemeral_loopback_reservation_cloexec_and_exact_private_setter(self):
        selected_tmp = self.root / "selected-temporary"
        selected_tmp.mkdir(mode=0o700)
        self.env["TMPDIR"] = str(selected_tmp)
        self.assertEqual(self.prepare(), self.port)
        self.socket.assert_called_once_with(ui.socket.AF_INET, ui.socket.SOCK_STREAM)
        self.reservation.set_inheritable.assert_called_once_with(False)
        self.reservation.bind.assert_called_once_with(("127.0.0.1", 0))
        self.reservation.__exit__.assert_called_once()
        self.run.assert_called_once()
        options = self.run.call_args.kwargs
        self.assertEqual(
            options,
            {
                "env": {
                    name: self.env[name]
                    for name in (
                        "HOME",
                        "PATH",
                        "XDG_RUNTIME_DIR",
                        "XDG_CONFIG_HOME",
                        "XDG_DATA_HOME",
                        "XDG_STATE_HOME",
                        "XDG_CACHE_HOME",
                        "TMPDIR",
                    )
                },
                "cwd": self.root / "source-a",
                "stdin": subprocess.DEVNULL,
                "capture": True,
                "private": True,
                "timeout": 15,
            },
        )
        marker = self.root / ui.HOST_SERVICE_MARKER
        info = marker.stat()
        self.assertEqual(
            (stat.S_IMODE(info.st_mode), info.st_nlink, info.st_uid),
            (0o600, 1, os.getuid()),
        )
        metadata = json.loads(marker.read_text())
        self.assertEqual(metadata["source"], self.source)
        self.assertEqual(metadata["hostname"], "127.0.0.1")
        self.assertEqual(metadata["native_sha256"], ui.file_hash(self.binary))
        self.assertEqual(metadata["wrapper_sha256"], ui.file_hash(self.wrapper))
        self.assertFalse((self.root / ui.HOST_SERVICE_REGISTRATION).exists())
        self.assertNotIn(
            "synthetic-private", marker.read_text() + self.output.getvalue()
        )

    def test_preexisting_config_registration_or_marker_refuses_before_socket_or_setter(
        self,
    ):
        for relative in (
            ui.HOST_SERVICE_CONFIG,
            ui.HOST_SERVICE_REGISTRATION,
            ui.HOST_SERVICE_MARKER,
        ):
            with self.subTest(relative=relative):
                self.write(relative, {"synthetic-private-existing": True})
                before = (self.root / relative).read_bytes()
                with (
                    patch.dict(os.environ, self.env, clear=True),
                    self.assertRaisesRegex(
                        fixture.FixtureError, "host-service-existing"
                    ),
                ):
                    ui.prepare_host_service_port(self.root, self.source, self.binary)
                self.assertEqual((self.root / relative).read_bytes(), before)
                (self.root / relative).unlink()
        self.socket.assert_not_called()
        self.run.assert_not_called()

    def test_bad_wrapper_or_native_selector_refuses_before_allocation_and_setter(self):
        original = self.wrapper.read_bytes()
        for mode, content in ((0o644, original), (0o700, b"#!/bin/sh\nexit 0\n")):
            with self.subTest(mode=oct(mode)):
                self.wrapper.write_bytes(content)
                self.wrapper.chmod(mode)
                with (
                    patch.dict(os.environ, self.env, clear=True),
                    self.assertRaisesRegex(
                        fixture.FixtureError, "host-service-selector"
                    ),
                ):
                    ui.prepare_host_service_port(self.root, self.source, self.binary)
        self.wrapper.write_bytes(original)
        self.wrapper.chmod(0o700)
        self.write("executables.json", {"2": "/synthetic/different-native"})
        with (
            patch.dict(os.environ, self.env, clear=True),
            self.assertRaisesRegex(fixture.FixtureError, "host-service-selector"),
        ):
            ui.prepare_host_service_port(self.root, self.source, self.binary)
        self.socket.assert_not_called()
        self.run.assert_not_called()

    def test_unisolated_environment_refuses_before_allocation_or_setter(self):
        with self.assertRaisesRegex(fixture.FixtureError, "host-service-environment"):
            ui.prepare_host_service_port(self.root, self.source, self.binary)
        self.socket.assert_not_called()
        self.run.assert_not_called()

    def test_invalid_allocated_port_or_nonloopback_address_never_runs_setter(self):
        for address, port in [
            ("127.0.0.1", p) for p in (0, -1, 1, 1023, 49374, 65536, True, 40003.0)
        ] + [("0.0.0.0", self.port)]:
            with self.subTest(address=address, port=port):
                self.reservation.getsockname.return_value = (address, port)
                with (
                    patch.dict(os.environ, self.env, clear=True),
                    self.assertRaisesRegex(fixture.FixtureError, "host-service-port"),
                ):
                    ui.prepare_host_service_port(self.root, self.source, self.binary)
        self.run.assert_not_called()
        self.assertFalse((self.root / ui.HOST_SERVICE_MARKER).exists())

    def test_native_failure_is_fixed_private_and_single_attempt_without_recovery(self):
        for error in (
            box.BoxError("synthetic-private-token"),
            OSError("synthetic-private-error"),
            subprocess.CalledProcessError(
                1, ["synthetic-private-argv"], stderr="synthetic-private-stderr"
            ),
            subprocess.TimeoutExpired(["synthetic-private-argv"], 15),
        ):
            self.run.reset_mock(side_effect=True)
            self.run.side_effect = error
            self.socket.reset_mock()
            with (
                self.subTest(error=type(error).__name__),
                patch.dict(os.environ, self.env, clear=True),
            ):
                with self.assertRaisesRegex(
                    fixture.FixtureError, "^host-service-setter$"
                ) as caught:
                    ui.prepare_host_service_port(self.root, self.source, self.binary)
                self.assertEqual(
                    ui.error_outcome(caught.exception),
                    ("failed", "host-service-setter", 1),
                )
            self.run.assert_called_once()
            self.socket.assert_called_once()
            self.assertFalse((self.root / ui.HOST_SERVICE_MARKER).exists())
        self.assertNotIn("synthetic-private", self.output.getvalue())

    def test_marker_creation_is_exclusive_if_another_file_appears_during_setter(self):
        def race(command, **kwargs):
            result = self.native_setter(command, **kwargs)
            fixture.write_json_new(
                self.root / ui.HOST_SERVICE_MARKER,
                {"retained": "synthetic-private-existing"},
            )
            return result

        self.run.side_effect = race
        with (
            patch.dict(os.environ, self.env, clear=True),
            self.assertRaises(FileExistsError),
        ):
            ui.prepare_host_service_port(self.root, self.source, self.binary)
        self.assertEqual(
            json.loads((self.root / ui.HOST_SERVICE_MARKER).read_text()),
            {"retained": "synthetic-private-existing"},
        )
        self.run.assert_called_once()
        self.reservation.__exit__.assert_called_once()

    def test_missing_marker_and_invalid_ports_fail_file_only_without_migration(self):
        self.prepare()
        marker = self.root / ui.HOST_SERVICE_MARKER
        original = json.loads(marker.read_text())
        marker.unlink()
        with self.assertRaises((fixture.FixtureError, FileNotFoundError)):
            self.validate()
        for port in (0, -1, 1023, 49374, 65536, True, float(self.port), str(self.port)):
            self.write(ui.HOST_SERVICE_MARKER, {**original, "port": port})
            with (
                self.subTest(port=port),
                self.assertRaisesRegex(fixture.FixtureError, "host-service-port"),
            ):
                self.validate()
        self.run.assert_called_once()

    def test_marker_binds_source_native_wrapper_version_and_private_profile(self):
        self.prepare()
        original = json.loads((self.root / ui.HOST_SERVICE_MARKER).read_text())
        for key, value in (
            ("source", {**self.source, "commit": "b" * 40}),
            ("native_sha256", "0" * 64),
            ("wrapper_sha256", "0" * 64),
            ("native_version", "1.19.5"),
            ("hostname", "0.0.0.0"),
            ("profile", {"config": "/synthetic/foreign"}),
            ("version", 2),
            ("version", True),
        ):
            self.write(ui.HOST_SERVICE_MARKER, {**original, key: value})
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(fixture.FixtureError, "host-service-marker"),
            ):
                self.validate()
        self.run.assert_called_once()

    def test_opaque_native_auth_addition_is_accepted_without_rewrite_or_output(self):
        self.prepare()
        opaque = "SYNTHETIC_OPAQUE_NATIVE_AUTH"
        self.write(ui.HOST_SERVICE_CONFIG, {"port": self.port, "password": opaque})
        self.write(
            ui.HOST_SERVICE_REGISTRATION,
            {
                "id": "synthetic-service",
                "pid": 4242,
                "url": "http://127.0.0.1:" + str(self.port),
                "version": fixture.NATIVE_VERSION,
                "password": opaque,
            },
        )
        paths = [
            self.root / name
            for name in (
                ui.HOST_SERVICE_MARKER,
                ui.HOST_SERVICE_CONFIG,
                ui.HOST_SERVICE_REGISTRATION,
            )
        ]
        before = {path: path.read_bytes() for path in paths}
        self.assertEqual(self.validate(), self.port)
        self.assertEqual(
            ui.validate_interactive(self.root, self.source), (self.root, self.binary)
        )
        self.assertEqual({path: path.read_bytes() for path in paths}, before)
        report = ui.report_value("manager", self.source, "returned", "client-return", 0)
        self.assertNotIn(
            opaque,
            json.dumps(report)
            + (self.root / ui.HOST_SERVICE_MARKER).read_text()
            + self.output.getvalue(),
        )
        self.run.assert_called_once()

    def test_native_control_fields_and_malformed_auth_are_refused_without_rewrites(
        self,
    ):
        self.prepare()
        for key, value in (
            ("hostname", "127.0.0.1"),
            ("hostname", "0.0.0.0"),
            ("remote", {"route": "synthetic"}),
            ("remoteAccepted", True),
            ("disabled", False),
            ("cors", []),
            ("env", {}),
            ("unknown", True),
            ("password", ""),
            ("password", False),
            ("password", "x" * 4097),
        ):
            self.write(ui.HOST_SERVICE_CONFIG, {"port": self.port, key: value})
            before = (self.root / ui.HOST_SERVICE_CONFIG).read_bytes()
            with (
                self.subTest(key=key, kind=type(value).__name__),
                self.assertRaisesRegex(fixture.FixtureError, "host-service-config"),
            ):
                self.validate()
            self.assertEqual((self.root / ui.HOST_SERVICE_CONFIG).read_bytes(), before)
        self.run.assert_called_once()

    def test_unsafe_config_permissions_links_owner_size_and_json_are_refused(self):
        self.prepare()
        path = self.root / ui.HOST_SERVICE_CONFIG
        path.chmod(0o644)
        with self.assertRaisesRegex(fixture.FixtureError, "unsafe-metadata"):
            self.validate()
        path.chmod(0o600)
        linked = path.with_name("linked")
        linked.hardlink_to(path)
        with self.assertRaisesRegex(fixture.FixtureError, "unsafe-metadata"):
            self.validate()
        linked.unlink()
        inode = path.stat().st_ino
        fstat = os.fstat

        def foreign(fd):
            info = fstat(fd)
            return (
                SimpleNamespace(
                    st_mode=info.st_mode,
                    st_uid=os.getuid() + 1,
                    st_nlink=info.st_nlink,
                    st_size=info.st_size,
                )
                if info.st_ino == inode
                else info
            )

        with (
            patch.object(os, "fstat", side_effect=foreign),
            self.assertRaisesRegex(fixture.FixtureError, "unsafe-metadata"),
        ):
            self.validate()
        for value in (
            "{" + "x" * ui.HOST_SERVICE_MAX_BYTES,
            "{invalid synthetic-private-json",
        ):
            path.write_text(value)
            with self.assertRaisesRegex(fixture.FixtureError, "unsafe-metadata"):
                self.validate()
        path.rename(linked)
        path.symlink_to(linked)
        with self.assertRaisesRegex(fixture.FixtureError, "unsafe-path"):
            self.validate()
        self.run.assert_called_once()
        self.assertNotIn("synthetic-private", self.output.getvalue())

    def test_registration_endpoint_version_and_pid_mismatch_are_file_only_refusals(
        self,
    ):
        self.prepare()
        record = {
            "id": "synthetic-service",
            "pid": 4242,
            "url": "http://127.0.0.1:" + str(self.port),
            "version": fixture.NATIVE_VERSION,
            "password": "synthetic-private-control",
        }
        for key, value in (
            ("url", "http://127.0.0.1:49374"),
            ("url", "http://0.0.0.0:" + str(self.port)),
            ("version", "1.19.5"),
            ("pid", True),
            ("pid", 1),
        ):
            self.write(ui.HOST_SERVICE_REGISTRATION, {**record, key: value})
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(
                    fixture.FixtureError, "host-service-registration"
                ),
            ):
                self.validate()
        self.run.assert_called_once()

    def test_changed_selector_refuses_before_engine_native_or_dashboard_dispatch(self):
        self.prepare()
        self.write(ui.HOST_SERVICE_CONFIG, {"port": self.port + 1})
        self.run.reset_mock(side_effect=True)
        with (
            patch.object(fixture, "validate_store") as engine,
            patch.object(fixture, "check_native_version") as native,
            patch.object(fixture, "verify_owned_resources") as resources,
            patch.object(tui, "cli") as manager,
        ):
            args = ui.parser().parse_args(["manager", "--fixture", str(self.root)])
            with self.assertRaisesRegex(fixture.FixtureError, "host-service-config"):
                ui.attach(args, self.source)
        for operation in (self.run, engine, native, resources, manager):
            operation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
