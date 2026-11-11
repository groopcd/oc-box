"""Offline safety contracts only: never execute the host Podman acceptance run."""

import copy
import contextlib
import hashlib
import importlib.util
import io
from itertools import product
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oc_box import oc_box as box, backup_crypto as crypto, backups, vault
from tools import test_global_vault_podman as fixture

fixture.load_runtime()  # Explicit trusted library setup; production bootstrap is separate.

APP = Path(__file__).resolve().parents[1]
SCRIPT = APP / "tools/test_global_vault_podman.py"


class GlobalVaultHostFixtureTests(unittest.TestCase):
    def setUp(self):
        preferred = Path("/tmp/opencode")
        temporary = tempfile.TemporaryDirectory(
            prefix="oc-box-gv-fixture-unit-",
            dir=preferred if preferred.is_dir() else None,
        )
        self.addCleanup(temporary.cleanup)
        cached_temporary = tempfile.tempdir
        self.addCleanup(setattr, tempfile, "tempdir", cached_temporary)
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
        self.boot = b"00000000-0000-0000-0000-000000000001\n"
        boot_reader = patch.object(fixture, "system_boot_id", return_value=self.boot)
        boot_reader.start()
        self.addCleanup(boot_reader.stop)
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
        (self.root / "podman-tmp/alive").write_bytes(self.boot)
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

    def retained_source(self):
        self.complete_fixture_metadata()
        path, data = self.record("source-a", "a" * 32)
        data["ready"] = True
        box.atomic_json(path, data)
        fixture.record_ids(self.root)
        baseline = {
            "info": {
                "id": "ses_fixture",
                "location": {"directory": data["project"]},
                "permissions": fixture.ASK_RULES,
            },
            "messages": [
                {
                    "id": "msg_fixture",
                    "type": "user",
                    "text": fixture.SEED_TEXT,
                    "time": {"created": 1},
                }
            ],
        }
        fixture.write_json_new(self.root / "seed-baseline.json", baseline)
        evidence = path.parent / "imports-v2/ses_fixture.json"
        evidence.parent.mkdir(mode=0o700)
        fixture.write_json_new(evidence, baseline)
        box.atomic_json(
            box.session_file(path),
            {
                "sessions": ["ses_fixture"],
                "last": "ses_fixture",
                "imported_from_host": {"ses_fixture": {"backup": str(evidence)}},
            },
        )
        fixture.write_json_new(
            self.root / "executables.json", {"2": str(Path(sys.executable).resolve())}
        )
        for name in ("config", "data", "state", "cache"):
            (self.root / ("host-" + name + "-2")).mkdir(mode=0o700)
        wrapper = self.root / "bin/opencode"
        wrapper.write_text(fixture.native_wrapper_source(sys.executable))
        wrapper.chmod(0o700)
        config = self.root / "config/opencode"
        config.mkdir(mode=0o700)
        fixture.write_json_new(
            config / "opencode.json", fixture.fixture_config(12345, self.root)
        )
        fixture.write_plugin(self.root)
        settings = self.root / "config/oc-box"
        settings.mkdir(mode=0o700)
        fixture.write_json_new(
            settings / "config.json",
            {
                "default_distro": "tumbleweed",
                "pass_env": [fixture.PASS_ENV],
                "auto_update": False,
            },
        )
        original = self.root / "data/oc-box/credentials/vault.json"
        original.parent.mkdir(mode=0o700, parents=True)
        original.write_text('{"opaque":"synthetic encrypted evidence placeholder"}')
        original.chmod(0o600)
        return path, data, baseline

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
        for option in (
            "--opencode-v2",
            "--work-dir",
            "--temp-dir",
            "--cleanup",
            "--diagnose",
        ):
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
            self.assertEqual(
                call.args[0],
                [str(Path(sys.executable).resolve()), "info", "--format", "json"],
            )
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

    def test_diagnostics_inspect_checkpoints_without_unlock_start_or_secret_output(
        self,
    ):
        self.complete_fixture_metadata()
        path, source = self.record("source-a", "a" * 32)
        source["ready"] = False
        box.atomic_json(path, source)
        fixture.record_ids(self.root)
        before = path.read_bytes()
        ledger = (self.root / "fixture-ids.json").read_bytes()
        secret = "synthetic-sensitive-string-not-for-diagnostic-output"
        info = {
            "State": {
                "Status": "exited",
                "ExitCode": 125,
                "Error": "systemd cgroup failure " + secret,
                "Running": False,
            },
            "Config": {"Env": ["EXAMPLE_TOKEN=" + secret]},
        }
        volume = {
            "Name": source["home_volume"],
            "Mountpoint": str(
                self.root
                / "data/containers/storage/volumes"
                / source["home_volume"]
                / "_data"
            ),
        }
        with (
            patch.object(fixture, "host_guard"),
            patch.object(fixture, "validate_store"),
            patch.object(backups, "owned_container", return_value=info),
            patch.object(backups, "owned_volume", return_value=volume),
            patch.object(
                box, "podman", return_value=subprocess.CompletedProcess([], 0, "", "")
            ) as podman,
            patch.object(
                vault,
                "is_present",
                side_effect=AssertionError("No diagnostic vault probe"),
            ),
            patch.object(
                vault, "select_all", side_effect=AssertionError("No diagnostic unlock")
            ),
            patch.object(
                box,
                "start_container",
                side_effect=AssertionError("No diagnostic start"),
            ),
            patch.object(
                box, "exec_command", side_effect=AssertionError("No diagnostic exec")
            ),
        ):
            result = fixture.diagnose(self.root)
        self.assertEqual(result["status"], "diagnostic-only")
        checkpoint = result["workspaces"]["source-a"]
        self.assertFalse(checkpoint["baseline_ready"])
        self.assertEqual(checkpoint["container_exit_code"], 125)
        self.assertEqual(checkpoint["container_error_category"], "cgroup-runtime")
        self.assertTrue(checkpoint["private_home_present"])
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual((self.root / "fixture-ids.json").read_bytes(), ledger)
        self.assertEqual(
            podman.call_args.args, ("volume", "exists", source["home_volume"])
        )

    def test_unknown_state_error_text_never_appears_in_diagnostic_category(self):
        self.assertEqual(
            fixture.state_error_category("private unexpected message"), "unclassified"
        )
        self.assertIsNone(fixture.state_error_category(""))
        self.assertIsNone(fixture.state_error_category(None))

    def test_diagnostic_selected_session_is_bounded_and_never_renders_invalid_data(
        self,
    ):
        self.complete_fixture_metadata()
        path, data = self.record("source-a", "a" * 32)
        fixture.record_ids(self.root)
        for selected in (
            None,
            "ses_fixture",
            "private-sentinel",
            {"private-sentinel": 1},
            "ses_" + "x" * 1000,
        ):
            with self.subTest(
                kind=type(selected).__name__, valid=selected == "ses_fixture"
            ):
                box.atomic_json(box.session_file(path), {"last": selected})
                with (
                    patch.object(fixture, "host_guard"),
                    patch.object(fixture, "validate_store"),
                    patch.object(backups, "owned_container", return_value=None),
                    patch.object(
                        box,
                        "podman",
                        return_value=subprocess.CompletedProcess([], 1, "", ""),
                    ),
                ):
                    result = fixture.diagnose(self.root)["workspaces"]["source-a"]
                self.assertEqual(
                    result["selected_session"],
                    "ses_fixture" if selected == "ses_fixture" else None,
                )
                self.assertEqual(
                    result["selected_session_validity"],
                    "valid"
                    if selected == "ses_fixture"
                    else "absent"
                    if selected is None
                    else "invalid",
                )
                self.assertNotIn("private-sentinel", json.dumps(result))

    def test_scoped_environment_checks_insertion_mutation_and_cache_before_restoration(
        self,
    ):
        baseline = dict(os.environ)
        for phase in ("second-owner", "opt-out", "retry", "restored-owner"):
            for mutation in ("insert", "change", "cache"):
                for failing in (False, True):
                    with self.subTest(phase=phase, mutation=mutation, failing=failing):
                        primary = (
                            box.BoxError("private-primary-sentinel")
                            if failing
                            else None
                        )
                        cache = False
                        with patch.object(
                            fixture.credentials,
                            "has_unlocked_values",
                            side_effect=lambda: cache,
                        ):
                            with self.assertRaises(
                                (box.BoxError, fixture.FixtureError)
                            ) as raised:
                                with fixture.scoped_host_environment(
                                    {fixture.PASS_ENV: "synthetic-control"}
                                ):
                                    if mutation == "insert":
                                        os.environ[fixture.SIDE] = (
                                            "private-value-sentinel"
                                        )
                                    elif mutation == "change":
                                        os.environ[fixture.PASS_ENV] = (
                                            "private-value-sentinel"
                                        )
                                    else:
                                        cache = True
                                    if primary is not None:
                                        raise primary
                            if primary is not None:
                                self.assertIs(raised.exception, primary)
                                self.assertTrue(raised.exception.fixture_secondary)
                            else:
                                self.assertIsInstance(
                                    raised.exception, fixture.FixtureError
                                )
                            self.assertNotIn(
                                "private-value-sentinel", str(raised.exception)
                            )
                        self.assertEqual(dict(os.environ), baseline)

    def test_provider_finalization_attempts_every_step_and_checks_join(self):
        for failure in ("shutdown", "close", "join", "alive"):
            for body_failure in (False, True):
                with self.subTest(failure=failure, body_failure=body_failure):
                    calls = []
                    primary = (
                        fixture.FirstLaunchFailure(
                            {
                                "stage": "selected-session",
                                "error_class": "invalid-data",
                            },
                            True,
                            None,
                        )
                        if body_failure
                        else None
                    )

                    def action(name):
                        calls.append(name)
                        if name == failure:
                            raise OSError("private-finalizer-sentinel")

                    server = SimpleNamespace(
                        shutdown=lambda: action("shutdown"),
                        server_close=lambda: action("close"),
                    )
                    worker = SimpleNamespace(
                        join=lambda **kw: action("join"),
                        is_alive=lambda: failure == "alive",
                    )
                    if primary is None:
                        with self.assertRaises((OSError, fixture.FixtureError)):
                            fixture.finish_provider(server, worker, primary=None)
                    else:
                        fixture.finish_provider(server, worker, primary=primary)
                        self.assertTrue(primary.shutdown_errors)
                        self.assertNotIn(
                            "private-finalizer-sentinel",
                            json.dumps(primary.shutdown_errors),
                        )
                    self.assertEqual(calls, ["shutdown", "close", "join"])

    def test_provider_partial_start_closes_owned_listener_without_joining_or_shutdown(
        self,
    ):
        primary = RuntimeError("private-provider-start")
        server = Mock()
        worker = Mock()
        worker.start.side_effect = primary
        with (
            patch.object(fixture, "ThreadingHTTPServer", return_value=server),
            patch.object(fixture.threading, "Thread", return_value=worker),
            self.assertRaises(RuntimeError) as raised,
        ):
            fixture.make_provider({})
        self.assertIs(raised.exception, primary)
        server.server_close.assert_called_once()
        server.shutdown.assert_not_called()
        worker.join.assert_not_called()

    def test_reconciliation_failure_preserves_first_launch_object_and_diagnostic(self):
        diagnostic = {"stage": "config-reload", "error_class": "invalid-data"}
        primary = fixture.FirstLaunchFailure(diagnostic, True, None)
        with (
            patch.object(fixture, "run", side_effect=primary),
            patch.object(
                fixture, "record_ids", side_effect=OSError("private-journal-sentinel")
            ),
            self.assertRaises(fixture.FirstLaunchFailure) as raised,
        ):
            fixture.run_and_journal(
                self.root, self.root / "native", "tumbleweed", lambda name: None
            )
        self.assertIs(raised.exception, primary)
        self.assertIs(raised.exception.diagnostic, diagnostic)
        self.assertEqual(
            primary.shutdown_errors,
            [{"stage": "journal-reconciliation", "error_class": "os-error"}],
        )

    def test_engine_categories_are_fixed_including_errors_without_old_keywords(self):
        secret = "synthetic-sensitive-engine-message"
        for text, category in (
            (
                'Error: current system boot ID differs from cached boot ID; an unhandled reboot has occurred. Please delete directories "private-a" and "private-b" and re-run Podman',
                "runtime-boot-mismatch",
            ),
            ("Error: EOF", "unexpected-end"),
            ("unexpected end of JSON input", "unexpected-end"),
            ("Error: error parsing TOML", "invalid-data"),
            ("Error: sqlite database is locked", "engine-database"),
            ("Error: cannot clone", "engine-namespace"),
            ("Error: conmon failed", "engine-helper"),
            ("Error: unknown flag", "invalid-command"),
            ("panic: unexpected fault", "engine-panic"),
            ("private unknown problem", "unclassified"),
        ):
            with self.subTest(text=text):
                actual = fixture.stderr_category(text + " " + secret)
                self.assertEqual(actual, category)
                self.assertIn(actual, fixture.STDERR_CATEGORIES)
                self.assertNotIn(secret, actual)
                self.assertEqual(
                    fixture.stderr_category((text + " " + secret).encode()), actual
                )
        self.assertEqual(fixture.stderr_category("a" * 65536 + " EOF"), "unclassified")

    def test_exact_native_probe_includes_classifier_dependencies(self):
        scope = {"__name__": "fixture_offline_compile_only"}
        # The probe deliberately runs main unconditionally under python -c.
        # Execute only its exact imports/classifier, never the native client.
        exec(fixture.NATIVE_PROBE.split("\ndef main():", 1)[0], scope)
        self.assertEqual(scope["stderr_category"]("Error: EOF"), "unexpected-end")

    def test_failed_store_diagnostic_compares_selectors_without_resource_access(self):
        path, _, _ = self.retained_source()
        before = fixture.source_evidence_digests(self.root, path)
        session = box.session_file(path).read_bytes()
        secret = "synthetic-sensitive-store-error"
        native = fixture.private_json(self.root, "podman-executable.json")["podman"]
        wrapper = str(self.root / "bin/podman")
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            self.assertIn(command[0], (wrapper, native))
            self.assertIn(command[1:], (["--version"], ["info", "--format", "json"]))
            self.assertTrue(kwargs["private"])
            self.assertTrue(kwargs["capture"])
            self.assertFalse(kwargs["check"])
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertLessEqual(kwargs["timeout"], 30)
            self.assertEqual(kwargs["env"]["HOME"], str(self.root / "host-home"))
            self.assertNotIn("EXAMPLE_TOKEN", kwargs["env"])
            self.assertNotIn("CONTAINER_HOST", kwargs["env"])
            if command[0] == native:
                for name, relative in (
                    ("CONTAINERS_CONF", "config/containers/containers.conf"),
                    ("CONTAINERS_STORAGE_CONF", "config/containers/storage.conf"),
                ):
                    self.assertEqual(kwargs["env"][name], str(self.root / relative))
            if command == [native, "--version"]:
                return subprocess.CompletedProcess(
                    command, 0, "podman version 6.1.4\n", ""
                )
            return subprocess.CompletedProcess(
                command, 125, secret, "Error: EOF " + secret
            )

        with (
            patch.dict(os.environ, {"EXAMPLE_TOKEN": secret, "CONTAINER_HOST": secret}),
            patch.object(fixture, "host_guard"),
            patch.object(fixture, "validate_store", side_effect=box.BoxError(secret)),
            patch.object(box, "run", side_effect=run),
            patch.object(backups, "owned_container") as container,
            patch.object(backups, "owned_volume") as volume,
            patch.object(box, "podman") as podman,
            patch.object(fixture, "launcher") as launch,
            patch.object(vault, "select_all") as unlock,
        ):
            result = fixture.diagnose(self.root)
        self.assertEqual(result["status"], "diagnostic-failed")
        self.assertEqual(result["failed_stage"], "private-store")
        self.assertEqual(result["workspaces"], {})
        self.assertEqual(len(calls), 6)
        self.assertEqual(result["engine_probes"][1]["version"], "6.1.4")
        self.assertTrue(
            all(
                item.get("stderr_category") == "unexpected-end"
                for index, item in enumerate(result["engine_probes"])
                if index != 1
            )
        )
        for mock in (container, volume, podman, launch, unlock):
            mock.assert_not_called()
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(fixture.source_evidence_digests(self.root, path), before)
        self.assertEqual(box.session_file(path).read_bytes(), session)

    def test_probe_success_does_not_bypass_original_store_refusal(self):
        self.retained_source()
        private = self.info()
        escaped = copy.deepcopy(private)
        escaped["store"]["graphRoot"] = str(self.parent)
        replies = [
            subprocess.CompletedProcess([], 0, "podman version 6.1.4", ""),
            subprocess.CompletedProcess([], 0, "private-version-string", ""),
            subprocess.CompletedProcess([], 0, json.dumps(private), ""),
            subprocess.CompletedProcess([], 0, json.dumps(escaped), ""),
            subprocess.CompletedProcess([], 0, "private-invalid-json", ""),
            subprocess.CompletedProcess([], 0, json.dumps(private), ""),
        ]
        with (
            patch.object(fixture, "host_guard"),
            patch.object(
                fixture, "validate_store", side_effect=box.BoxError("private")
            ),
            patch.object(box, "run", side_effect=replies),
            patch.object(backups, "owned_container") as container,
        ):
            result = fixture.diagnose(self.root)
        container.assert_not_called()
        self.assertEqual(result["status"], "diagnostic-failed")
        probes = result["engine_probes"]
        self.assertIsNone(probes[1]["version"])
        self.assertTrue(probes[2]["private_store_verified"])
        self.assertEqual(probes[3]["validation_category"], "private-store")
        self.assertEqual(probes[4]["error_class"], "invalid-data")
        self.assertNotIn("private-invalid-json", json.dumps(result))

    def test_diagnostic_probes_bound_timeout_and_do_not_echo_exception_text(self):
        self.retained_source()
        secret = "synthetic-sensitive-timeout"
        with patch.object(
            box,
            "run",
            side_effect=subprocess.TimeoutExpired([secret], 30, stderr=secret),
        ) as run:
            probes = fixture.diagnostic_store_probes(self.root)
        self.assertEqual(run.call_count, 6)
        self.assertTrue(all(item["error_class"] == "timeout" for item in probes))
        self.assertNotIn(secret, json.dumps(probes))

    def test_diagnostic_cli_prints_structured_failure_and_exits_nonzero(self):
        self.retained_source()
        program = """import importlib.util,sys
from unittest.mock import patch
from pathlib import Path
root=Path(sys.argv[1]).resolve().parents[1]
sys.path[:0]=[str(root/'src'),str(root)]
from tools import test_global_vault_podman as fixture
fixture.load_runtime()
result={'status':sys.argv[3], 'engine_probes':[]}
with patch.object(fixture,'host_guard'), patch.object(fixture,'diagnose',return_value=result):
    sys.exit(fixture.main(['--diagnose',sys.argv[2]]))
"""
        for status, code in (("diagnostic-only", 0), ("diagnostic-failed", 1)):
            with self.subTest(status=status):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        "-c",
                        program,
                        str(SCRIPT),
                        str(self.root),
                        status,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(json.loads(result.stdout)["status"], status)
                self.assertEqual(result.stderr, "")

    def test_stale_boot_refuses_all_engine_operations_and_preserves_evidence(self):
        path, _, _ = self.retained_source()
        marker = self.root / "podman-tmp/alive"
        stale = b"00000000-0000-0000-0000-000000000002\n"
        marker.write_bytes(stale)
        before = fixture.source_evidence_digests(self.root, path)
        sessions = box.session_file(path).read_bytes()
        with (
            patch.object(fixture, "host_guard"),
            patch.object(box, "run") as run,
            patch.object(box, "podman") as podman,
            patch.object(fixture, "launcher") as launch,
            patch.object(backups, "owned_container") as container,
            patch.object(vault, "select_all") as unlock,
        ):
            result = fixture.diagnose(self.root)
            self.assertEqual(result["failed_stage"], "runtime-boot")
            self.assertEqual(result["error_category"], "runtime-boot-mismatch")
            self.assertFalse(result["runtime_boot"]["boot_ids_match"])
            self.assertEqual(result["engine_probes"], [])
            for operation in (
                fixture.validate_store,
                fixture.diagnostic_store_probes,
                fixture.cleanup,
            ):
                with (
                    self.subTest(operation=operation.__name__),
                    self.assertRaisesRegex(
                        fixture.FixtureError, "runtime-boot-mismatch"
                    ),
                ):
                    operation(self.root)
            with self.assertRaisesRegex(fixture.FixtureError, "retry-runtime-boot"):
                fixture.retry_source(self.root)
        for mock in (run, podman, launch, container, unlock):
            mock.assert_not_called()
        self.assertEqual(marker.read_bytes(), stale)
        self.assertEqual(fixture.source_evidence_digests(self.root, path), before)
        self.assertEqual(box.session_file(path).read_bytes(), sessions)
        self.assertFalse((self.root / "source-retries").exists())
        self.assertNotIn(stale.decode().strip(), json.dumps(result))
        self.assertNotIn(self.boot.decode().strip(), json.dumps(result))

    def test_boot_checkpoint_matches_exact_bytes_and_rejects_invalid_or_missing_marker(
        self,
    ):
        self.retained_source()
        marker = self.root / "podman-tmp/alive"
        self.assertTrue(fixture.require_runtime_boot(self.root)["boot_ids_match"])
        for content, category in (
            (self.boot.rstrip(b"\n"), "runtime-boot-mismatch"),
            (b"", "runtime-boot-marker"),
            (b"synthetic-private-invalid-marker", "runtime-boot-marker"),
        ):
            with self.subTest(content=content):
                marker.write_bytes(content)
                with self.assertRaisesRegex(fixture.FixtureError, category):
                    fixture.require_runtime_boot(self.root)
        marker.unlink()
        with self.assertRaisesRegex(fixture.FixtureError, "runtime-boot-marker"):
            fixture.require_runtime_boot(self.root)
        with patch.object(box, "run") as run:
            with self.assertRaisesRegex(fixture.FixtureError, "runtime-boot-marker"):
                fixture.validate_store(self.root)
        run.assert_not_called()
        self.assertFalse(marker.exists())

    def test_uninitialized_fresh_fixture_can_initialize_but_invalid_current_boot_cannot(
        self,
    ):
        self.complete_fixture_metadata()
        marker = self.root / "podman-tmp/alive"
        marker.unlink()
        with patch.object(
            box,
            "run",
            return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(self.info()), ""
            ),
        ) as run:
            fixture.validate_store(self.root)
        self.assertEqual(run.call_count, 3)
        with (
            patch.object(
                fixture, "system_boot_id", return_value=b"private-invalid-current"
            ),
            patch.object(box, "run") as run,
        ):
            with self.assertRaisesRegex(fixture.FixtureError, "runtime-boot-current"):
                fixture.validate_store(self.root)
        run.assert_not_called()

    def test_boot_marker_redirect_and_unreadable_current_id_fail_closed(self):
        self.retained_source()
        marker = self.root / "podman-tmp/alive"
        marker.unlink()
        marker.symlink_to(self.parent / "foreign-boot-marker")
        with patch.object(box, "run") as run:
            with self.assertRaises(fixture.FixtureError):
                fixture.diagnostic_store_probes(self.root)
        run.assert_not_called()
        marker.unlink()
        marker.write_bytes(self.boot)
        with (
            patch.object(fixture, "host_guard"),
            patch.object(
                fixture, "system_boot_id", side_effect=OSError("private-boot-error")
            ),
            patch.object(box, "run") as run,
        ):
            result = fixture.diagnose(self.root)
        run.assert_not_called()
        self.assertEqual(result["error_category"], "runtime-boot-inspection")
        self.assertNotIn("private-boot-error", json.dumps(result))

    def test_first_launch_trace_retains_primary_substep_without_private_output(self):
        self.complete_fixture_metadata()
        secret = "synthetic-private-first-launch-error"
        original = fixture.private_json(self.root, "fixture-ids.json")
        with self.assertRaises(fixture.FirstLaunchFailure) as raised:
            with fixture.first_launch_trace(self.root) as trace:
                try:
                    with trace.checkpoint("native-tool-client"):
                        trace.observe(
                            ["podman", secret], 19, "connection refused " + secret
                        )
                        raise box.BoxError(secret)
                finally:
                    with trace.checkpoint("container-stop"):
                        trace.observe(["podman", secret], 23, "private " + secret)
                        raise box.BoxError("private-cleanup " + secret)
        result = fixture.private_json(self.root, "first-launch-failure.json")
        self.assertEqual(result["scope"], "new-fixture-first-owning-launch")
        self.assertEqual(result["error"]["stage"], "native-tool-client")
        self.assertEqual(result["error"]["exit_code"], 19)
        self.assertEqual(result["cleanup_errors"][0]["stage"], "container-stop")
        self.assertEqual(raised.exception.diagnostic, result["error"])
        self.assertTrue(raised.exception.report_written)
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(fixture.private_json(self.root, "fixture-ids.json"), original)
        before = (self.root / "first-launch-failure.json").read_bytes()
        with self.assertRaisesRegex(fixture.FixtureError, "first-launch-report-exists"):
            with fixture.first_launch_trace(self.root):
                self.fail("Existing report allowed another launch")
        self.assertEqual((self.root / "first-launch-failure.json").read_bytes(), before)

    def test_first_launch_report_errors_do_not_replace_the_launch_failure(self):
        self.complete_fixture_metadata()
        with (
            patch.object(
                fixture, "write_json_new", side_effect=OSError("private-report-error")
            ),
            self.assertRaises(fixture.FirstLaunchFailure) as raised,
        ):
            with fixture.first_launch_trace(self.root) as trace:
                with trace.checkpoint("selected-session"):
                    raise KeyError("private-session-error")
        self.assertEqual(raised.exception.diagnostic["stage"], "selected-session")
        self.assertEqual(raised.exception.diagnostic["error_class"], "invalid-data")
        self.assertEqual(raised.exception.report_error, "os-error")
        self.assertFalse(raised.exception.report_written)
        self.assertNotIn("private", str(raised.exception))

    def test_successful_first_launch_trace_does_not_create_failure_evidence(self):
        self.complete_fixture_metadata()
        with fixture.first_launch_trace(self.root) as trace:
            with trace.checkpoint("selected-session"):
                pass
        self.assertIsNone(trace.failure)
        self.assertFalse((self.root / "first-launch-failure.json").exists())

    def test_new_full_driver_uses_trace_for_the_actual_first_owner_callback(self):
        self.complete_fixture_metadata()
        (self.root / "config/opencode").mkdir(mode=0o700)
        secret = "synthetic-first-owner-provider-error"
        baseline = {
            "info": {
                "id": "ses_fixture",
                "location": {"directory": str(self.root / "source-a")},
                "permissions": fixture.ASK_RULES,
            },
            "messages": [
                {
                    "id": "msg_fixture",
                    "type": "user",
                    "text": fixture.SEED_TEXT,
                    "time": {"created": 1},
                }
            ],
        }
        cleanup = []

        def shutdown():
            cleanup.append("shutdown")
            raise OSError("private-provider-shutdown " + secret)

        server = SimpleNamespace(
            server_port=12345,
            shutdown=shutdown,
            server_close=lambda: cleanup.append("close"),
        )
        worker = SimpleNamespace(
            join=lambda **kwargs: cleanup.append("join"), is_alive=lambda: False
        )

        def launch(arguments):
            self.assertIn("--import-session", arguments)
            path, _ = self.record("source-a", "a" * 32)
            box.atomic_json(
                box.session_file(path),
                {"sessions": ["ses_fixture"], "last": "ses_fixture"},
            )
            return box.run_tui(["opencode", "--session", "ses_fixture"])

        def native(*args, trace):
            trace("host-provider-health")
            trace.observe(["podman", secret], 18, "connection refused " + secret)
            raise box.BoxError(secret)

        with (
            patch.object(fixture, "host_guard"),
            patch.object(
                fixture,
                "isolated_env",
                return_value={"XDG_RUNTIME_DIR": "/run/user/9999"},
            ),
            patch.object(fixture, "validate_store"),
            patch.object(box, "check_backend"),
            patch.object(crypto, "tools"),
            patch.object(vault, "crypto"),
            patch.object(fixture, "prepare_native_cli"),
            patch.object(fixture, "seed_native_history", return_value=baseline),
            patch.object(fixture, "write_vault"),
            patch.object(fixture, "make_provider", return_value=(server, worker)),
            patch.object(fixture, "launcher", side_effect=launch),
            patch.object(fixture, "verify_owned_resources"),
            patch.object(
                fixture.workspace_service,
                "live_connection",
                return_value={"url": "http://127.0.0.1:1"},
            ),
            patch.object(box, "export_container_session", return_value=baseline),
            patch.object(fixture, "request_for", return_value=lambda *args: {}),
            patch.object(fixture, "run_native", side_effect=native),
            self.assertRaises(fixture.FirstLaunchFailure) as raised,
        ):
            fixture.run(
                self.root,
                Path(sys.executable).resolve(),
                "tumbleweed",
                report=lambda phase: None,
            )
        result = fixture.private_json(self.root, "first-launch-failure.json")
        self.assertEqual(raised.exception.diagnostic["stage"], "host-provider-health")
        self.assertEqual(result["error"]["exit_code"], 18)
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(cleanup, ["shutdown", "close", "join"])
        self.assertEqual(
            raised.exception.shutdown_errors,
            [{"stage": "provider-shutdown", "error_class": "os-error"}],
        )

    def test_new_full_driver_cli_prints_primary_failure_and_private_report_path(self):
        secret = "synthetic-private-first-launch-cli-error"
        program = """import importlib.util,sys
from unittest.mock import patch
from pathlib import Path
root=Path(sys.argv[1]).resolve().parents[1]
sys.path[:0]=[str(root/'src'),str(root)]
from tools import test_global_vault_podman as fixture
fixture.load_runtime()
def fail(root,*args):
    with fixture.first_launch_trace(root) as trace:
        with trace.checkpoint('config-reload'):
            raise KeyError(sys.argv[3])
with patch.object(fixture,'host_guard'), patch.object(fixture.shutil,'which',return_value=str(fixture.Path(sys.executable).resolve())), patch.object(fixture,'capture_binding',return_value={'source':{'commit':'a'*40},'native':{}}), patch.object(fixture,'verify_binding'), patch.object(fixture,'run',side_effect=fail):
    sys.exit(fixture.main(['--work-dir',sys.argv[2],'--opencode-v2',sys.executable,'--expect-commit','a'*40]))
"""
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                program,
                str(SCRIPT),
                str(self.parent),
                secret,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("stage=config-reload class=invalid-data", result.stdout)
        self.assertIn("Report: ", result.stdout)
        self.assertIn("first-launch-failure.json", result.stdout)
        self.assertEqual(result.stderr, "")
        self.assertNotIn(secret, result.stdout)

    def test_first_launch_host_clean_runs_after_synthetic_controls_are_removed(self):
        self.complete_fixture_metadata()
        (self.root / "config/opencode").mkdir(mode=0o700)
        baseline = {"info": {"id": "ses_fixture"}, "messages": []}
        server = SimpleNamespace(
            server_port=12345, shutdown=lambda: None, server_close=lambda: None
        )
        worker = SimpleNamespace(join=lambda **kwargs: None, is_alive=lambda: False)
        checked = []
        original_check = fixture.assert_host_clean
        original_vault_check = fixture.assert_host_vault_clean

        class FirstPhaseComplete(Exception):
            pass

        def report(phase):
            if phase == "first-import-all-entries-build-asks":
                raise FirstPhaseComplete()

        def launch(arguments):
            self.assertIn("--import-session", arguments)
            self.assertIn(fixture.AMBIENT, os.environ)
            self.assertIn(fixture.PASS_ENV, os.environ)
            self.record("source-a", "a" * 32)
            vault.hidden()  # Exactly one scripted unlock for this owning launch.
            return 0

        def host_check():
            checked.append({name: name in os.environ for name in fixture.KNOWN})
            original_check()

        with (
            patch.object(fixture, "host_guard"),
            patch.object(
                fixture,
                "isolated_env",
                return_value={"XDG_RUNTIME_DIR": "/run/user/9999"},
            ),
            patch.object(fixture, "validate_store"),
            patch.object(box, "check_backend"),
            patch.object(crypto, "tools"),
            patch.object(vault, "crypto"),
            patch.object(fixture, "prepare_native_cli"),
            patch.object(fixture, "seed_native_history", return_value=baseline),
            patch.object(fixture, "write_vault"),
            patch.object(fixture, "make_provider", return_value=(server, worker)),
            patch.object(fixture, "launcher", side_effect=launch),
            patch.object(fixture, "verify_owned_resources"),
            patch.object(fixture, "assert_host_clean", side_effect=host_check),
            patch.object(
                fixture, "assert_host_vault_clean", wraps=original_vault_check
            ) as vault_check,
            self.assertRaises(FirstPhaseComplete),
        ):
            fixture.run(
                self.root, Path(sys.executable).resolve(), "tumbleweed", report=report
            )
        self.assertEqual(checked, [{name: False for name in fixture.KNOWN}])
        self.assertEqual(vault_check.call_count, 2)
        self.assertFalse((self.root / "first-launch-failure.json").exists())

    def test_host_clean_retains_cache_and_environment_leak_checks(self):
        with patch.object(
            fixture.credentials, "has_unlocked_values", return_value=True
        ):
            with self.assertRaisesRegex(fixture.FixtureError, "host-value-retention"):
                fixture.assert_host_clean()
        for name in fixture.KNOWN:
            with (
                self.subTest(name=name),
                patch.dict(os.environ, {name: "synthetic-leak-control"}),
            ):
                with self.assertRaisesRegex(fixture.FixtureError, "host-value-leak"):
                    fixture.assert_host_clean()
                if name in (fixture.KEY, fixture.SIDE, fixture.ADDED):
                    with self.assertRaisesRegex(
                        fixture.FixtureError, "host-value-leak"
                    ):
                        fixture.assert_host_vault_clean()
                else:
                    fixture.assert_host_vault_clean()

    def test_source_retry_requires_verified_selection_and_rejects_later_evidence(self):
        path, data, baseline = self.retained_source()
        result = fixture.validate_source_retry(self.root)
        self.assertEqual(result[:3], (self.root, path, data))
        self.assertEqual(result[3], baseline)
        original = fixture.source_evidence_digests(self.root, path)
        for name in ("full.ocbox", "result.json", "restored-full"):
            artifact = self.root / name
            artifact.write_text("retained later-stage evidence")
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(fixture.FixtureError, "retry-later-evidence"),
            ):
                fixture.validate_source_retry(self.root)
            artifact.unlink()
        self.assertEqual(fixture.source_evidence_digests(self.root, path), original)
        state = box.session_state(path)
        state["last"] = "ses_unexpected"
        box.atomic_json(box.session_file(path), state)
        with self.assertRaisesRegex(fixture.FixtureError, "retry-verified-import"):
            fixture.validate_source_retry(self.root)

    def test_attempt_selector_keeps_original_history_without_rewriting_legacy_wrapper(
        self,
    ):
        path, _, _ = self.retained_source()
        before = fixture.source_evidence_digests(self.root, path)
        attempt = self.root / "source-retries/attempt-unit"
        attempt.mkdir(mode=0o700, parents=True)
        wrapper = fixture.prepare_source_retry_selector(
            self.root, attempt, Path(sys.executable).resolve()
        )
        program = "import json,os; print(json.dumps({key:os.environ.get(key) for key in ('HOME','XDG_CONFIG_HOME','XDG_DATA_HOME','XDG_STATE_HOME','OPENCODE_AUTH_CONTENT')}))"
        result = subprocess.run(
            [str(wrapper), "-I", "-c", program],
            env={
                **os.environ,
                "XDG_DATA_HOME": "/unrelated/data",
                "OPENCODE_AUTH_CONTENT": "unrelated private value",
            },
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)
        self.assertEqual(response["XDG_DATA_HOME"], str(self.root / "host-data-2"))
        self.assertEqual(response["XDG_STATE_HOME"], str(self.root / "host-state-2"))
        self.assertEqual(response["HOME"], str(self.root / "host-home"))
        self.assertIsNone(response["OPENCODE_AUTH_CONTENT"])
        self.assertEqual(fixture.source_evidence_digests(self.root, path), before)

    def test_retry_trace_keeps_first_failure_separate_and_never_echoes_private_text(
        self,
    ):
        secret = "synthetic-sensitive-trace-value"
        trace = fixture.SourceRetryTrace()
        trace("host-provider-health")
        trace.observe(["podman", secret], 17, b"connection refused " + secret.encode())
        trace.fail(box.BoxError("private failure " + secret))
        original = copy.deepcopy(trace.failure)
        trace("container-stop")
        trace.observe(["podman", secret], 23, secret.encode())
        trace.fail(box.BoxError("private cleanup " + secret))
        self.assertEqual(trace.failure, original)
        self.assertEqual(trace.failure["exit_code"], 17)
        self.assertEqual(len(trace.cleanup_errors), 1)
        self.assertNotIn(
            secret,
            json.dumps(
                {
                    "error": trace.failure,
                    "cleanup": trace.cleanup_errors,
                    "steps": trace.steps,
                }
            ),
        )
        with os.fdopen(
            os.memfd_create("trace-fixture", os.MFD_CLOEXEC), "w+b"
        ) as stream:
            stream.write(b"private native stderr " + secret.encode())
            stream.seek(5)
            position = stream.tell()
            self.assertIn(secret.encode(), fixture.private_stderr(stream))
            self.assertEqual(stream.tell(), position)

    def test_nested_native_probe_diagnostic_accepts_fixed_schema_only(self):
        valid = {
            "stage": "native-tool-client",
            "error_class": "subprocess-exit",
            "native_exit_code": 2,
            "stderr_category": "http",
        }
        self.assertEqual(fixture.probe_diagnostic(json.dumps(valid)), valid)
        for field, value in (
            ("stage", "private-stage-value"),
            ("stderr_category", "private-category"),
            ("error_class", "private-error"),
        ):
            poisoned = {**valid, field: value}
            with self.subTest(field=field):
                self.assertIsNone(fixture.probe_diagnostic(json.dumps(poisoned)))
        self.assertIsNone(
            fixture.probe_diagnostic(json.dumps({**valid, "private": "value"}))
        )

    def test_repropagated_error_does_not_invent_a_completed_shutdown_failure(self):
        trace = fixture.SourceRetryTrace()
        error = json.JSONDecodeError("synthetic-private-error", "", 0)
        trace("permission-cleanup")
        trace.fail(error)
        trace("container-stop")
        trace.completed()
        trace.fail(error)
        self.assertEqual(trace.failure["stage"], "permission-cleanup")
        self.assertEqual(
            trace.steps[-1], {"stage": "container-stop", "state": "completed"}
        )
        self.assertEqual(trace.cleanup_errors, [])
        trace("container-stop")
        trace.fail(box.BoxError("synthetic-actual-shutdown-error"))
        self.assertEqual(trace.cleanup_errors[0]["stage"], "container-stop")
        self.assertEqual(trace.steps[-1]["state"], "failed")

    def trace_owner(self, *, ready=False, code=None, primary=None, cleanup=None):
        calls = []

        class Owner:
            def __enter__(self):
                if primary is not None:
                    try:
                        raise primary
                    except BaseException as error:
                        self.shutdown(primary=error)
                        raise
                return self

            def shutdown(self, *args, **kwargs):
                calls.append((args, kwargs))
                if cleanup is not None:
                    raise cleanup
                return "synthetic cleanup completed"

        owner = Owner()
        owner.ready = ready
        owner.error_file = io.BytesIO(b"private-observer-sentinel")
        owner.child = SimpleNamespace(
            poll=Mock(side_effect=AssertionError("observer must not reap"))
        )
        owner.child_owner = SimpleNamespace(status=Mock(return_value=code))
        return Owner, owner, calls

    def test_owner_trace_observes_nonreaping_status_and_always_forwards_cleanup(self):
        for state in (None, 0, 17, "no-child", "lost"):
            with self.subTest(state=state):
                cleanup = (
                    box.BoxError("synthetic cleanup refusal")
                    if state == "lost"
                    else None
                )
                owner_class, owner, calls = self.trace_owner(
                    code=state, cleanup=cleanup
                )
                status = owner.child_owner.status
                if state == "no-child":
                    owner.child = owner.child_owner = None
                elif state == "lost":
                    status.side_effect = ChildProcessError("private-observer-sentinel")
                trace = fixture.SourceRetryTrace()
                trace("private-owner")
                with (
                    patch.object(fixture.workspace_service, "Owner", owner_class),
                    trace.installed(),
                ):
                    if cleanup is None:
                        self.assertEqual(
                            owner.shutdown(), "synthetic cleanup completed"
                        )
                    else:
                        with self.assertRaises(box.BoxError) as raised:
                            owner.shutdown()
                        self.assertIs(raised.exception, cleanup)
                self.assertEqual(calls, [((), {})])
                if owner.child is not None:
                    owner.child.poll.assert_not_called()
                    status.assert_called_once()
                else:
                    status.assert_not_called()
                if state != "lost":
                    self.assertEqual(
                        trace.subprocess_failure["exit_code"],
                        None if state == "no-child" else state,
                    )
                self.assertNotIn(
                    "private-observer-sentinel",
                    json.dumps(
                        {"failure": trace.failure, "cleanup": trace.cleanup_errors}
                    ),
                )

    def test_owner_trace_preserves_startup_primary_identity_and_keyword(self):
        primary = ValueError("private-primary-sentinel")
        owner_class, owner, calls = self.trace_owner(code=17, primary=primary)
        trace = fixture.SourceRetryTrace()
        with (
            patch.object(fixture.workspace_service, "Owner", owner_class),
            trace.installed(),
            self.assertRaises(ValueError) as raised,
        ):
            owner.__enter__()
        self.assertIs(raised.exception, primary)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], ())
        self.assertIs(calls[0][1]["primary"], primary)
        self.assertEqual(trace.cleanup_errors, [])
        self.assertEqual(len(trace._seen_failures), 1)
        self.assertIs(trace._seen_failures[0], primary)
        self.assertEqual(trace.failure["stage"], "private-owner")
        self.assertEqual(trace.failure["exit_code"], 17)
        owner.child.poll.assert_not_called()

    def test_owner_observer_uses_explicit_primary_not_an_unrelated_caught_exception(
        self,
    ):
        owner_class, owner, calls = self.trace_owner(code=0)
        trace = fixture.SourceRetryTrace()
        with (
            patch.object(fixture.workspace_service, "Owner", owner_class),
            trace.installed(),
        ):
            try:
                raise ValueError("private-unrelated-caught-error")
            except ValueError:
                owner.shutdown(primary=None)
        self.assertIsNone(trace.failure)
        self.assertEqual(calls, [((), {"primary": None})])

    def test_owner_trace_forwards_arguments_and_preserves_actual_cleanup_error(self):
        primary, extra = ValueError("synthetic primary"), object()
        cleanup = OSError("private-cleanup-sentinel")
        owner_class, owner, calls = self.trace_owner(ready=True, cleanup=cleanup)
        trace = fixture.SourceRetryTrace()
        with (
            patch.object(fixture.workspace_service, "Owner", owner_class),
            trace.installed(),
            self.assertRaises(OSError) as raised,
        ):
            owner.shutdown("synthetic argument", primary=primary, extra=extra)
        self.assertIs(raised.exception, cleanup)
        self.assertEqual(calls[0][0], ("synthetic argument",))
        self.assertIs(calls[0][1]["primary"], primary)
        self.assertIs(calls[0][1]["extra"], extra)
        owner.child_owner.status.assert_not_called()
        owner.child.poll.assert_not_called()
        self.assertEqual(trace.failure["stage"], "private-owner-stop")
        self.assertNotIn("private-cleanup-sentinel", json.dumps(trace.failure))

    def test_owner_trace_wraps_real_partial_startup_without_replacing_primary(self):
        service = fixture.workspace_service
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.parent)}):
            owner = service.Owner(
                box, self.root / "workspace.json", {"id": "synthetic-trace"}
            )
        primary = RuntimeError("private-primary-sentinel")
        program = "import json,signal,sys; signal.alarm(10); print(json.dumps({'url':'http://127.0.0.1:12345','password':'a'*43}),flush=True); sys.stdin.read()"
        trace = fixture.SourceRetryTrace()
        previous = service.live_connection()
        try:
            with (
                patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.parent)}),
                patch.object(
                    box,
                    "exec_command",
                    return_value=box.ContainerCommand(
                        [sys.executable, "-B", "-c", program],
                        box.maintenance_host_env(),
                    ),
                ),
                patch.object(service.threading.Thread, "start", side_effect=primary),
                trace.installed(),
                box.workspace_lock(self.root / "workspace.json"),
                self.assertRaises(RuntimeError) as raised,
            ):
                owner.__enter__()
            self.assertIs(raised.exception, primary)
            self.assertIsNone(owner.child)
            self.assertIsNone(owner.child_owner)
            self.assertTrue(owner.error_file.closed)
            self.assertFalse(owner.address.exists())
            self.assertIs(service.live_connection(), previous)
            self.assertEqual(len(trace._seen_failures), 1)
            self.assertIs(trace._seen_failures[0], primary)
            self.assertEqual(trace.cleanup_errors, [])
            self.assertNotIn("private-primary-sentinel", json.dumps(trace.failure))
        finally:
            owner.shutdown(primary=primary)

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

    def test_failed_source_retry_writes_private_report_and_keeps_original_evidence(
        self,
    ):
        path, data, baseline = self.retained_source()
        state = box.session_state(path)
        state["sessions"].append("ses_other_recorded")
        state["last"] = "ses_other_recorded"
        box.atomic_json(box.session_file(path), state)
        before = fixture.source_evidence_digests(self.root, path)
        secret = "synthetic-private-retry-failure-value"
        with (
            patch.object(fixture, "host_guard"),
            patch.object(fixture, "validate_store"),
            patch.object(fixture, "check_native_version", return_value="2.2.6"),
            patch.object(
                fixture,
                "verify_stopped_source",
                return_value={"State": {"Running": False, "Status": "exited"}},
            ),
            patch.object(box, "check_backend"),
            patch.object(
                fixture, "launcher", side_effect=box.BoxError("private " + secret)
            ) as launch,
            patch.object(
                box,
                "remove_resources",
                side_effect=AssertionError("No retry reset/purge"),
            ),
            patch.object(
                box,
                "import_host_session",
                side_effect=AssertionError("No retry reimport"),
            ),
        ):
            report_path, result = fixture.retry_source(self.root)
        self.assertEqual(launch.call_args.args[0], ["start", "--workspace", data["id"]])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["scope"], "retained-source-only")
        self.assertEqual(result["diagnostic_session"], baseline["info"]["id"])
        self.assertEqual(result["selected_session_before"], "ses_other_recorded")
        self.assertEqual(result["error"]["stage"], "owning-launch")
        self.assertEqual(result["error"]["error_class"], "box-error")
        self.assertTrue(result["evidence_unchanged"])
        self.assertNotIn(secret, report_path.read_text())
        self.assertEqual(report_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(fixture.source_evidence_digests(self.root, path), before)
        self.assertTrue((report_path.parent / "vault/vault.json").is_file())
        self.assertFalse((self.root / "result.json").exists())

    def test_verified_seed_can_be_checked_without_rewriting_another_recorded_selection(
        self,
    ):
        path, _, baseline = self.retained_source()
        state = box.session_state(path)
        state["sessions"].append("ses_other_recorded")
        state["last"] = "ses_other_recorded"
        box.atomic_json(box.session_file(path), state)
        before = box.session_file(path).read_bytes()
        _, _, _, checked, _ = fixture.validate_source_retry(self.root)
        self.assertEqual(checked, baseline)
        self.assertEqual(box.session_file(path).read_bytes(), before)
        state["last"] = "ses_not_recorded"
        box.atomic_json(box.session_file(path), state)
        with self.assertRaisesRegex(fixture.FixtureError, "retry-verified-import"):
            fixture.validate_source_retry(self.root)

    def test_retry_selected_metadata_must_be_a_root_in_the_same_private_project(self):
        _, data, _ = self.retained_source()
        info = {"id": "ses_other_recorded", "location": {"directory": data["project"]}}
        fixture.validate_selected_source_session(info, "ses_other_recorded", data)
        for changed in (
            {**info, "id": "ses_wrong"},
            {**info, "parentID": "ses_parent"},
            {**info, "location": {"directory": str(self.parent)}},
            {**info, "location": None},
        ):
            with self.subTest(info=changed), self.assertRaises(fixture.FixtureError):
                fixture.validate_selected_source_session(
                    changed, "ses_other_recorded", data
                )

    @contextlib.contextmanager
    def full_driver(
        self, *, changed_phase=None, failing=False, finalizer=None, mutation="change"
    ):
        """Real orchestration; synthetic native and engine boundaries only."""
        with tempfile.TemporaryDirectory(
            prefix="gv-driver-", dir=self.parent
        ) as parent:
            root = Path(parent) / (fixture.PREFIX + "driver")
            root.mkdir(mode=0o700)
            fixture.prepare_root(root, "tumbleweed")
            fixture.prepare_podman_cli(root, Path(sys.executable).resolve())
            (root / "podman-tmp/alive").write_bytes(self.boot)
            records, events = {}, []
            cache = [False]
            baseline = {
                "info": {
                    "id": "ses_fixture",
                    "location": {"directory": str(root / "source-a")},
                    "permissions": fixture.ASK_RULES,
                },
                "messages": [
                    {
                        "id": "msg_fixture",
                        "type": "user",
                        "text": fixture.SEED_TEXT,
                        "time": {"created": 1},
                    }
                ],
            }
            primary = box.BoxError("private-launch-sentinel")

            def create(name, origin=None):
                path, data = box.create_workspace(root / name, "tumbleweed")
                data.update(opencode_version="2.2.6", ready=True)
                if origin is not None:
                    data.update(
                        container_project=str(root / "source-a"),
                        restored_from={"kind": "full", "workspace_id": origin},
                        credentials={"mode": "vault", "env": [], "vault": []},
                    )
                box.atomic_json(path, data)
                box.atomic_json(
                    box.session_file(path),
                    {"sessions": ["ses_fixture"], "last": "ses_fixture"},
                )
                records[name] = (path, data)

            current = ["source-a"]

            def launch(arguments):
                name = (
                    "source-b"
                    if "--no-vault" in arguments or str(root / "source-b") in arguments
                    else "source-a"
                )
                current[0] = name
                if name not in records:
                    create(name)
                phase = (
                    "opt-out"
                    if "--no-vault" in arguments
                    else "second-owner"
                    if name == "source-b"
                    else "first-owner"
                )
                if phase != "opt-out":
                    vault.hidden()
                box.run_tui(["native", "--session", "ses_fixture"])
                if phase == changed_phase:
                    if mutation == "cache":
                        cache[0] = True
                    else:
                        os.environ[
                            fixture.SIDE if mutation == "insert" else fixture.PASS_ENV
                        ] = "private-environment-sentinel"
                    if failing:
                        raise primary
                return 0

            def restored(*args):
                (root / "restored-full").mkdir()
                create("restored-full", records["source-a"][1]["id"])

            def action(stage):
                events.append(stage)
                if stage == finalizer:
                    raise OSError("private-finalizer-sentinel")

            server = SimpleNamespace(
                server_port=12345,
                shutdown=lambda: action("shutdown"),
                server_close=lambda: action("close"),
            )
            worker = SimpleNamespace(
                join=lambda **kw: action("join"), is_alive=lambda: finalizer == "alive"
            )

            def request(*args):
                return {
                    "data": {
                        "id": "ses_fixture",
                        "location": {"directory": str(root / current[0])},
                    }
                }

            with contextlib.ExitStack() as stack:
                stack.enter_context(
                    patch.dict(os.environ, fixture.isolated_env(root), clear=True)
                )
                for target, name, value in (
                    (fixture.credentials, "has_unlocked_values", lambda: cache[0]),
                    (box, "exec_command", lambda data, command: command),
                    (fixture, "host_guard", lambda: None),
                    (fixture, "validate_store", lambda *a, **k: None),
                    (box, "check_backend", lambda **kw: None),
                    (crypto, "tools", lambda: None),
                    (vault, "crypto", lambda: None),
                    (fixture, "prepare_native_cli", lambda *a: None),
                    (fixture, "seed_native_history", lambda *a: baseline),
                    (fixture, "write_vault", lambda *a: None),
                    (fixture, "make_provider", lambda *a: (server, worker)),
                    (fixture, "launcher", launch),
                    (fixture, "verify_owned_resources", lambda *a, **kw: {}),
                    (
                        fixture.workspace_service,
                        "live_connection",
                        lambda: {"url": "http://127.0.0.1:12345"},
                    ),
                    (box, "export_container_session", lambda *a: baseline),
                    (fixture, "run_native", lambda *a, **kw: baseline),
                    (fixture, "permission_matrix", lambda *a, **kw: None),
                    (fixture, "default_plan_fallback", lambda *a: None),
                    (fixture, "request_for", lambda *a: request),
                    (
                        fixture.workspace_service,
                        "attach",
                        lambda *a: contextlib.nullcontext({"session": "ses_fixture"}),
                    ),
                    (fixture.workspace_service, "api", lambda *a: {}),
                    (box, "run", lambda *a, **kw: None),
                    (fixture, "create_session", lambda *a, **kw: {"id": "ses_fixture"}),
                    (crypto, "digest", lambda *a: "a" * 64),
                    (fixture, "full_backup_restore", restored),
                ):
                    stack.enter_context(patch.object(target, name, value))
                yield root, events, primary

    def test_actual_later_owner_scopes_detect_mutation_after_callback_and_on_failure(
        self,
    ):
        for phase, mutation in product(
            ("second-owner", "opt-out"), ("insert", "change", "cache")
        ):
            for failing in (False, True):
                with self.subTest(phase=phase, mutation=mutation, failing=failing):
                    with self.full_driver(
                        changed_phase=phase, failing=failing, mutation=mutation
                    ) as (
                        root,
                        events,
                        primary,
                    ):
                        baseline = dict(os.environ)
                        with self.assertRaises(
                            (box.BoxError, fixture.FixtureError)
                        ) as raised:
                            fixture.run(
                                root, Path(sys.executable).resolve(), "tumbleweed"
                            )
                        if failing:
                            self.assertIs(raised.exception, primary)
                            self.assertTrue(primary.fixture_secondary)
                        else:
                            self.assertEqual(
                                str(raised.exception),
                                "host-value-retention"
                                if mutation == "cache"
                                else "host-environment-changed",
                            )
                        self.assertEqual(dict(os.environ), baseline)
                        self.assertEqual(events, ["shutdown", "close", "join"])
                        self.assertFalse((root / "result.json").exists())

    def test_actual_success_body_does_not_publish_pass_before_provider_finalization(
        self,
    ):
        for finalizer in (None, "shutdown", "close", "join", "alive"):
            with self.subTest(finalizer=finalizer):
                with self.full_driver(finalizer=finalizer) as (root, events, _):
                    if finalizer is None:
                        result = fixture.run(
                            root, Path(sys.executable).resolve(), "tumbleweed"
                        )
                        self.assertEqual(result["status"], "passed")
                    else:
                        with self.assertRaises((OSError, fixture.FixtureError)):
                            fixture.run(
                                root, Path(sys.executable).resolve(), "tumbleweed"
                            )
                    self.assertFalse((root / "result.json").exists())
                    self.assertEqual(events, ["shutdown", "close", "join"])

    def test_cli_terminal_verdict_agrees_with_finalizer_and_journal_failures(self):
        for failure in (None, "shutdown", "close", "join", "alive", "journal"):
            with self.subTest(failure=failure):
                before = set(self.parent.glob(fixture.PREFIX + "*"))
                calls = []

                def action(stage):
                    calls.append(stage)
                    if stage == failure:
                        raise OSError("private-finalizer-sentinel")

                def body(*args):
                    server = SimpleNamespace(
                        shutdown=lambda: action("shutdown"),
                        server_close=lambda: action("close"),
                    )
                    worker = SimpleNamespace(
                        join=lambda **kw: action("join"),
                        is_alive=lambda: failure == "alive",
                    )
                    fixture.finish_provider(server, worker, primary=None)
                    return {"checks": [], "status": "passed"}

                actual_journal = fixture.record_ids

                def journal(root):
                    action("journal")
                    return actual_journal(root)

                with (
                    patch.object(fixture, "host_guard"),
                    patch.object(
                        fixture,
                        "capture_binding",
                        return_value={"source": {"commit": "a" * 40}, "native": {}},
                    ),
                    patch.object(fixture, "verify_binding"),
                    patch.object(
                        fixture.shutil,
                        "which",
                        return_value=str(Path(sys.executable).resolve()),
                    ),
                    patch.object(fixture, "run", side_effect=body),
                    patch.object(fixture, "record_ids", side_effect=journal),
                    patch("sys.stdout", new_callable=io.StringIO),
                ):
                    code = fixture.main(
                        [
                            "--work-dir",
                            str(self.parent),
                            "--opencode-v2",
                            sys.executable,
                            "--expect-commit",
                            "a" * 40,
                        ]
                    )
                created = set(self.parent.glob(fixture.PREFIX + "*")) - before
                self.assertEqual(len(created), 1)
                result = fixture.private_json(created.pop(), "result.json")
                self.assertEqual(code, int(failure is not None))
                self.assertEqual(result["status"], "failed" if failure else "passed")
                if failure is not None:
                    self.assertEqual(
                        result["error"]["stage"],
                        "journal-reconciliation"
                        if failure == "journal"
                        else "provider-join"
                        if failure == "alive"
                        else "provider-" + failure,
                    )
                self.assertEqual(calls, ["shutdown", "close", "join", "journal"])
                self.assertNotIn("private-finalizer-sentinel", json.dumps(result))

    def test_fresh_placement_rejects_retained_families_and_renamed_markers_before_binding(
        self,
    ):
        for marker in fixture.RETAINED_MARKERS:
            for option in ("--work-dir", "--temp-dir"):
                with self.subTest(marker=marker, option=option):
                    with tempfile.TemporaryDirectory(
                        prefix="retained-", dir=self.parent
                    ) as parent:
                        retained = Path(parent)
                        (retained / marker).write_text("private-retained-evidence")
                        child = retained / "nested"
                        child.mkdir()
                        arguments = [
                            "--work-dir",
                            str(child if option == "--work-dir" else self.parent),
                            "--opencode-v2",
                            sys.executable,
                            "--expect-commit",
                            "a" * 40,
                        ]
                        if option == "--temp-dir":
                            arguments.extend([option, str(child)])
                        with (
                            patch.object(fixture, "host_guard"),
                            patch.object(fixture, "capture_binding") as capture,
                            patch.object(fixture.tempfile, "mkdtemp") as allocate,
                            patch("sys.stdout", new_callable=io.StringIO),
                        ):
                            self.assertEqual(fixture.main(arguments), 1)
                        capture.assert_not_called()
                        allocate.assert_not_called()
                        self.assertEqual(
                            (retained / marker).read_text(), "private-retained-evidence"
                        )
        for prefix in fixture.RETAINED_PREFIXES:
            root = self.parent / (prefix + "retained")
            root.mkdir()
            with self.assertRaisesRegex(fixture.FixtureError, "fresh-parent-required"):
                fixture.fresh_parent(root)

    def test_missing_expected_source_and_foreign_source_refuse_before_allocation(self):
        for extra in (
            [],
            ["--source", "/private-source-sentinel", "--expect-commit", "a" * 40],
        ):
            with self.subTest(extra=bool(extra)):
                with (
                    patch.object(fixture, "host_guard"),
                    patch.object(fixture, "capture_binding") as capture,
                    patch.object(fixture.tempfile, "mkdtemp") as allocate,
                    patch("sys.stdout", new_callable=io.StringIO) as output,
                ):
                    self.assertEqual(
                        fixture.main(
                            [
                                "--work-dir",
                                str(self.parent),
                                "--opencode-v2",
                                sys.executable,
                                *extra,
                            ]
                        ),
                        1,
                    )
                capture.assert_not_called()
                allocate.assert_not_called()
                self.assertNotIn("private-source-sentinel", output.getvalue())

    def test_cli_host_refusal_and_help_never_import_candidate_runtime(self):
        program = """import runpy,sys
def guard(event,args):
    if event=='import' and (args[0]=='oc_box' or args[0].startswith(('oc_box.','tools.'))):
        raise AssertionError('candidate imported before host/source verification')
sys.addaudithook(guard)
sys.argv=[sys.argv[1],*sys.argv[2:]]
runpy.run_path(sys.argv[0],run_name='__main__')
"""
        for arguments, expected in (
            (["--help"], 0),
            (["--work-dir", str(self.parent)], 1),
        ):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        "-c",
                        program,
                        str(SCRIPT),
                        *arguments,
                    ],
                    env={"PATH": os.defpath, "OC_BOX_CONTAINER": "1"},
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertNotIn("candidate imported", result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_binding_refuses_changed_source_and_native_without_repinning(self):
        binary = self.parent / "native"
        binary.write_bytes(Path(sys.executable).resolve().read_bytes())
        binary.chmod(0o700)
        original = hashlib.sha256(binary.read_bytes()).hexdigest()
        identity = {
            "commit": "a" * 40,
            "commit_count": 140,
            "dirty": False,
            "sha256": {"tools/test_global_vault_podman.py": "b" * 64},
        }
        binding = {
            "source": identity,
            "native": {
                "opencode": {
                    "path": str(binary),
                    "sha256": original,
                    "bytes": binary.stat().st_size,
                }
            },
        }
        with (
            patch.object(fixture, "source_snapshot", return_value=identity),
            patch.object(fixture, "import_origins"),
        ):
            fixture.verify_binding(binding)
            with binary.open("ab") as stream:
                stream.write(b"synthetic changed bytes")
            with self.assertRaisesRegex(fixture.FixtureError, "native-changed"):
                fixture.verify_binding(binding)
        self.assertEqual(binding["native"]["opencode"]["sha256"], original)
        with patch.object(
            fixture, "source_snapshot", return_value={**identity, "commit": "c" * 40}
        ):
            with self.assertRaisesRegex(fixture.FixtureError, "source-changed"):
                fixture.verify_binding(binding)

    def test_retry_scope_checks_mutation_before_restoring_attempt_configuration(self):
        path, data, baseline = self.retained_source()
        for failing, mutation in product((False, True), ("insert", "change", "cache")):
            with self.subTest(failing=failing, mutation=mutation):
                primary = box.BoxError("private-retry-primary")
                environment = dict(os.environ)
                cache = [False]

                def launch(arguments):
                    vault.hidden()
                    box.run_tui(["native", "--session", "ses_fixture"])
                    if mutation == "cache":
                        cache[0] = True
                    else:
                        os.environ[
                            fixture.SIDE if mutation == "insert" else "XDG_CONFIG_HOME"
                        ] = "private-retry-value"
                    if failing:
                        raise primary
                    return 0

                request = lambda *a: {
                    "data": {
                        "id": "ses_fixture",
                        "location": {"directory": data["project"]},
                    }
                }
                server = SimpleNamespace(
                    server_port=12345, shutdown=lambda: None, server_close=lambda: None
                )
                worker = SimpleNamespace(join=lambda **kw: None, is_alive=lambda: False)
                if failing and mutation == "insert":

                    def refused_cleanup(*args, **kwargs):
                        raise OSError("private-retry-cleanup")

                    server.shutdown = server.server_close = worker.join = (
                        refused_cleanup
                    )
                with (
                    patch.object(
                        fixture.credentials,
                        "has_unlocked_values",
                        side_effect=lambda: cache[0],
                    ),
                    patch.object(fixture, "host_guard"),
                    patch.object(fixture, "validate_store"),
                    patch.object(fixture, "check_native_version"),
                    patch.object(fixture, "verify_stopped_source"),
                    patch.object(box, "check_backend"),
                    patch.object(
                        fixture, "make_provider", return_value=(server, worker)
                    ),
                    patch.object(fixture, "launcher", side_effect=launch),
                    patch.object(fixture, "verify_owned_resources"),
                    patch.object(
                        fixture.workspace_service,
                        "live_connection",
                        return_value={"url": "http://127.0.0.1:12345"},
                    ),
                    patch.object(fixture, "request_for", return_value=request),
                    patch.object(
                        box, "export_container_session", return_value=baseline
                    ),
                    patch.object(fixture, "run_native"),
                    patch.object(fixture, "permission_matrix"),
                ):
                    report_path, result = fixture.retry_source(self.root)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(dict(os.environ), environment)
                self.assertNotIn("private-retry-value", report_path.read_text())
                self.assertNotIn("private-retry-primary", report_path.read_text())
                self.assertNotIn("private-retry-cleanup", report_path.read_text())
                self.assertTrue(result["evidence_unchanged"])
                if failing and mutation == "insert":
                    self.assertTrue(
                        {"provider-shutdown", "provider-close", "provider-join"}
                        <= {item["stage"] for item in result["cleanup_errors"]}
                    )
                if failing:
                    self.assertTrue(
                        any(
                            item.get("category")
                            in ("host-environment-changed", "host-value-retention")
                            for item in result["cleanup_errors"]
                        )
                    )

    def test_capture_isolates_poisoned_bytecode_after_initial_snapshot_before_import(
        self,
    ):
        program = r"""import hashlib,importlib,json,os,py_compile,sys,tempfile
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=[sys.argv[1]+'/src',sys.argv[1]]
from tools import test_global_vault_podman as fixture
real=fixture.APP
with tempfile.TemporaryDirectory(prefix='gv-bootstrap-',dir=sys.argv[2]) as temporary:
    root=Path(temporary); (root/'tools').mkdir(); (root/'src').mkdir()
    (root/'src/oc_box').mkdir()
    init=root/'src/oc_box/__init__.py'; init.write_text('')
    foreign=root/'foreign'; (foreign/'tools').mkdir(parents=True)
    (foreign/'tools/__init__.py').write_text("raise RuntimeError('foreign candidate executed')\n")
    if sys.argv[3]=='root': (root/'tools.py').write_text("raise RuntimeError('root candidate executed')\n")
    if sys.argv[3]=='sourceless':
        evil=root/'compile-only.py'; evil.write_text("raise RuntimeError('sourceless candidate executed')\n")
        py_compile.compile(str(evil),cfile=str(root/'tools.pyc'),doraise=True); evil.unlink()
    source=root/'tools/probe.py'; source.write_text("VALUE='evil'\n")
    stamp=source.stat().st_mtime
    py_compile.compile(str(source),doraise=True)
    source.write_text("VALUE='safe'\n"); os.utime(source,(stamp,stamp))
    identity={'commit':'a'*40,'sha256':{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (init,source)}}
    for name in tuple(sys.modules):
        if name in ('oc_box','tools') or name.startswith(('oc_box.','tools.')): del sys.modules[name]
    sys.path[:]=[p for p in sys.path if not Path(p or '.').resolve().is_relative_to(real)]
    fixture.APP=root; order=[]; result=[]
    def snapshot(*args): order.append('verified'); return identity
    def load():
        assert order==['verified']
        sys.path[:0]=[str(root/'src'),str(root),str(foreign)]
        result.append(importlib.import_module('tools.probe').VALUE)
        order.append('imported')
    with patch.object(fixture,'source_snapshot',side_effect=snapshot),patch.object(fixture,'load_runtime',side_effect=load):
        if sys.argv[3]=='preloaded-alias':
            spec=importlib.util.spec_from_file_location('preloaded_candidate',source)
            loaded=importlib.util.module_from_spec(spec); sys.modules[spec.name]=loaded
            spec.loader.exec_module(loaded)
            assert loaded.VALUE=='evil'
            try: fixture.capture_binding(root,'a'*40)
            except fixture.FixtureError as error: assert str(error)=='source-shadowing'
            else: raise AssertionError('preloaded bytecode was retroactively attested')
            assert order==['verified']
            print(json.dumps({'refused':True}))
            raise SystemExit(0)
        binding=fixture.capture_binding(root,'a'*40)
    assert result==['safe'] and order==['verified','imported','verified']
    assert binding['source']==identity and sys.dont_write_bytecode
    print(json.dumps({'source_only':True,'ordered':True}))
"""
        for mode in ("root", "foreign", "sourceless", "preloaded-alias"):
            with self.subTest(mode=mode):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        "-c",
                        program,
                        str(APP),
                        str(self.parent),
                        mode,
                    ],
                    env={"PATH": os.defpath},
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    json.loads(result.stdout),
                    {"refused": True}
                    if mode == "preloaded-alias"
                    else {"source_only": True, "ordered": True},
                )

    def test_final_source_refusal_keeps_initial_identity_and_failed_terminal_status(
        self,
    ):
        before = set(self.parent.glob(fixture.PREFIX + "*"))
        binding = {
            "source": {
                "commit": "a" * 40,
                "sha256": {"tools/acceptance_guards.py": "b" * 64},
            },
            "native": {},
        }
        changed = []

        def verify(current):
            self.assertIs(current, binding)
            if changed:
                raise fixture.FixtureError("source-changed")

        def body(*args):
            changed.append(True)
            return {"status": "passed", "source_identity": {"commit": "c" * 40}}

        with (
            patch.object(fixture, "host_guard"),
            patch.object(fixture, "capture_binding", return_value=binding),
            patch.object(fixture, "verify_binding", side_effect=verify),
            patch.object(
                fixture.shutil,
                "which",
                return_value=str(Path(sys.executable).resolve()),
            ),
            patch.object(fixture, "run", side_effect=body),
        ):
            self.assertEqual(
                fixture.main(
                    [
                        "--work-dir",
                        str(self.parent),
                        "--opencode-v2",
                        sys.executable,
                        "--expect-commit",
                        "a" * 40,
                    ]
                ),
                1,
            )
        created = set(self.parent.glob(fixture.PREFIX + "*")) - before
        self.assertEqual(len(created), 1)
        report = fixture.private_json(created.pop(), "result.json")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["source_identity"]["commit"], "a" * 40)
        self.assertEqual(report["executing_binding"]["source"], binding["source"])

    def test_scoped_cache_guard_detects_empty_values_and_effective_policy_authority(
        self,
    ):
        for variable, value in (
            (fixture.credentials._UNLOCKED, ("synthetic", {})),
            (fixture.credentials._EFFECTIVE_POLICY, ("synthetic", {"mode": "vault"})),
        ):
            with self.subTest(variable=variable.name):
                token = None
                try:
                    with self.assertRaisesRegex(
                        fixture.FixtureError, "host-value-retention"
                    ):
                        with fixture.scoped_host_environment():
                            token = variable.set(value)
                finally:
                    if token is not None:
                        variable.reset(token)

    def test_new_failed_terminal_report_and_original_binding_remain_retry_evidence(
        self,
    ):
        path, data, baseline = self.retained_source()
        native = {
            "path": str(Path(sys.executable).resolve()),
            "sha256": "b" * 64,
            "bytes": 1,
        }
        identity = {
            "commit": "a" * 40,
            "commit_count": 140,
            "version": "1.6.111+g" + "a" * 7,
            "dirty": False,
            "sha256": {"tools/acceptance_guards.py": "b" * 64},
        }
        captured = {
            "format": fixture.FORMAT + "-binding-v1",
            "root": str(self.root),
            "source_identity": identity,
            "native": {
                role: dict(native) for role in ("opencode", "podman", "interpreter")
            },
        }
        fixture.write_json_new(self.root / "source-binding.json", captured)
        fixture.terminal_report(
            self.root,
            [],
            primary=fixture.FirstLaunchFailure(
                {"stage": "selected-session"}, True, None
            ),
            identity={"source": identity, "native": captured["native"]},
        )
        before = fixture.source_evidence_digests(self.root, path)
        self.assertIn("source-binding.json", before)
        self.assertIn("result.json", before)
        self.assertEqual(
            fixture.validate_source_retry(self.root)[:4],
            (self.root, path, data, baseline),
        )
        self.assertEqual(fixture.source_evidence_digests(self.root, path), before)
        original = (self.root / "result.json").read_bytes()
        with self.assertRaises(FileExistsError):
            fixture.terminal_report(self.root, [], result={"status": "passed"})
        self.assertEqual((self.root / "result.json").read_bytes(), original)
        captured["source_identity"]["dirty"] = True
        (self.root / "source-binding.json").write_text(json.dumps(captured))
        with self.assertRaisesRegex(fixture.FixtureError, "retry-original-provenance"):
            fixture.validate_source_retry(self.root)

    def test_restored_owner_checks_scoped_environment_and_cache_on_success_and_failure(
        self,
    ):
        for mutation, failing in product(("insert", "change", "cache"), (False, True)):
            with self.subTest(mutation=mutation, failing=failing):
                root = self.parent / (fixture.PREFIX + mutation + str(failing))
                root.mkdir(mode=0o700)
                for relative in (
                    "host-home",
                    "registry/workspaces",
                    "data/containers/storage/volumes",
                    "config/containers",
                    "podman-run",
                    "temporary",
                ):
                    (root / relative).mkdir(parents=True, mode=0o700, exist_ok=True)
                (root / "config/containers/storage.conf").write_text(
                    fixture.expected_storage(root)
                )
                with (
                    patch.object(self, "root", root),
                    patch.dict(os.environ, fixture.isolated_env(root), clear=True),
                ):
                    path, data, baseline = self.retained_source()
                    cache = [False]
                    primary = box.BoxError("private-restored-primary")
                    manifest = {
                        "kind": "full",
                        "source_workspace_id": data["id"],
                        "project": data["project"],
                        "container_project": data["project"],
                        "configuration": {
                            "credentials": {"mode": "vault", "env": [], "vault": []}
                        },
                        "sessions": {"last": baseline["info"]["id"]},
                        "components": dict.fromkeys(
                            ("project.age", "home.age", "registry.age", "image.age")
                        ),
                    }

                    def launch(arguments):
                        if arguments[0] == "restore":
                            destination = root / "restored-full"
                            destination.mkdir()
                            (destination / "unfinished.txt").write_text(
                                "PROJECT_RETAINED\n"
                            )
                            restored_path, restored = box.create_workspace(
                                destination, "tumbleweed"
                            )
                            restored.update(
                                opencode_version="2.2.6",
                                container_project=data["project"],
                                restored_from={
                                    "kind": "full",
                                    "workspace_id": data["id"],
                                },
                                credentials={"mode": "vault", "env": [], "vault": []},
                            )
                            box.atomic_json(restored_path, restored)
                            box.atomic_json(
                                box.session_file(restored_path),
                                {
                                    "sessions": [baseline["info"]["id"]],
                                    "last": baseline["info"]["id"],
                                },
                            )
                        elif arguments[0] == "start":
                            vault.hidden()
                            box.run_tui(["native", "--session", baseline["info"]["id"]])
                            if mutation == "cache":
                                cache[0] = True
                            else:
                                os.environ[
                                    fixture.SIDE if mutation == "insert" else "PATH"
                                ] = "private-restored-value"
                            if failing:
                                raise primary

                    original = dict(os.environ)
                    with contextlib.ExitStack() as stack:
                        for target, name, options in (
                            (
                                fixture.credentials,
                                "has_unlocked_values",
                                {"side_effect": lambda: cache[0]},
                            ),
                            (
                                fixture,
                                "snapshot_source",
                                {
                                    "return_value": {
                                        "home": {"db": "a" * 64},
                                        "registry": {},
                                    }
                                },
                            ),
                            (crypto, "digest", {"return_value": "a" * 64}),
                            (
                                crypto,
                                "small",
                                {"return_value": b"synthetic-age-fixture"},
                            ),
                            (crypto, "validate_recipients", {}),
                            (
                                crypto,
                                "opened",
                                {
                                    "return_value": contextlib.nullcontext(
                                        (manifest, None, None)
                                    )
                                },
                            ),
                            (backups, "validate_manifest", {}),
                            (fixture, "launcher", {"side_effect": launch}),
                            (fixture, "verify_owned_resources", {}),
                            (fixture, "digest_home", {"return_value": "a" * 64}),
                            (fixture, "tree_digest", {"return_value": {}}),
                            (fixture, "os_sentinel", {}),
                            (
                                fixture.workspace_service,
                                "live_connection",
                                {"return_value": {"url": "http://127.0.0.1:12345"}},
                            ),
                            (
                                box,
                                "export_container_session",
                                {"return_value": baseline},
                            ),
                            (fixture, "run_native", {}),
                            (fixture, "permission_matrix", {}),
                        ):
                            stack.enter_context(patch.object(target, name, **options))
                        with self.assertRaises(
                            (box.BoxError, fixture.FixtureError)
                        ) as raised:
                            fixture.full_backup_restore(
                                root,
                                baseline,
                                {},
                                {},
                                12345,
                                "synthetic-password",
                                lambda name: None,
                            )
                    if failing:
                        self.assertIs(raised.exception, primary)
                        self.assertTrue(primary.fixture_secondary)
                    else:
                        self.assertEqual(
                            str(raised.exception),
                            "host-value-retention"
                            if mutation == "cache"
                            else "host-environment-changed",
                        )
                    self.assertEqual(dict(os.environ), original)

    def test_initial_binding_rejects_preloaded_application_before_runtime_loading(self):
        previous = sys.pycache_prefix
        with (
            patch.object(fixture, "source_snapshot", return_value={"commit": "a" * 40}),
            patch.object(fixture, "load_runtime") as load,
        ):
            with self.assertRaisesRegex(fixture.FixtureError, "source-shadowing"):
                fixture.capture_binding(APP, "a" * 40)
        load.assert_not_called()
        self.assertEqual(sys.pycache_prefix, previous)

    def test_isolated_entry_guard_precedes_shadowable_stdlib_imports(self):
        root = self.parent / "entry-guard"
        root.mkdir()
        script = root / "fixture.py"
        script.write_bytes(SCRIPT.read_bytes())
        marker = root / "shadow-executed"
        (root / "argparse.py").write_text(
            "from pathlib import Path\nPath("
            + repr(str(marker))
            + ").write_text('executed')\nraise RuntimeError('private shadow')\n"
        )
        for flags, expected in ((("-B",), 2), (("-I",), 2), (("-I", "-B"), 0)):
            with self.subTest(flags=flags):
                result = subprocess.run(
                    [sys.executable, *flags, str(script), "--help"],
                    env={"PATH": os.defpath},
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertFalse(marker.exists())
                self.assertNotIn("Traceback", result.stderr)

    def test_ignored_root_import_code_refuses_before_loading_candidate_packages(self):
        root = self.parent / "source-proof"
        root.mkdir()
        environment = {
            "PATH": os.defpath,
            "HOME": str(root),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        }

        def git(*args):
            return subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Synthetic Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    *args,
                ],
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=True,
                text=True,
                timeout=15,
            ).stdout.strip()

        git("init", "-q")
        for relative in (*fixture.SOURCE_DONORS, "src/oc_box/oc_box.py"):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# synthetic tracked source, never executed\n")
        git("add", "tools", "src")
        for number in range(30):
            git("commit", "-q", "--allow-empty", "-m", "synthetic " + str(number))
        expected = git("rev-parse", "HEAD")
        (root / ".git/info/exclude").write_text("tools.py\n")
        shim = root / "tools.py"
        shim.write_text(
            "raise RuntimeError('private ignored source must not execute')\n"
        )
        with (
            patch.object(fixture, "APP", root),
            patch.object(fixture, "load_runtime") as load,
        ):
            with self.assertRaisesRegex(fixture.FixtureError, "clean-source-required"):
                fixture.capture_binding(root, expected)
            load.assert_not_called()
            shim.unlink()
            snapshot = fixture.source_snapshot(root, expected)
        self.assertEqual(snapshot["commit"], expected)
        self.assertEqual(snapshot["commit_count"], 30)
        # Switch index/worktree only AFTER a genuine clean status was read.
        actual_run = subprocess.run
        switched = []

        def interleaved(argv, **kwargs):
            result = actual_run(argv, **kwargs)
            if "status" in argv and not switched:
                switched.append(True)
                changed = root / "tools/test_v2_podman.py"
                changed.write_text("# staged after clean status; never executed\n")
                git("add", "tools/test_v2_podman.py")
            return result

        with (
            patch.object(fixture, "APP", root),
            patch.object(subprocess, "run", side_effect=interleaved),
        ):
            with self.assertRaisesRegex(fixture.FixtureError, "source-changed"):
                fixture.source_snapshot(root, expected)
        self.assertEqual(switched, [True])
        # A replacement with the same parent preserves count and a clean status,
        # while making literal A appear to contain the staged B tree.
        tree = git("write-tree")
        parent = git("rev-parse", expected + "^")
        substitute = git(
            "commit-tree", tree, "-p", parent, "-m", "synthetic replacement"
        )
        git("replace", expected, substitute)
        self.assertEqual(git("status", "--porcelain"), "")
        self.assertEqual(git("rev-parse", "HEAD"), expected)
        with patch.object(fixture, "APP", root):
            with self.assertRaisesRegex(fixture.FixtureError, "clean-source-required"):
                fixture.source_snapshot(root, expected)

    def test_module_definitions_are_lazy_and_verified_callers_reuse_runtime_exports(
        self,
    ):
        program = r"""import importlib.util,json,sys
from pathlib import Path
from unittest.mock import patch
script=Path(sys.argv[1]); root=script.parents[1]
spec=importlib.util.spec_from_file_location('late_fixture',script)
fixture=importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)
assert not any(n=='oc_box' or n.startswith('oc_box.') for n in sys.modules)
# This is the explicitly trusted-library seam, not a second initial bootstrap.
sys.path[:0]=[str(root/'src'),str(root)]
from oc_box import oc_box as box
from tools import acceptance_guards as guards
with patch.object(fixture,'capture_binding',side_effect=AssertionError('second bootstrap')):
    assert fixture.box is box and fixture.guards is guards
    assert fixture.vault is box.vault and fixture.credentials is box.credentials
    assert fixture.workspace_service is box.workspace_service
    assert fixture.crypto.__name__=='oc_box.backup_crypto'
    assert fixture.backups.__name__=='oc_box.backups'
    fixture.load_runtime()
print(json.dumps({'lazy':True,'exports':True}))
"""
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", program, str(SCRIPT)],
            env={"PATH": os.defpath},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"lazy": True, "exports": True})

    def private_source_repository(self, name, *, actual=False):
        root = self.parent / name
        root.mkdir(mode=0o700)
        environment = {
            "PATH": os.defpath,
            "HOME": str(root),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        }

        def git(*args):
            return subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Synthetic Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    *args,
                ],
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=True,
                text=True,
                timeout=15,
            ).stdout.strip()

        git("init", "-q")
        if actual:
            shutil.copytree(
                APP / "src/oc_box",
                root / "src/oc_box",
                ignore=shutil.ignore_patterns("__pycache__", "vault-deps"),
            )
            (root / "tools").mkdir()
            for relative in fixture.SOURCE_DONORS:
                shutil.copy2(APP / relative, root / relative)
            shutil.copy2(APP / "pyproject.toml", root / "pyproject.toml")
            git("add", "src", "tools", "pyproject.toml")
        else:
            for relative in (*fixture.SOURCE_DONORS, "src/oc_box/oc_box.py"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# synthetic tracked source, never executed\n")
            git("add", "src", "tools")
        for number in range(30):
            git("commit", "-q", "--allow-empty", "-m", "synthetic " + str(number))
        return root, git, git("rev-parse", "HEAD")

    def test_expected_tree_mode_is_checked_even_when_git_filemode_is_disabled(self):
        root, git, expected = self.private_source_repository("mode-proof")
        git("config", "core.fileMode", "false")
        for relative in (*fixture.SOURCE_DONORS, "src/oc_box/oc_box.py"):
            with self.subTest(relative=relative):
                path = root / relative
                mode = path.stat().st_mode
                path.chmod(mode | 0o100)
                self.assertEqual(git("status", "--porcelain"), "")
                with (
                    patch.object(fixture, "APP", root),
                    patch.object(fixture, "load_runtime") as load,
                ):
                    with self.assertRaisesRegex(fixture.FixtureError, "source-changed"):
                        fixture.source_snapshot(root, expected)
                load.assert_not_called()
                path.chmod(mode)
        with patch.object(fixture, "APP", root):
            self.assertEqual(
                fixture.source_snapshot(root, expected)["commit_count"], 30
            )

    def test_commit_count_remains_bound_to_expected_after_head_advance(self):
        root, git, expected = self.private_source_repository("count-proof")
        actual_run = subprocess.run
        advanced = []

        def interleaved(argv, **kwargs):
            result = actual_run(argv, **kwargs)
            if "status" in argv and not advanced:
                advanced.append(True)
                git("commit", "-q", "--allow-empty", "-m", "advance after status")
            return result

        with (
            patch.object(fixture, "APP", root),
            patch.object(subprocess, "run", side_effect=interleaved),
        ):
            result = fixture.source_snapshot(root, expected)
        self.assertNotEqual(git("rev-parse", "HEAD"), expected)
        self.assertEqual(result["commit"], expected)
        self.assertEqual(result["commit_count"], 30)
        self.assertEqual(result["version"], "1.6.1+g" + expected[:7])

    def test_dangling_original_binding_is_refused_before_retry_allocation(self):
        path, _, _ = self.retained_source()
        self.assertEqual(
            fixture.validate_source_retry(self.root)[:2], (self.root, path)
        )
        before = fixture.source_evidence_digests(self.root, path)
        original = self.root / "source-binding.json"
        original.symlink_to(self.root / "missing-private-binding")
        with (
            patch.object(fixture, "host_guard"),
            patch.object(fixture, "validate_store") as engine,
            patch.object(
                fixture.tempfile,
                "mkdtemp",
                side_effect=AssertionError("attempt allocated before refusal"),
            ) as allocate,
        ):
            with self.assertRaises(fixture.FixtureError):
                fixture.retry_source(self.root)
        engine.assert_not_called()
        allocate.assert_not_called()
        self.assertFalse((self.root / "source-retries").exists())
        self.assertTrue(original.is_symlink())
        self.assertEqual(
            os.readlink(original), str(self.root / "missing-private-binding")
        )
        with self.assertRaises(fixture.FixtureError):
            fixture.source_evidence_digests(self.root, path)
        original.unlink()  # Remove only this test's link, never an evidence target.
        self.assertEqual(fixture.source_evidence_digests(self.root, path), before)

    def test_trace_popen_failure_always_forwards_real_shutdown_and_closes_output(self):
        service = fixture.workspace_service
        for primary in (
            OSError("private-before-child"),
            KeyboardInterrupt("private-before-child"),
        ):
            with self.subTest(kind=type(primary).__name__):
                with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.parent)}):
                    owner = service.Owner(
                        box, self.root / "workspace.json", {"id": "synthetic-trace"}
                    )
                calls = []
                actual_shutdown = service.Owner.shutdown

                def shutdown(current, *args, **kwargs):
                    calls.append((current, args, kwargs))
                    return actual_shutdown(current, *args, **kwargs)

                trace = fixture.SourceRetryTrace()
                previous = service.live_connection()
                with (
                    patch.object(service.Owner, "shutdown", shutdown),
                    patch.object(
                        box,
                        "exec_command",
                        return_value=box.ContainerCommand(["synthetic-no-process"], {}),
                    ),
                    patch.object(service.subprocess, "Popen", side_effect=primary),
                    trace.installed(),
                    self.assertRaises(BaseException) as raised,
                ):
                    owner.__enter__()
                self.assertIs(raised.exception, primary)
                self.assertEqual(len(calls), 1)
                self.assertIs(calls[0][0], owner)
                self.assertEqual(calls[0][1], ())
                self.assertIs(calls[0][2]["primary"], primary)
                self.assertTrue(owner.error_file.closed)
                self.assertFalse(owner.errors_entered)
                self.assertIsNone(owner.errors)
                self.assertIsNone(owner.child)
                self.assertIsNone(owner.token)
                self.assertIs(service.live_connection(), previous)
                self.assertNotIn(
                    "private-before-child",
                    json.dumps(
                        {"failure": trace.failure, "cleanup": trace.cleanup_errors}
                    ),
                )

    def test_observer_status_failures_never_bypass_owned_shutdown(self):
        for error in (
            AttributeError("private-observer"),
            ValueError("private-observer"),
            KeyboardInterrupt("private-observer"),
        ):
            with self.subTest(kind=type(error).__name__):
                owner_class, owner, calls = self.trace_owner(code=None)
                owner.child_owner.status.side_effect = error
                primary = OSError("private-startup-primary")
                trace = fixture.SourceRetryTrace()
                with (
                    patch.object(fixture.workspace_service, "Owner", owner_class),
                    trace.installed(),
                ):
                    try:
                        result = owner.shutdown(primary=primary)
                    except BaseException as observed:
                        self.fail(
                            "observer prevented shutdown: " + type(observed).__name__
                        )
                    self.assertEqual(result, "synthetic cleanup completed")
                self.assertEqual(calls, [((), {"primary": primary})])
                owner.child.poll.assert_not_called()
                self.assertNotIn(
                    "private-observer",
                    json.dumps(
                        {"failure": trace.failure, "cleanup": trace.cleanup_errors}
                    ),
                )

    def test_provider_join_and_liveness_are_independent_under_errors_and_cancellation(
        self,
    ):
        for primary_kind, join_kind in product(
            (None, "body", "cancel"), ("error", "cancel")
        ):
            with self.subTest(primary=primary_kind, join=join_kind):
                calls = []
                primary = (
                    None
                    if primary_kind is None
                    else OSError("private-body")
                    if primary_kind == "body"
                    else KeyboardInterrupt("private-body")
                )
                join_error = (
                    OSError("private-join")
                    if join_kind == "error"
                    else KeyboardInterrupt("private-join")
                )

                def join(**kwargs):
                    calls.append("join")
                    raise join_error

                def live():
                    calls.append("live")
                    return True

                server = SimpleNamespace(
                    shutdown=lambda: calls.append("shutdown"),
                    server_close=lambda: calls.append("close"),
                )
                worker = SimpleNamespace(join=join, is_alive=live)
                if primary is None:
                    with self.assertRaises(BaseException) as raised:
                        fixture.finish_provider(server, worker, primary=None)
                    self.assertIs(raised.exception, join_error)
                    notes = join_error.fixture_secondary
                else:
                    fixture.finish_provider(server, worker, primary=primary)
                    notes = primary.fixture_secondary
                self.assertEqual(calls, ["shutdown", "close", "join", "live"])
                self.assertTrue(any(note.get("check") == "liveness" for note in notes))
                self.assertNotIn("private-", json.dumps(notes))

    def test_retry_preserves_each_first_provider_failure_action_and_all_secondaries(
        self,
    ):
        self.retained_source()
        for first, body_fails in product(("shutdown", "close", "join"), (False, True)):
            with self.subTest(first=first, body_fails=body_fails):
                calls = []

                def action(name):
                    calls.append(name)
                    if ("shutdown", "close", "join").index(name) >= (
                        "shutdown",
                        "close",
                        "join",
                    ).index(first):
                        raise OSError("private-provider-" + name)

                def launch(arguments):
                    vault.hidden()
                    if body_fails:
                        raise box.BoxError("private-launch-primary")
                    return 0

                server = SimpleNamespace(
                    server_port=12345,
                    shutdown=lambda: action("shutdown"),
                    server_close=lambda: action("close"),
                )
                worker = SimpleNamespace(
                    join=lambda **kw: action("join"),
                    is_alive=lambda: calls.append("live") or False,
                )
                with contextlib.ExitStack() as stack:
                    for target, name, options in (
                        (fixture, "host_guard", {}),
                        (fixture, "validate_store", {}),
                        (fixture, "check_native_version", {}),
                        (fixture, "verify_stopped_source", {}),
                        (box, "check_backend", {}),
                        (fixture, "make_provider", {"return_value": (server, worker)}),
                        (fixture, "launcher", {"side_effect": launch}),
                    ):
                        stack.enter_context(patch.object(target, name, **options))
                    report_path, result = fixture.retry_source(self.root)
                self.assertEqual(result["status"], "failed")
                records = [result["error"], *result["cleanup_errors"]]
                expected = {
                    "provider-" + name
                    for name in ("shutdown", "close", "join")[
                        ("shutdown", "close", "join").index(first) :
                    ]
                }
                self.assertTrue(expected <= {item["stage"] for item in records})
                if not body_fails:
                    self.assertEqual(result["error"]["stage"], "provider-" + first)
                else:
                    self.assertEqual(result["error"]["stage"], "owning-launch")
                    self.assertEqual(result["error"]["error_class"], "box-error")
                self.assertEqual(calls, ["shutdown", "close", "join", "live"])
                self.assertNotIn("private-provider-", report_path.read_text())
                self.assertNotIn("private-launch-primary", report_path.read_text())


if __name__ == "__main__":
    unittest.main()
