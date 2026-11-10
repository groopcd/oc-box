import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oc_box import oc_box as box, tui_backend as backend


class HostSamplerTests(unittest.TestCase):
    def test_cpu_deltas_guest_accounting_memory_and_counter_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "stat").write_text("cpu 10 0 0 90 0 0 0 0 10 0\n")
            (root / "meminfo").write_text(
                "MemTotal: 100 kB\nMemAvailable: 40 kB\nSwapTotal: 20 kB\nSwapFree: 15 kB\n"
            )
            sampler = backend.HostSampler(root)
            self.assertIsNone(sampler.sample()["cpu_percent"])
            (root / "stat").write_text("cpu 20 0 0 100 0 0 0 0 20 0\n")
            sample = sampler.sample()
            self.assertEqual(sample["cpu_percent"], 50)
            self.assertEqual(sample["memory_bytes"], 60 * 1024)
            self.assertEqual(sample["swap_bytes"], 5 * 1024)
            (root / "stat").write_text("cpu 1 0 0 1\n")
            self.assertIsNone(sampler.sample()["cpu_percent"])

    def test_missing_and_invalid_counters_are_unknown_not_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            sampler = backend.HostSampler(Path(temporary))
            sample = sampler.sample()
            self.assertIsNone(sample["cpu_percent"])
            self.assertIsNone(sample["memory_bytes"])
            self.assertIsNone(sample["swap_bytes"])

    def test_literal_metadata_sanitization(self):
        self.assertEqual(backend.plain("a\x1b[31m\nb"), "a [31m b")


class DashboardBackendTests(unittest.TestCase):
    def setUp(self):
        self.box = SimpleNamespace(
            BoxError=box.BoxError,
            maintenance_host_env=Mock(
                return_value={"PATH": "/bin", "HOME": "/home/demo"}
            ),
            workspace_paths=Mock(return_value=[]),
            read_json=Mock(),
            run=Mock(),
        )
        self.backend = backend.DashboardBackend(self.box)

    def test_construction_and_empty_inventory_never_discover_or_start_services(self):
        self.assertEqual(self.backend.inventory(), [])
        self.box.run.assert_not_called()

    def test_real_json_wrapper_failure_is_isolated_from_valid_registrations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bad, good = root / "bad.json", root / "good.json"
            bad.write_text("{invalid PRIVATE_VALUE")
            data = {
                "id": "a" * 32,
                "project": "/project",
                "distro": "debian",
                "container": "oc-box-fixture",
                "home_volume": "home-fixture",
                "image": "debian:stable",
                "home": "/home/demo",
            }
            good.write_text(json.dumps(data))
            self.box.workspace_paths.return_value = [bad, root / "removed.json", good]
            self.box.read_json = box.read_json
            self.assertEqual(self.backend.inventory(), [(good, data)])
            self.assertEqual(self.backend.inventory_skipped, 2)
            self.assertEqual(bad.read_text(), "{invalid PRIVATE_VALUE")
            self.box.run.assert_not_called()

    def test_explicit_host_api_has_clean_env_and_204_semantics(self):
        self.box.run.return_value = subprocess.CompletedProcess([], 0, "", "")
        self.assertIsNone(
            self.backend.host_request(
                "PATCH", "/api/session/ses_demo", {"archived": True}
            )
        )
        call = self.box.run.call_args
        self.assertEqual(call.args[0][:3], ["opencode", "api", "patch"])
        self.assertEqual(
            call.kwargs["env"],
            {"PATH": "/bin", "HOME": "/home/demo", "OPENCODE_DISABLE_AUTOUPDATE": "1"},
        )
        self.assertTrue(call.kwargs["private"])

    def test_private_host_errors_are_not_presented(self):
        self.box.run.side_effect = box.BoxError("PRIVATE_TOKEN=never-display-this")
        with self.assertRaises(backend.DashboardError) as caught:
            self.backend.host_request("GET", "/api/info")
        self.assertNotIn("never-display", str(caught.exception))

    def test_host_connection_pins_process_and_refuses_after_disconnect(self):
        info = {"version": "2.2.6", "pid": 42}
        self.box.run.side_effect = lambda command, **kwargs: (
            subprocess.CompletedProcess(
                [],
                0,
                "/home/demo/.local/share/opencode/opencode.db\n"
                if command[1:3] == ["debug", "paths"]
                else json.dumps(info),
                "",
            )
        )
        connection = self.backend.session_connection()
        self.assertEqual(connection.origin, "host")
        self.assertEqual(
            connection.ownership("host", connection.store, ("ses_demo",)),
            "process-local",
        )
        info["pid"] = 43
        self.assertIsNone(connection.ownership("host", connection.store, ("ses_demo",)))
        connection.close()
        connection.close()
        with self.assertRaises(backend.DashboardError):
            connection.request("GET", "/api/info")

    def test_workspace_browsing_holds_and_releases_only_explicit_client_lease(self):
        data = {"id": "a" * 32, "opencode_version": "2.2.6"}
        lease = Mock()
        lease.__enter__ = Mock(return_value={"password": "never-persist-this"})
        lease.__exit__ = Mock(return_value=False)
        self.backend.selected = Mock(
            return_value=(Path("/private/workspace.json"), data)
        )
        with (
            patch.object(backend.workspace_service, "attach", return_value=lease),
            patch.object(
                backend.workspace_service,
                "api",
                return_value={"version": "2.2.6", "pid": 42},
            ),
        ):
            connection = self.backend.session_connection(data["id"])
            lease.__exit__.assert_not_called()
            self.assertNotIn("never-persist", repr(connection))
            connection.close()
            connection.close()
        lease.__exit__.assert_called_once()

    def test_foreign_terminal_session_refuses_and_releases_lease(self):
        data = {"id": "a" * 32, "opencode_version": "2.2.6"}
        lease = Mock()
        lease.__enter__ = Mock(return_value={"session": "ses_demo"})
        lease.__exit__ = Mock(return_value=False)
        self.box.valid_session_id = box.valid_session_id
        self.box.container_project = Mock(return_value="/project")
        self.backend.selected = Mock(
            return_value=(Path("/private/workspace.json"), data)
        )
        with (
            patch.object(backend.workspace_service, "attach", return_value=lease),
            patch.object(
                backend.workspace_service,
                "api",
                return_value={
                    "data": {"id": "ses_foreign", "location": {"directory": "/other"}}
                },
            ),
            patch.object(backend.workspace_service, "client_command") as client,
        ):
            with self.assertRaises(backend.DashboardError):
                self.backend.terminal(data["id"], "ses_foreign")
            client.assert_not_called()
        lease.__exit__.assert_called_once()

    def test_disconnect_does_not_block_ui_or_release_lease_during_an_inflight_query(
        self,
    ):
        data = {"id": "a" * 32, "opencode_version": "2.2.6"}
        lease = Mock()
        lease.__enter__ = Mock(return_value={})
        lease.__exit__ = Mock(return_value=False)
        self.backend.selected = Mock(
            return_value=(Path("/private/workspace.json"), data)
        )
        entered, finish, closed = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        def request(*args, **kwargs):
            if args[4] == "/held":
                entered.set()
                finish.wait(5)
                return {"data": []}
            return {"version": "2.2.6", "pid": 42}

        with (
            patch.object(backend.workspace_service, "attach", return_value=lease),
            patch.object(backend.workspace_service, "api", side_effect=request),
        ):
            connection = self.backend.session_connection(data["id"])
            worker = threading.Thread(target=lambda: connection.request("GET", "/held"))
            closer = threading.Thread(target=lambda: (connection.close(), closed.set()))
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                closer.start()
                self.assertTrue(closed.wait(2))
                lease.__exit__.assert_not_called()
            finally:
                finish.set()
                worker.join(5)
                if closer.ident is not None:
                    closer.join(5)
            self.assertFalse(worker.is_alive())
            lease.__exit__.assert_called_once()

    def test_archive_project_mapping_requires_private_saved_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            history = root / "history"
            history.mkdir(mode=0o700)
            workspace = "a" * 32
            data = {"id": workspace, "project": "/project"}
            archive = (
                "ses_"
                + hashlib.sha256((workspace + "-v2").encode()).hexdigest()[:12]
                + "_demo"
            )
            state = root / "sessions-v2.json"
            state.write_text(json.dumps({"host_sessions": {"ses_demo": archive}}))
            state.chmod(0o600)
            saved = history / (archive + ".json")
            saved.write_text(
                json.dumps(
                    {
                        "info": {"id": archive, "location": {"directory": "/project"}},
                        "messages": [{"text": "PRIVATE_TRANSCRIPT"}],
                    }
                )
            )
            saved.chmod(0o600)
            self.backend.inventory = Mock(
                return_value=[(root / "workspace.json", data)]
            )
            self.box.valid_session_id = box.valid_session_id
            relationships = self.backend.associations("host-fixture")
            self.assertEqual(len(relationships), 1)
            self.assertEqual(relationships[0]["archive"]["id"], archive)
            self.assertEqual(
                relationships[0]["native_directory"], str(root / "controller")
            )
            self.assertNotIn("PRIVATE_TRANSCRIPT", repr(relationships))
            saved.chmod(0o644)
            self.assertEqual(self.backend.associations("host-fixture"), [])

    def test_terminal_uses_existing_native_helper_with_remote_tty_and_private_control_env(
        self,
    ):
        data = {
            "id": "a" * 32,
            "opencode_version": "2.2.6",
            "container": "oc-box-fixture",
            "home": "/home/demo",
        }
        lease = Mock()
        lease.__enter__ = Mock(
            return_value={
                "session": "ses_demo",
                "url": "http://127.0.0.1:1234",
                "password": "synthetic-private-control",
            }
        )
        lease.__exit__ = Mock(return_value=False)
        self.box.valid_session_id = box.valid_session_id
        self.box.container_project = Mock(return_value="/project")
        self.box.PACKAGE = box.PACKAGE
        self.box.ContainerCommand = box.ContainerCommand
        self.backend.selected = Mock(
            return_value=(Path("/private/workspace.json"), data)
        )
        with (
            patch.object(backend.workspace_service, "attach", return_value=lease),
            patch.object(
                backend.workspace_service,
                "api",
                return_value={
                    "data": {"id": "ses_demo", "location": {"directory": "/project"}}
                },
            ),
        ):
            handle = self.backend.terminal(data["id"])
        self.assertEqual(handle.command[:4], ["podman", "exec", "-i", "-t"])
        self.assertNotIn("synthetic-private-control", " ".join(handle.command))
        self.assertNotIn("synthetic-private-control", repr(handle))
        self.assertEqual(
            handle.environment["OPENCODE_PASSWORD"], "synthetic-private-control"
        )
        self.assertEqual(handle.session_id, "ses_demo")
        handle.close()
        lease.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()
