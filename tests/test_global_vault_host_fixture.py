"""Offline safety contracts only: never execute the host Podman acceptance run."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import oc_box as box
import backup_crypto as crypto

APP = Path(__file__).resolve().parents[1]
SCRIPT = APP / "tools/test_global_vault_podman.py"
spec = importlib.util.spec_from_file_location("global_vault_host_fixture", SCRIPT)
assert spec is not None and spec.loader is not None
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class GlobalVaultHostFixtureTests(unittest.TestCase):
    def setUp(self):
        preferred = Path("/tmp/opencode")
        temporary = tempfile.TemporaryDirectory(
            prefix="oc-box-gv-fixture-unit-",
            dir=preferred if preferred.is_dir() else None,
        )
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / (fixture.PREFIX + "unit")
        self.root.mkdir(mode=0o700)
        self.runtime = self.parent / "host-runtime"
        self.runtime.mkdir(mode=0o700)
        environment = patch.dict(
            os.environ,
            {
                "PATH": "/usr/bin:/bin",
                "HOME": str(self.root / "host-home"),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_DATA_HOME": str(self.root / "data"),
                "XDG_STATE_HOME": str(self.root / "state"),
                "XDG_RUNTIME_DIR": str(self.runtime),
                "TMPDIR": str(self.root / "temporary"),
                "OC_BOX_STATE_HOME": str(self.root / "registry"),
                "OC_BOX_CONTAINER": "0",
            },
            clear=True,
        )
        environment.start()
        self.addCleanup(environment.stop)
        for relative in (
            "host-home",
            "registry/workspaces",
            "data/containers/storage/volumes",
            "config/containers",
            "podman-run",
            "temporary",
        ):
            (self.root / relative).mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.root / "config/containers/storage.conf").write_text(
            fixture.expected_storage(self.root)
        )

    def info(self):
        return {
            "host": {"security": {"rootless": True}, "serviceIsRemote": False},
            "store": {
                "graphDriverName": "overlay",
                "graphRoot": str(self.root / "data/containers/storage"),
                "runRoot": str(self.root / "podman-run"),
                "volumePath": str(self.root / "data/containers/storage/volumes"),
                "imageCopyTmpDir": str(self.root / "temporary"),
                "configFile": str(self.root / "config/containers/storage.conf"),
            },
        }

    def record(self, name, ident, *, origin=None):
        project = self.root / name
        project.mkdir(mode=0o700)
        with patch.object(box.uuid, "uuid4", return_value=SimpleNamespace(hex=ident)):
            path, data = box.create_workspace(project, "tumbleweed")
        data["opencode_version"] = "2.2.6"
        if origin is not None:
            data["container_project"] = str(self.root / "source-a")
            data["restored_from"] = {"kind": "full", "workspace_id": origin}
            data["credentials"] = {"mode": "vault", "env": [], "vault": []}
        box.atomic_json(path, data)
        return path, data

    def complete_fixture_metadata(self):
        for relative in ("bin", "podman-tmp", "data/libpod", "state", "cache"):
            (self.root / relative).mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.root / "config/containers/containers.conf").write_text(
            fixture.containers_config(self.root)
        )
        fixture.write_json_new(
            self.root / "fixture.json",
            {
                "format": fixture.FORMAT,
                "version": 1,
                "projects": list(fixture.PROJECTS),
            },
        )
        fixture.write_json_new(self.root / "fixture-ids.json", {})
        # The real Python ELF stands in for native Podman ONLY in this offline
        # selector test; it prints a bounded diagnostic and performs no lifecycle.
        fixture.prepare_podman_cli(self.root, Path(sys.executable).resolve())

    def test_help_is_harmless_and_host_run_refuses_container_before_placement(self):
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.root / "host-home"),
            "OC_BOX_CONTAINER": "1",
            "PYTHONNOUSERSITE": "1",
        }
        help_result = subprocess.run(
            [sys.executable, "-I", "-B", str(SCRIPT), "--help"],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        for option in ("--opencode-v2", "--work-dir", "--temp-dir", "--cleanup"):
            self.assertIn(option, help_result.stdout)
        denied = subprocess.run(
            [sys.executable, "-I", "-B", str(SCRIPT), "--work-dir", str(self.root)],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("host", (denied.stdout + denied.stderr).lower())
        self.assertNotIn("Traceback", denied.stdout + denied.stderr)
        self.assertEqual(list(self.root.glob(fixture.PREFIX + "*")), [])

    def test_runtime_discovery_is_preserved_but_secret_and_engine_overrides_are_not(
        self,
    ):
        baseline = {
            "PATH": "/usr/bin:/bin",
            "USER": "fixture",
            "LOGNAME": "fixture",
            "XDG_RUNTIME_DIR": str(self.runtime),
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + str(self.runtime / "bus"),
            "BRAVE_API_KEY": "synthetic-unrelated-key",
            "CUSTOM_SECRET": "synthetic-custom",
            "CONTAINER_HOST": "ssh://unrelated",
            "DOCKER_HOST": "tcp://unrelated",
            "CONTAINERS_STORAGE_CONF": "/unrelated/storage.conf",
            "CONTAINERS_CONF_OVERRIDE": "/unrelated/override.conf",
            "HTTP_PROXY": "http://unrelated",
            "OPENCODE_DB": "/unrelated/db",
        }
        env = fixture.isolated_env(self.root, baseline)
        self.assertEqual(env["XDG_RUNTIME_DIR"], str(self.runtime))
        self.assertEqual(
            env.get("DBUS_SESSION_BUS_ADDRESS"), baseline["DBUS_SESSION_BUS_ADDRESS"]
        )
        for name in (
            "BRAVE_API_KEY",
            "CUSTOM_SECRET",
            "CONTAINER_HOST",
            "DOCKER_HOST",
            "CONTAINERS_CONF_OVERRIDE",
            "HTTP_PROXY",
            "OPENCODE_DB",
        ):
            self.assertNotIn(name, env)
        self.assertEqual(env["HOME"], str(self.root / "host-home"))
        self.assertEqual(env["XDG_DATA_HOME"], str(self.root / "data"))
        self.assertEqual(env["OC_BOX_STATE_HOME"], str(self.root / "registry"))

    def test_store_info_rejects_remote_rootful_or_escaped_storage_before_resources(
        self,
    ):
        fixture.validate_store_info(self.root, self.info())
        cases = (
            ("host", "serviceIsRemote", True),
            ("store", "graphDriverName", "vfs"),
            ("store", "graphRoot", str(self.parent)),
            ("store", "runRoot", str(self.runtime)),
            ("store", "volumePath", str(self.parent)),
            ("store", "imageCopyTmpDir", str(self.parent)),
            ("store", "configFile", str(self.parent / "other.conf")),
        )
        for group, name, value in cases:
            with self.subTest(name=name):
                info = self.info()
                info[group][name] = value
                with self.assertRaises(fixture.FixtureError):
                    fixture.validate_store_info(self.root, info)
        info = self.info()
        info["host"]["security"]["rootless"] = False
        with self.assertRaises(fixture.FixtureError):
            fixture.validate_store_info(self.root, info)

    def test_symlinked_store_or_configuration_is_not_authorized(self):
        target = self.root / "config/containers/storage.conf"
        target.unlink()
        other = self.parent / "unrelated.conf"
        other.write_text("unrelated fixture data")
        target.symlink_to(other)
        with self.assertRaises(fixture.FixtureError):
            fixture.validate_store_info(self.root, self.info())
        with self.assertRaises(fixture.FixtureError):
            fixture.no_symlinks(target)

    def test_audited_podman_614_info_without_configfile_requires_private_selector(self):
        self.complete_fixture_metadata()
        info = self.info()
        info["version"] = {"Version": "6.1.4"}
        del info["store"]["configFile"]
        fixture.validate_fixture(self.root)
        fixture.validate_store_info(self.root, info)
        for version in (None, "6.1.5", "5.6.2"):
            with self.subTest(version=version):
                unknown = copy.deepcopy(info)
                unknown["version"] = {} if version is None else {"Version": version}
                with self.assertRaises(fixture.FixtureError):
                    fixture.validate_store_info(self.root, unknown)
        poisoned = copy.deepcopy(info)
        poisoned["store"]["configFile"] = str(self.parent / "unrelated.conf")
        with self.assertRaises(fixture.FixtureError):
            fixture.validate_store_info(self.root, poisoned)
        wrapper = self.root / "bin/podman"
        wrapper.write_text("unrecognized fixture selector")
        with self.assertRaises(fixture.FixtureError):
            fixture.validate_fixture(self.root)

    def test_restored_provenance_is_independent_of_uuid_directory_order(self):
        _, source = self.record("source-a", "f" * 32)
        _, restored = self.record("restored-full", "0" * 32, origin=source["id"])
        records = fixture.workspace_records(self.root)
        self.assertEqual(records["source-a"][1]["id"], source["id"])
        self.assertEqual(records["restored-full"][1]["id"], restored["id"])
        path = records["restored-full"][0]
        invalid = copy.deepcopy(restored)
        invalid["restored_from"]["workspace_id"] = "a" * 32
        box.atomic_json(path, invalid)
        with self.assertRaises(fixture.FixtureError):
            fixture.workspace_records(self.root)

    def test_registry_cannot_select_a_real_project_or_selected_credential_policy(self):
        path, data = self.record("source-a", "a" * 32)
        invalid = copy.deepcopy(data)
        invalid["project"] = str(self.parent)
        box.atomic_json(path, invalid)
        with self.assertRaises(fixture.FixtureError):
            fixture.workspace_records(self.root)

        invalid = copy.deepcopy(data)
        invalid["credentials"] = {
            "mode": "selected",
            "env": [],
            "vault": ["EXAMPLE_TOKEN"],
        }
        box.atomic_json(path, invalid)
        with self.assertRaises(fixture.FixtureError):
            fixture.workspace_records(self.root)

    def test_selected_session_uses_client_and_state_not_the_live_endpoint(self):
        endpoint = {
            "url": "http://127.0.0.1:1",
            "password": "synthetic",
            "workspace": "a" * 32,
        }
        self.assertNotIn("session", endpoint)
        path = self.root / "registry/workspaces" / ("a" * 32) / "workspace.json"
        command = ["opencode", "--server", endpoint["url"], "--session", "ses_fixture"]
        with patch.object(box, "session_state", return_value={"last": "ses_fixture"}):
            self.assertEqual(
                fixture.selected_session(command, path, "ses_fixture"), "ses_fixture"
            )
            with self.assertRaises(fixture.FixtureError):
                fixture.selected_session(command, path, "ses_wrong")
            with self.assertRaises(fixture.FixtureError):
                fixture.selected_session(["opencode"], path)

    def test_private_engine_selectors_survive_sanitized_production_environments(self):
        self.complete_fixture_metadata()
        baseline = {
            "PATH": "/usr/bin:/bin",
            "USER": "fixture",
            "LOGNAME": "fixture",
            "XDG_RUNTIME_DIR": str(self.runtime),
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + str(self.runtime / "bus"),
        }
        normal = fixture.isolated_env(self.root, baseline)
        program = "import json,os; print(json.dumps({key:os.environ.get(key) for key in ('CONTAINERS_STORAGE_CONF','CONTAINERS_CONF','CONTAINERS_CONF_OVERRIDE','HOME','XDG_RUNTIME_DIR','TMPDIR')}))"
        with patch.dict(os.environ, normal, clear=True):
            environments = (normal, box.maintenance_host_env(), crypto.environment())
        self.assertNotIn("CONTAINERS_CONF", environments[1])
        self.assertNotIn("CONTAINERS_CONF", environments[2])
        for index, env in enumerate(environments):
            with self.subTest(environment=index):
                result = subprocess.run(
                    [str(self.root / "bin/podman"), "-I", "-c", program],
                    env={**env, "CONTAINERS_CONF_OVERRIDE": "/unrelated/config"},
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual(
                    data["CONTAINERS_STORAGE_CONF"],
                    str(self.root / "config/containers/storage.conf"),
                )
                self.assertEqual(
                    data["CONTAINERS_CONF"],
                    str(self.root / "config/containers/containers.conf"),
                )
                self.assertIsNone(data["CONTAINERS_CONF_OVERRIDE"])
                self.assertEqual(data["XDG_RUNTIME_DIR"], str(self.runtime))
                self.assertEqual(data["HOME"], str(self.root / "host-home"))
                self.assertEqual(data["TMPDIR"], str(self.root / "temporary"))

    def test_store_probe_covers_every_environment_and_preserves_temporary_placement(
        self,
    ):
        self.complete_fixture_metadata()
        selected_tmp = self.parent / "selected-temporary"
        selected_tmp.mkdir(mode=0o700)
        info = self.info()
        info["store"]["imageCopyTmpDir"] = str(selected_tmp)
        with (
            patch.dict(os.environ, {"TMPDIR": str(selected_tmp)}),
            patch.object(
                box,
                "run",
                return_value=subprocess.CompletedProcess([], 0, json.dumps(info), ""),
            ) as run,
        ):
            fixture.validate_store(self.root)
        self.assertEqual(run.call_count, 3)
        for call in run.call_args_list:
            self.assertEqual(call.args[0], ["podman", "info", "--format", "json"])
            self.assertTrue(call.kwargs["private"])
            self.assertEqual(call.kwargs["env"]["TMPDIR"], str(selected_tmp))
            self.assertEqual(call.kwargs["env"]["XDG_RUNTIME_DIR"], str(self.runtime))

    def test_unrecorded_fixture_journal_requires_explicit_cleanup_validation(self):
        self.complete_fixture_metadata()
        _, source = self.record("source-a", "a" * 32)
        with self.assertRaisesRegex(fixture.FixtureError, "unrecorded-journal"):
            fixture.validate_fixture(self.root)
        self.assertEqual(
            fixture.validate_fixture(self.root, allow_unrecorded=True), self.root
        )
        with (
            patch.object(fixture, "host_guard"),
            patch.object(fixture, "validate_store"),
            patch.object(box, "resource_ids", return_value=[]),
            patch.object(box, "podman") as podman,
        ):
            ids = fixture.cleanup(self.root)
        self.assertEqual(ids, {"source-a": source["id"]})
        self.assertEqual(fixture.private_json(self.root, "fixture-ids.json"), ids)
        podman.assert_not_called()

    def test_cleanup_validates_recorded_interpreter_not_current_python(self):
        self.complete_fixture_metadata()
        _, source = self.record("source-a", "a" * 32)
        fixture.record_ids(self.root)
        original = (self.root / "bin/podman").read_bytes()
        mapping = fixture.private_json(self.root, "podman-executable.json")
        self.assertEqual(mapping["interpreter"], str(Path(sys.executable).resolve()))
        with (
            patch.object(fixture.sys, "executable", "/unrelated/different-python"),
            patch.object(fixture, "host_guard"),
            patch.object(fixture, "validate_store"),
            patch.object(box, "resource_ids", return_value=[]),
            patch.object(box, "podman") as podman,
        ):
            self.assertEqual(fixture.validate_fixture(self.root), self.root)
            self.assertEqual(fixture.cleanup(self.root), {"source-a": source["id"]})
        podman.assert_not_called()
        self.assertEqual((self.root / "bin/podman").read_bytes(), original)

    def test_cleanup_validates_all_labels_before_any_removal(self):
        self.complete_fixture_metadata()
        _, source = self.record("source-a", "a" * 32)
        _, second = self.record("source-b", "b" * 32)
        records = {source["id"]: source, second["id"]: second}
        fixture.record_ids(self.root)
        operations = []

        def resource_ids(kind, ident):
            return [kind + "-" + ident] if kind in ("container", "volume") else []

        def podman(kind, action, ident, **kwargs):
            operations.append((kind, action, ident))
            self.assertEqual(
                action, "inspect", "Removal happened before validation completed"
            )
            data = records[ident.split("-", 1)[1]]
            if kind == "container":
                value = {
                    "Name": data["container"],
                    "Config": {"Labels": {box.LABEL: data["id"]}},
                    "Mounts": [
                        {
                            "Type": "bind",
                            "Source": data["project"],
                            "Destination": box.container_project(data),
                        },
                        {
                            "Type": "volume",
                            "Source": str(
                                self.root
                                / "data/containers/storage/volumes"
                                / data["home_volume"]
                                / "_data"
                            ),
                            "Destination": data["home"],
                        },
                    ],
                }
            else:
                value = {
                    "Name": data["home_volume"],
                    "Labels": {
                        box.LABEL: "wrong-owner"
                        if data["id"] == second["id"]
                        else data["id"]
                    },
                    "Mountpoint": str(
                        self.root
                        / "data/containers/storage/volumes"
                        / data["home_volume"]
                        / "_data"
                    ),
                }
            return subprocess.CompletedProcess([], 0, json.dumps([value]), "")

        with (
            patch.object(fixture, "host_guard"),
            patch.object(fixture, "validate_store"),
            patch.object(box, "resource_ids", side_effect=resource_ids),
            patch.object(box, "podman", side_effect=podman),
            self.assertRaisesRegex(fixture.FixtureError, "cleanup-allowlist"),
        ):
            fixture.cleanup(self.root)
        self.assertEqual(len(operations), 4)
        self.assertTrue(all(operation[1] == "inspect" for operation in operations))


if __name__ == "__main__":
    unittest.main()
