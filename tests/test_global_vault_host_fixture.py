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
import backups
import vault

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
spec=importlib.util.spec_from_file_location('fixture',sys.argv[1])
fixture=importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)
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
        worker = SimpleNamespace(join=lambda **kwargs: cleanup.append("join"))

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
spec=importlib.util.spec_from_file_location('fixture',sys.argv[1])
fixture=importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)
def fail(root,*args):
    with fixture.first_launch_trace(root) as trace:
        with trace.checkpoint('config-reload'):
            raise KeyError(sys.argv[3])
with patch.object(fixture,'host_guard'), patch.object(fixture.shutil,'which',return_value=sys.executable), patch.object(fixture,'run',side_effect=fail):
    sys.exit(fixture.main(['--work-dir',sys.argv[2],'--opencode-v2',sys.executable]))
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


if __name__ == "__main__":
    unittest.main()
