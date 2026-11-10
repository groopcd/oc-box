import contextlib
import copy
import hashlib
from http.client import HTTPConnection
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from oc_box import oc_box as box, vault
from tools import test_global_vault_podman as cleanup_guards

APP = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "v2_host_fixture", APP / "tools/test_v2_podman.py"
)
assert spec is not None and spec.loader is not None
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class V2HostFixtureTests(unittest.TestCase):
    def test_fixture_refuses_container_or_root_before_any_workspace_access(self):
        with (
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "1"}),
            patch.object(fixture, "run") as run,
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as error,
        ):
            fixture.main(
                ["--opencode-v1", "/example/v1", "--opencode-v2", "/example/v2"]
            )
        self.assertEqual(error.exception.code, 1)
        run.assert_not_called()

    def test_synthetic_credential_names_are_valid_and_pass_env_cannot_leak_real_values(
        self,
    ):
        vault.validate_name(fixture.KEY)
        vault.validate_name(fixture.UNSELECTED)
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "real-provider-fixture",
                "CUSTOM_SECRET": "real-custom-fixture",
                "XDG_DATA_HOME": "/real-host-data",
            },
        ):
            env = fixture.isolated_env(Path("/tmp/example"))
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("CUSTOM_SECRET", env)
        self.assertEqual(env["OPENCODE_AUTH_CONTENT"], "{}")
        self.assertEqual(env["XDG_DATA_HOME"], "/tmp/example/data")
        self.assertEqual(env["XDG_STATE_HOME"], "/tmp/example/state")

    def test_fixture_configuration_contains_only_synthetic_names_and_local_endpoints(
        self,
    ):
        config = fixture.fixture_config(12345)
        self.assertEqual(
            config["provider"]["fixture"]["options"]["apiKey"],
            "{env:" + fixture.KEY + "}",
        )
        self.assertEqual(
            config["provider"]["fixture"]["options"]["baseURL"],
            "http://host.containers.internal:12345/v1",
        )
        self.assertEqual(
            config["mcp"]["fixture"]["environment"],
            {"FIXTURE_MCP_KEY": "{env:" + fixture.KEY + "}"},
        )
        self.assertNotIn("plugin", config)

    def test_native_host_wrapper_uses_isolated_databases_and_no_shared_service_config(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            v1, v2 = root / "v1", root / "v2"
            program = (
                "#!"
                + sys.executable
                + "\nimport json,os,sys; print(json.dumps({'argv':sys.argv[1:],'home':os.environ['HOME'],'config':os.environ['XDG_CONFIG_HOME'],'data':os.environ['XDG_DATA_HOME'],'state':os.environ['XDG_STATE_HOME'],'settings':json.loads(os.environ['OPENCODE_CONFIG_CONTENT']),'db':os.environ.get('OPENCODE_DB')}))\n"
            )
            for file in (v1, v2):
                file.write_text(program)
                file.chmod(0o700)
            wrapper = fixture.write_wrapper(root, v1, v2)
            for major in ("1", "2"):
                (root / "host-major").write_text(major)
                result = subprocess.check_output(
                    [str(wrapper), "session", "export", "ses_fixture"],
                    text=True,
                    env={
                        **os.environ,
                        "OPENCODE_DB": "real-host-fixture.db",
                        "OPENCODE_CONFIG": "/real-host-config",
                    },
                    timeout=10,
                )
                payload = json.loads(result)
                self.assertEqual(payload["argv"], ["session", "export", "ses_fixture"])
                self.assertEqual(payload["data"], str(root / ("host-data-" + major)))
                self.assertEqual(payload["state"], str(root / ("host-state-" + major)))
                self.assertEqual(payload["home"], str(root / "host-home"))
                self.assertIsNone(payload["db"])

    def native_binaries(self, root):
        paths = []
        for major, version in fixture.NATIVE_VERSIONS.items():
            binary = root / ("opencode-" + major)
            binary.write_text(
                "#!"
                + sys.executable
                + "\nprint("
                + repr("opencode v" + version)
                + ")\n"
            )
            binary.chmod(0o700)
            paths.append(binary)
        return paths

    def test_native_cli_setup_and_retained_refresh_verify_both_absolute_versions(self):
        with tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as temporary:
            root = Path(temporary)
            v1, v2 = self.native_binaries(root)
            originals = {path: path.read_bytes() for path in (v1, v2)}
            # Even a shadowing 2.x opencode on PATH must not take the place of
            # the caller's separate native 1.x executable.
            with (
                patch.object(
                    box, "detect_opencode_release", return_value="2.2.6"
                ) as host_detect,
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                wrapper = fixture.prepare_native_cli(root, v1, v2)
                (root / "host-major").write_text("2")
                wrapper.write_text(
                    wrapper.read_text() + "\n# previous generated fixture selector\n"
                )
                fixture.prepare_native_cli(root, v1, v2, resume=True)
            host_detect.assert_not_called()
            self.assertIn("Verified native 1.x 1.19.5", output.getvalue())
            self.assertIn("Verified native 2.x 2.2.6", output.getvalue())
            self.assertEqual((root / "host-major").read_text(), "1")
            self.assertNotIn("previous generated", wrapper.read_text())
            self.assertEqual(
                json.loads((root / "executables.json").read_text()),
                {"1": str(v1), "2": str(v2)},
            )
            for path, before in originals.items():
                self.assertEqual(path.read_bytes(), before)

    def test_native_mismatch_explains_expected_actual_version_and_path_before_changes(
        self,
    ):
        with tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as temporary:
            root = Path(temporary)
            v1, v2 = self.native_binaries(root)
            v1.write_text("#!" + sys.executable + "\nprint('opencode v2.2.6')\n")
            with self.assertRaises(ValueError) as error:
                fixture.prepare_native_cli(root, v1, v2)
            for text in ("expected 1.19.5", "detected 2.2.6", str(v1)):
                self.assertIn(text, str(error.exception))
            self.assertFalse((root / "bin").exists())
            self.assertFalse((root / "host-major").exists())

    def test_native_version_probes_clear_host_settings_and_close_stdin(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.dict(
                    os.environ,
                    {
                        "OPENCODE_CONFIG": "/host-config",
                        "OPENCODE_DB": "real-host-db",
                        "OPENAI_API_KEY": "real-provider",
                    },
                ),
                patch.object(
                    box,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, "1.19.5", ""),
                ) as probe,
            ):
                self.assertEqual(
                    fixture.check_native_version(root, Path("/native/v1"), "1"),
                    "1.19.5",
                )
            self.assertEqual(probe.call_args.args[0], ["/native/v1", "--version"])
            env = probe.call_args.kwargs["env"]
            self.assertNotIn("OPENCODE_DB", env)
            self.assertNotIn("OPENCODE_CONFIG", env)
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertEqual(env["XDG_DATA_HOME"], str(root / "host-data-1"))
            self.assertEqual(probe.call_args.kwargs["stdin"], subprocess.DEVNULL)
            self.assertTrue(probe.call_args.kwargs["private"])

    def test_retained_wrapper_refresh_refuses_unfamiliar_or_symlinked_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            v1, v2 = self.native_binaries(root)
            (root / "bin").mkdir()
            wrapper = root / "bin/opencode"
            wrapper.write_text("user file")
            with self.assertRaisesRegex(ValueError, "unfamiliar"):
                fixture.write_wrapper(root, v1, v2, refresh=True)
            self.assertEqual(wrapper.read_text(), "user file")
            wrapper.unlink()
            wrapper.symlink_to(v1)
            with self.assertRaisesRegex(ValueError, "symlinked"):
                fixture.write_wrapper(root, v1, v2, refresh=True)
            self.assertTrue(wrapper.is_symlink())

    def test_podman_storage_and_runroot_are_both_inside_the_disposable_fixture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture.storage_config(root)
            text = (root / "config/containers/storage.conf").read_text()
            self.assertIn('driver = "overlay"', text)
            self.assertIn("runroot = " + json.dumps(str(root / "podman-run")), text)
            self.assertIn(
                "graphroot = " + json.dumps(str(root / "data/containers/storage")), text
            )

    def test_temp_location_overrides_cached_python_and_podman_environment_then_restores(
        self,
    ):
        with tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as directory:
            root = Path(directory)
            previous_env = os.environ.get("TMPDIR")
            previous_cached = tempfile.tempdir
            with (
                contextlib.redirect_stdout(io.StringIO()),
                fixture.temporary_location(root) as location,
            ):
                self.assertEqual(location, root / "temporary")
                self.assertEqual(tempfile.gettempdir(), str(location))
                self.assertEqual(fixture.isolated_env(root)["TMPDIR"], str(location))
                self.assertEqual(fixture.crypto.environment()["TMPDIR"], str(location))
                self.assertEqual(location.stat().st_mode & 0o777, 0o700)
                with tempfile.TemporaryDirectory() as staging:
                    self.assertEqual(Path(staging).parent, location)
            self.assertEqual(os.environ.get("TMPDIR"), previous_env)
            self.assertEqual(tempfile.tempdir, previous_cached)

    def test_explicit_temp_parent_is_separate_from_retained_fixture_and_is_private(
        self,
    ):
        with (
            tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as fixture_dir,
            tempfile.TemporaryDirectory(prefix="oc-box-temp-parent-") as parent_dir,
        ):
            root, parent = Path(fixture_dir), Path(parent_dir)
            fixture.storage_config(root)
            storage = root / "config/containers/storage.conf"
            before = storage.read_bytes()
            with (
                contextlib.redirect_stdout(io.StringIO()),
                fixture.temporary_location(root, parent) as location,
            ):
                self.assertEqual(location.parent, parent)
                self.assertEqual(location.stat().st_mode & 0o777, 0o700)
                self.assertEqual(fixture.crypto.environment()["TMPDIR"], str(location))
                self.assertEqual(storage.read_bytes(), before)
                (location / "fixture-staging").write_bytes(b"ciphertext fixture")
            self.assertFalse(location.exists())
            self.assertEqual(storage.read_bytes(), before)

    def test_temp_location_refuses_missing_parent_symlink_or_shared_fixture_directory(
        self,
    ):
        with tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as directory:
            root = Path(directory)
            with self.assertRaises(FileNotFoundError):
                fixture.existing_directory(root / "missing")
            (root / "file").write_text("unrelated")
            with self.assertRaisesRegex(ValueError, "existing directory"):
                fixture.existing_directory(root / "file")
            (root / "temporary").symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlinked"):
                with fixture.temporary_location(root):
                    pass
            (root / "temporary").unlink()
            (root / "temporary").mkdir(mode=0o755)
            with self.assertRaisesRegex(ValueError, "private"):
                with fixture.temporary_location(root):
                    pass

    def test_new_fixture_work_parent_selection_does_not_move_existing_storage(self):
        with tempfile.TemporaryDirectory(prefix="oc-box-work-parent-") as temporary:
            parent = Path(temporary)
            with (
                patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}),
                patch.object(os, "geteuid", return_value=1000),
                patch.object(fixture, "run") as run,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    fixture.main(
                        [
                            "--opencode-v1",
                            sys.executable,
                            "--opencode-v2",
                            sys.executable,
                            "--work-dir",
                            str(parent),
                        ]
                    ),
                    0,
                )
            root = run.call_args.args[0]
            self.assertEqual(root.parent, parent)
            self.assertTrue(root.name.startswith(fixture.PREFIX))
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)

    def test_full_restore_retry_temp_override_preserves_backup_and_cleans_only_new_temp_directory(
        self,
    ):
        with (
            tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as fixture_dir,
            tempfile.TemporaryDirectory(
                prefix="oc-box-retry-temp-parent-"
            ) as parent_dir,
        ):
            root, parent = Path(fixture_dir), Path(parent_dir)
            archive = root / "full.ocbox"
            archive.write_bytes(b"original encrypted fixture evidence")
            observed = []

            def retry(selected):
                self.assertEqual(selected, root)
                directory = Path(os.environ["TMPDIR"])
                observed.append(directory)
                self.assertEqual(directory.parent, parent)
                self.assertEqual(fixture.crypto.environment()["TMPDIR"], str(directory))
                self.assertEqual(
                    archive.read_bytes(), b"original encrypted fixture evidence"
                )

            with (
                patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}),
                patch.object(os, "geteuid", return_value=1000),
                patch.object(
                    fixture,
                    "resume_fixture",
                    return_value=(
                        root,
                        Path(sys.executable),
                        Path(sys.executable),
                        "tumbleweed",
                    ),
                ),
                patch.object(fixture, "retry_full_restore", side_effect=retry),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    fixture.main(
                        ["--retry-full-restore", str(root), "--temp-dir", str(parent)]
                    ),
                    0,
                )
            self.assertEqual(len(observed), 1)
            self.assertFalse(observed[0].exists())
            self.assertTrue(root.exists())
            self.assertEqual(
                archive.read_bytes(), b"original encrypted fixture evidence"
            )

    def test_work_parent_cannot_be_used_to_relocate_a_retained_fixture(self):
        with (
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}),
            patch.object(os, "geteuid", return_value=1000),
            patch.object(fixture, "resume_fixture") as resume,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            for option in ("--resume", "--retry-full-restore"):
                with (
                    self.subTest(option=option),
                    self.assertRaises(SystemExit) as error,
                ):
                    fixture.main(
                        [option, "/example/fixture", "--work-dir", "/example/parent"]
                    )
                self.assertEqual(error.exception.code, 2)
            resume.assert_not_called()

    def test_synthetic_provider_requires_current_vault_value_and_exercises_native_tools(
        self,
    ):
        state = {
            "key": "synthetic-current",
            "markers": set(),
            "requests": 0,
            "errors": [],
        }
        server, worker = fixture.make_provider(state)
        self.assertEqual(server.server_address[0], "127.0.0.1")
        try:
            health = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                health.request("GET", "/health")
                response = health.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(
                    json.loads(response.read()), {"fixture": "oc-box-v2-acceptance"}
                )
            finally:
                health.close()

            def request(messages, key):
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                try:
                    connection.request(
                        "POST",
                        "/v1/chat/completions",
                        json.dumps(
                            {
                                "messages": messages,
                                "tools": [
                                    {"function": {"name": name}}
                                    for name in ("shell", "execute")
                                ],
                            }
                        ),
                        {"Authorization": "Bearer " + key},
                    )
                    response = connection.getresponse()
                    content = response.read().decode()
                    return response.status, content
                finally:
                    connection.close()

            user = {"role": "user", "content": "fixture prompt"}
            status, content = request([user], state["key"])
            self.assertEqual(status, 200)
            self.assertIn('"name": "shell"', content)
            self.assertNotIn(state["key"], content)
            status, content = request(
                [user, {"role": "tool", "content": "VAULT_SHELL_OK"}], state["key"]
            )
            self.assertIn("tools.fixture.check", content)
            request(
                [
                    user,
                    {"role": "tool", "content": "VAULT_SHELL_OK"},
                    {"role": "tool", "content": "VAULT_MCP_OK"},
                ],
                state["key"],
            )
            self.assertEqual(state["markers"], {"VAULT_SHELL_OK", "VAULT_MCP_OK"})
            state["key"] = "synthetic-rotated"
            self.assertEqual(request([user], "synthetic-current")[0], 500)
            self.assertTrue(state["errors"])
            self.assertEqual(request([user], state["key"])[0], 200)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)

    def test_native_fixture_request_passes_eof_and_selects_build_after_network_probe(
        self,
    ):
        state = {"requests": 0, "markers": set(), "errors": []}
        with (
            patch.object(
                box, "exec_command", side_effect=lambda data, arguments: arguments
            ),
            patch.object(
                box,
                "run",
                return_value=subprocess.CompletedProcess(
                    [], 0, "HOST_V2_ACCEPTANCE_OK", ""
                ),
            ) as run,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            fixture.run_fixture_session({}, "ses_fixture", state, 12345)
        self.assertEqual(run.call_count, 2)
        health, native = run.call_args_list
        self.assertIn("http://host.containers.internal:12345/health", health.args[0])
        self.assertEqual(health.kwargs["timeout"], 15)
        self.assertEqual(native.kwargs["input"], "")
        self.assertTrue(native.kwargs["private"])
        self.assertIn("--agent", native.args[0])
        self.assertIn("build", native.args[0])

    def test_network_probe_failure_does_not_start_a_native_model_request(self):
        with (
            patch.object(
                box, "exec_command", side_effect=lambda data, arguments: arguments
            ),
            patch.object(
                box, "run", side_effect=box.BoxError("connection failed")
            ) as run,
            self.assertRaisesRegex(
                box.BoxError, "Native model request was not started"
            ),
        ):
            fixture.run_fixture_session({}, "ses_fixture", {}, 12345)
        run.assert_called_once()

    def retained_source(self, root):
        fixture.write_wrapper(root, Path(sys.executable), Path(sys.executable))
        fixture.storage_config(root)
        (root / "config/oc-box").mkdir()
        (root / "config/oc-box/config.json").write_text(
            '{"default_distro":"tumbleweed"}'
        )
        (root / "config/opencode").mkdir()
        (root / "config/opencode/opencode.json").write_text(
            json.dumps(fixture.fixture_config(12345))
        )
        (root / "host-home").mkdir()
        (root / "data/containers/storage").mkdir(parents=True)
        (root / "podman-run").mkdir()
        source = root / "source"
        source.mkdir()
        (source / "unfinished.txt").write_text("PROJECT_RETAINED\n")
        with (
            patch.dict(os.environ, fixture.isolated_env(root), clear=True),
            patch.object(Path, "home", return_value=root / "host-home"),
        ):
            path, data = box.create_workspace(source, "tumbleweed")
        (root / "fixture-ids.json").write_text(json.dumps([data["id"]]))
        return path, data

    def test_resume_validates_only_stopped_source_and_reuses_native_paths(self):
        with tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as temporary:
            root = Path(temporary)
            path, data = self.retained_source(root)
            with patch.object(
                fixture, "stopped_container", return_value={"State": {"Running": False}}
            ):
                recovered, v1, v2, distro = fixture.resume_fixture(root)
            self.assertEqual(recovered, root)
            self.assertEqual(v1, Path(sys.executable).resolve())
            self.assertEqual(v1, v2)
            self.assertEqual(distro, "tumbleweed")
            self.assertEqual(box.read_json(path)["id"], data["id"])
            (root / "full.ocbox").write_bytes(b"fixture archive evidence")
            with self.assertRaisesRegex(
                ValueError, "overwrite backup/restore evidence"
            ):
                fixture.resume_fixture(root)
            self.assertEqual(
                (root / "full.ocbox").read_bytes(), b"fixture archive evidence"
            )

    def test_resume_refuses_changed_storage_or_external_project_and_preserves_files(
        self,
    ):
        with tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as temporary:
            root = Path(temporary)
            path, data = self.retained_source(root)
            storage = root / "config/containers/storage.conf"
            original = storage.read_text()
            storage.write_text('[storage]\ngraphroot="/external/storage"\n')
            with self.assertRaisesRegex(ValueError, "expected isolated graphroot"):
                fixture.resume_fixture(root)
            self.assertIn("/external/storage", storage.read_text())
            storage.write_text(original)
            data["project"] = str(root.parent / "real-project")
            box.atomic_json(path, data)
            with self.assertRaisesRegex(ValueError, "isolated source/home"):
                fixture.resume_fixture(root)
            self.assertEqual(box.read_json(path)["project"], data["project"])

    def test_full_restore_retry_accepts_only_existing_private_backup_and_missing_destination(
        self,
    ):
        with tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as temporary:
            root = Path(temporary)
            self.retained_source(root)
            archive, identity = root / "full.ocbox", root / "age-identity.txt"
            for file in (archive, identity):
                file.write_bytes(b"retained fixture evidence")
                file.chmod(0o600)
            with patch.object(
                fixture, "stopped_container", return_value={"State": {"Running": False}}
            ):
                self.assertEqual(
                    fixture.resume_fixture(root, full_restore=True)[0], root
                )
            with self.assertRaisesRegex(
                ValueError, "overwrite backup/restore evidence"
            ):
                fixture.resume_fixture(root)
            (root / "restored-full").mkdir()
            with self.assertRaisesRegex(
                ValueError, "overwrite backup/restore evidence"
            ):
                fixture.resume_fixture(root, full_restore=True)
            self.assertEqual(archive.read_bytes(), b"retained fixture evidence")

    def test_full_restore_retry_refuses_insecure_identity_before_any_image_operation(
        self,
    ):
        with tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as temporary:
            root = Path(temporary)
            self.retained_source(root)
            for name in ("full.ocbox", "age-identity.txt"):
                (root / name).write_bytes(b"retained fixture evidence")
                (root / name).chmod(0o600)
            (root / "age-identity.txt").chmod(0o644)
            with self.assertRaisesRegex(ValueError, "private regular fixture files"):
                fixture.resume_fixture(root, full_restore=True)

    def test_full_restore_retry_authenticates_before_restore_and_preserves_encrypted_evidence(
        self,
    ):
        with tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as temporary:
            root = Path(temporary)
            path, data = self.retained_source(root)
            archive = root / "full.ocbox"
            archive.write_bytes(b"retained encrypted archive evidence")
            calls = []
            manifest = {
                "kind": "full",
                "source_workspace_id": data["id"],
                "project": data["project"],
            }
            fresh = {
                **data,
                "id": "f" * 32,
                "project": str(root / "restored-full"),
                "container_project": data["project"],
            }
            with (
                patch.object(
                    fixture,
                    "resume_fixture",
                    return_value=(
                        root,
                        Path(sys.executable),
                        Path(sys.executable),
                        "tumbleweed",
                    ),
                ),
                patch.object(fixture, "workspace", return_value=(path, data)),
                patch.object(fixture, "stopped_container"),
                patch.object(
                    fixture.crypto,
                    "opened",
                    return_value=contextlib.nullcontext(
                        (manifest, root / "encrypted", b"private fixture key")
                    ),
                ),
                patch.object(fixture.backups, "validate_manifest"),
                patch.object(
                    fixture.crypto,
                    "decrypt_stream",
                    side_effect=lambda *args: calls.append(("authenticate", args)),
                ),
                patch.object(
                    fixture.backups,
                    "restore_backup",
                    side_effect=lambda *args: calls.append(("restore", args)),
                ),
                patch.object(
                    box,
                    "find_workspace",
                    return_value=(root / "new/workspace.json", fresh),
                ),
                patch.object(
                    box, "session_state", return_value={"last": fixture.LEGACY_ID}
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                fixture.retry_full_restore(root)
            self.assertEqual([call[0] for call in calls], ["authenticate", "restore"])
            self.assertEqual(calls[0][1][0], root / "encrypted/image.age")
            self.assertNotIn("podman", calls[0][1][2])
            args = calls[1][1][1]
            self.assertEqual(args.file, archive)
            self.assertEqual(args.project, root / "restored-full")
            self.assertEqual(
                archive.read_bytes(), b"retained encrypted archive evidence"
            )
            self.assertIn(
                fresh["id"], json.loads((root / "fixture-ids.json").read_text())
            )
            self.assertEqual(
                json.loads((root / "full-restore-result.json").read_text())["status"],
                "restored",
            )

    def test_full_restore_authentication_failure_never_attempts_image_loading(self):
        with tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as temporary:
            root = Path(temporary)
            path, data = self.retained_source(root)
            archive = root / "full.ocbox"
            archive.write_bytes(b"retained encrypted archive evidence")
            manifest = {
                "kind": "full",
                "source_workspace_id": data["id"],
                "project": data["project"],
            }
            with (
                patch.object(
                    fixture,
                    "resume_fixture",
                    return_value=(
                        root,
                        Path(sys.executable),
                        Path(sys.executable),
                        "tumbleweed",
                    ),
                ),
                patch.object(fixture, "workspace", return_value=(path, data)),
                patch.object(fixture, "stopped_container"),
                patch.object(
                    fixture.crypto,
                    "opened",
                    return_value=contextlib.nullcontext(
                        (manifest, root / "encrypted", b"private fixture key")
                    ),
                ),
                patch.object(fixture.backups, "validate_manifest"),
                patch.object(
                    fixture.crypto,
                    "decrypt_stream",
                    side_effect=fixture.crypto.BackupError(
                        "fixture authentication failed"
                    ),
                ),
                patch.object(fixture.backups, "restore_backup") as restore,
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(
                    fixture.crypto.BackupError, "authentication failed"
                ),
            ):
                fixture.retry_full_restore(root)
            restore.assert_not_called()
            self.assertEqual(
                archive.read_bytes(), b"retained encrypted archive evidence"
            )
            self.assertFalse((root / "full-restore-result.json").exists())

    def cleanup_state(self, root, *, restored=False):
        """Independent synthetic authorization files and command-routed engine."""
        path, source = self.retained_source(root)
        records = [(path, source)]
        if restored:
            with (
                patch.dict(os.environ, fixture.isolated_env(root), clear=True),
                patch.object(Path, "home", return_value=root / "host-home"),
            ):
                for kind in ("full", "portable"):
                    project = root / ("restored-" + kind)
                    project.mkdir()
                    item_path, data = box.create_workspace(project, "tumbleweed")
                    data.update(
                        container_project=source["project"],
                        restored_from={"workspace_id": source["id"], "kind": kind},
                    )
                    box.atomic_json(item_path, data)
                    records.append((item_path, data))
        (root / "fixture-ids.json").write_text(
            json.dumps([data["id"] for _, data in records])
        )
        (root / "fixture-ids.json").chmod(0o600)
        for name in (
            "podman-tmp",
            "data/libpod",
            "data/containers/storage/volumes",
            "temporary",
        ):
            (root / name).mkdir(parents=True, mode=0o700, exist_ok=True)
        (root / "config/containers/storage.conf").chmod(0o600)
        cleanup_guards.write_json_new(
            root / "cleanup-purpose.json",
            {
                "format": "oc-box-v2-cleanup-v1",
                "root": str(root),
                "identity": {
                    "project_identity": list(box.workspace_identity.snapshot(root)[0])
                },
                "boot_id": "11111111-2222-3333-4444-555555555555\n",
            },
        )
        (root / "podman-tmp/alive").write_text("11111111-2222-3333-4444-555555555555\n")
        (root / "podman-tmp/alive").chmod(0o600)
        config = root / "config/containers/containers.conf"
        config.write_text(cleanup_guards.containers_config(root))
        config.chmod(0o600)
        cleanup_guards.prepare_podman_cli(root, Path(sys.executable).resolve())
        resources = {}
        for _, data in records:
            volume = (
                root / "data/containers/storage/volumes" / data["home_volume"] / "_data"
            )
            volume.mkdir(parents=True)
            resources[("volume", data["home_volume"])] = {
                "Name": data["home_volume"],
                "Labels": {box.LABEL: data["id"]},
                "Mountpoint": str(volume),
            }
            for kind in ("container", "network"):
                token = hashlib.sha256((kind + data["id"]).encode()).hexdigest()
                info = {
                    "Id": token,
                    "Name": "oc-box-" + data["id"],
                    "Labels": {box.LABEL: data["id"]},
                }
                if kind == "container":
                    info.update(
                        Config={"Labels": {box.LABEL: data["id"]}},
                        HostConfig={"PortBindings": {}},
                        Mounts=[
                            {
                                "Type": "bind",
                                "Source": data["project"],
                                "Destination": box.container_project(data),
                            },
                            {
                                "Type": "volume",
                                "Name": data["home_volume"],
                                "Source": str(volume),
                                "Destination": data["home"],
                            },
                        ],
                    )
                    info.pop("Labels")
                resources[(kind, token)] = info
        info = {
            "host": {"security": {"rootless": True}, "serviceIsRemote": False},
            "store": {
                "graphDriverName": "overlay",
                "graphRoot": str(root / "data/containers/storage"),
                "runRoot": str(root / "podman-run"),
                "volumePath": str(root / "data/containers/storage/volumes"),
                "imageCopyTmpDir": str(root / "temporary"),
                "configFile": str(root / "config/containers/storage.conf"),
            },
            "version": {"Version": "6.1.4"},
        }
        return records, resources, info

    @contextlib.contextmanager
    def cleanup_engine(
        self, root, records, resources, info, *, intercept=None, lock_action=None
    ):
        events, removals, held = [], [], set()
        lock = box.workspace_lock

        @contextlib.contextmanager
        def locked(path):
            with lock(path):
                held.add(path)
                try:
                    if lock_action is not None:
                        lock_action(path)
                    yield
                finally:
                    held.remove(path)

        def command(argv, **options):
            args = argv[1:]
            events.append(tuple(args))
            self.assertEqual(held, {path for path, _ in records})
            self.assertEqual(options["stdin"], subprocess.DEVNULL)
            self.assertTrue(options["private"])
            env = options["env"]
            self.assertEqual(
                env["CONTAINERS_STORAGE_CONF"],
                str(root / "config/containers/storage.conf"),
            )
            self.assertEqual(
                env["CONTAINERS_CONF"], str(root / "config/containers/containers.conf")
            )
            self.assertNotIn("CONTAINERS_CONF_OVERRIDE", env)
            if intercept is not None:
                result = intercept(argv, options, events)
                if result is not None:
                    return result
            if args[0] == "info":
                return subprocess.CompletedProcess(argv, 0, json.dumps(info), "")
            if args[0] == "ps" or args[1] == "ls":
                kind = "container" if args[0] == "ps" else args[0]
                ident = args[-1].split("=", 2)[-1]
                tokens = [
                    token
                    for (item_kind, token), value in resources.items()
                    if item_kind == kind
                    and value.get(
                        "Labels", value.get("Config", {}).get("Labels", {})
                    ).get(box.LABEL)
                    == ident
                ]
                return subprocess.CompletedProcess(argv, 0, "\n".join(tokens), "")
            kind, action, token = args[:3]
            if action == "exists":
                found = any(
                    k == kind and value["Name"].lstrip("/") == token
                    for (k, _), value in resources.items()
                )
                return subprocess.CompletedProcess(argv, 0 if found else 1, "", "")
            if action == "inspect":
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps([resources[(kind, token)]]), ""
                )
            self.assertEqual(action, "rm")
            token = args[-1]
            removals.append((kind, token))
            resources.pop((kind, token))
            return subprocess.CompletedProcess(argv, 0, "", "")

        with (
            patch.object(box, "run", side_effect=command),
            patch.object(box, "workspace_lock", side_effect=locked),
            patch.object(
                box,
                "remove_resources",
                side_effect=lambda data: removals.append(("old", data["id"])),
            ),
            patch.object(
                cleanup_guards,
                "system_boot_id",
                return_value=b"11111111-2222-3333-4444-555555555555\n",
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            yield events, removals

    def assert_cleanup_refuses(
        self, root, records, resources, info, *, file_only=False
    ):
        before = {
            path: path.read_bytes()
            for path in root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        failure = None
        with self.cleanup_engine(root, records, resources, info) as (events, removals):
            try:
                fixture.cleanup(root)
            except ValueError as error:
                failure = error
        self.assertEqual(removals, [], "a cleanup refusal removed resources")
        if file_only:
            self.assertEqual(events, [], "a file-only refusal reached the engine")
        self.assertIsNotNone(failure, "cleanup accepted unauthorized state")
        self.assertNotIn("private-sentinel", str(failure))
        for path, content in before.items():
            self.assertEqual(
                path.read_bytes(), content, "cleanup rewrote retained evidence"
            )

    def test_fixture_cleanup_refuses_projects_outside_fixture_root(self):
        with tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as temporary:
            root = Path(temporary)
            records, resources, info = self.cleanup_state(root)
            path, data = records[0]
            data["project"] = str(root.parent / "synthetic-unrelated-project")
            box.atomic_json(path, data)
            self.assert_cleanup_refuses(root, records, resources, info, file_only=True)

    def test_cleanup_journal_requires_exact_nonempty_unique_inventory(self):
        for name, journal in {
            "empty": [],
            "unrecorded": ["f" * 32],
            "duplicate": None,
            "object": {},
            "invalid-id": ["not-an-id"],
            "nonstring": [1],
            "missing-record": None,
            "malformed-json": "{",
        }.items():
            with (
                tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as temporary,
                self.subTest(name=name),
            ):
                root = Path(temporary)
                records, resources, info = self.cleanup_state(root)
                ident = records[0][1]["id"]
                if name == "duplicate":
                    journal = [ident, ident]
                elif name == "missing-record":
                    journal = [ident, "f" * 32]
                (root / "fixture-ids.json").write_text(
                    journal if name == "malformed-json" else json.dumps(journal)
                )
                self.assert_cleanup_refuses(
                    root, records, resources, info, file_only=True
                )

    def test_cleanup_file_guards_refuse_before_engine_and_preserve_evidence(self):
        for name in (
            "storage",
            "engine-config",
            "registry-link",
            "selector",
            "missing-purpose",
            "missing-boot",
            "invalid-boot",
            "stale-boot",
            "shared-root",
            "shared-journal",
            "journal-hardlink",
            "project-replaced",
            "purpose-boot",
            "mapping",
            "registry-descendant-link",
            "storage-link",
            "selector-link",
        ):
            with (
                tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as temporary,
                self.subTest(name=name),
            ):
                root = Path(temporary)
                records, resources, info = self.cleanup_state(root)
                if name == "storage":
                    (root / "config/containers/storage.conf").write_text(
                        '[storage]\ngraphroot="/synthetic-external"\n'
                    )
                elif name == "engine-config":
                    (root / "config/containers/containers.conf").write_text(
                        "[engine]\nremote=true\n"
                    )
                elif name == "registry-link":
                    (root / "registry").rename(root / "redirected-registry")
                    (root / "registry").symlink_to(
                        root / "redirected-registry", target_is_directory=True
                    )
                elif name == "selector":
                    (root / "bin/podman").write_text("#!/bin/sh\nexit 0\n")
                elif name == "missing-purpose":
                    (root / "cleanup-purpose.json").unlink()
                elif name == "missing-boot":
                    (root / "podman-tmp/alive").unlink()
                elif name in ("invalid-boot", "stale-boot"):
                    (root / "podman-tmp/alive").write_text(
                        "invalid"
                        if name == "invalid-boot"
                        else "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n"
                    )
                elif name == "shared-root":
                    root.chmod(0o755)
                elif name == "shared-journal":
                    (root / "fixture-ids.json").chmod(0o644)
                elif name == "journal-hardlink":
                    os.link(root / "fixture-ids.json", root / "journal-copy")
                elif name == "purpose-boot":
                    path = root / "cleanup-purpose.json"
                    purpose = json.loads(path.read_text())
                    purpose["boot_id"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n"
                    path.write_text(json.dumps(purpose))
                elif name == "mapping":
                    (root / "podman-executable.json").write_text(
                        '{"podman":"private-sentinel"}'
                    )
                elif name == "registry-descendant-link":
                    records[0][0].with_name("foreign-link").symlink_to(
                        root / "cleanup-purpose.json"
                    )
                elif name == "storage-link":
                    config = root / "config/containers/storage.conf"
                    config.rename(config.with_name("retained-storage.conf"))
                    config.symlink_to(config.with_name("retained-storage.conf"))
                elif name == "selector-link":
                    wrapper = root / "bin/podman"
                    wrapper.rename(wrapper.with_name("retained-podman"))
                    wrapper.symlink_to(wrapper.with_name("retained-podman"))
                else:
                    (root / "source").rename(root / "original-source")
                    (root / "source").mkdir()
                    (root / "source" / box.MARKER).write_text(records[0][1]["id"])
                self.assert_cleanup_refuses(
                    root, records, resources, info, file_only=True
                )

    def test_cleanup_invalid_later_record_never_removes_an_earlier_workspace(self):
        for name in ("project", "identity", "origin", "home", "resource-name"):
            with (
                tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as temporary,
                self.subTest(name=name),
            ):
                root = Path(temporary)
                records, resources, info = self.cleanup_state(root, restored=True)
                path, data = max(records, key=lambda item: item[0])
                if name == "project":
                    data["project"] = str(root / "unrecorded-project")
                elif name == "identity":
                    data["project_identity"][1] += 1
                elif name == "origin":
                    data["restored_from"] = {"kind": "full", "workspace_id": "f" * 32}
                elif name == "home":
                    data["home"] = "/synthetic-external-home"
                else:
                    data["container"] = "oc-box-foreign"
                box.atomic_json(path, data)
                self.assert_cleanup_refuses(
                    root, records, resources, info, file_only=True
                )

    def test_cleanup_actual_store_refusals_precede_all_resource_queries(self):
        for name in (
            "rootful",
            "remote",
            "graphRoot",
            "runRoot",
            "volumePath",
            "configFile",
            "imageCopyTmpDir",
            "unknown-omission",
        ):
            with (
                tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as temporary,
                self.subTest(name=name),
            ):
                root = Path(temporary)
                records, resources, info = self.cleanup_state(root)
                if name == "rootful":
                    info["host"]["security"]["rootless"] = False
                elif name == "remote":
                    info["host"]["serviceIsRemote"] = True
                elif name == "unknown-omission":
                    del info["store"]["configFile"]
                    info["version"]["Version"] = "999.0.0"
                else:
                    info["store"][name] = "/synthetic-external-store"
                with self.cleanup_engine(root, records, resources, info) as (
                    events,
                    removals,
                ):
                    failure = None
                    try:
                        fixture.cleanup(root)
                    except ValueError as error:
                        failure = error
                self.assertEqual(removals, [])
                self.assertIsNotNone(failure)
                self.assertTrue(events)
                self.assertTrue(all(event[0] == "info" for event in events))

    def test_cleanup_prevalidates_every_resource_before_first_removal(self):
        for name in (
            "label",
            "project-mount",
            "home-mount",
            "mount-type",
            "duplicate-mount",
            "published-port",
            "volume-path",
            "network-name",
            "volume-label",
            "extra-labelled-resource",
        ):
            with (
                tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as temporary,
                self.subTest(name=name),
            ):
                root = Path(temporary)
                records, resources, info = self.cleanup_state(root, restored=True)
                data = records[-1][1]
                if name == "extra-labelled-resource":
                    token = "f" * 64
                    resources[("network", token)] = {
                        "Id": token,
                        "Name": "foreign-network",
                        "Labels": {box.LABEL: data["id"]},
                    }
                elif name == "volume-label":
                    resources[("volume", data["home_volume"])]["Labels"][box.LABEL] = (
                        "f" * 32
                    )
                elif name == "volume-path":
                    resources[("volume", data["home_volume"])]["Mountpoint"] = (
                        "/synthetic-external-volume"
                    )
                else:
                    kind = "network" if name == "network-name" else "container"
                    value = next(
                        value
                        for (k, _), value in resources.items()
                        if k == kind and value["Name"] == data["container"]
                    )
                    if name == "label":
                        value["Config"]["Labels"][box.LABEL] = "f" * 32
                    elif name == "project-mount":
                        value["Mounts"][0]["Source"] = "/synthetic-foreign-project"
                    elif name == "home-mount":
                        value["Mounts"].pop()
                    elif name == "mount-type":
                        value["Mounts"][1]["Type"] = "bind"
                    elif name == "duplicate-mount":
                        value["Mounts"].append(copy.deepcopy(value["Mounts"][0]))
                    elif name == "published-port":
                        value["HostConfig"]["PortBindings"] = {"80/tcp": [{}]}
                    else:
                        value["Name"] = "foreign-network"
                self.assert_cleanup_refuses(root, records, resources, info)

    def test_cleanup_holds_all_locks_and_removes_only_prevalidated_resources(self):
        for omitted_config in (False, True):
            with (
                tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as temporary,
                self.subTest(omitted_config=omitted_config),
            ):
                root = Path(temporary)
                records, resources, info = self.cleanup_state(root, restored=True)
                if omitted_config:
                    del info["store"]["configFile"]
                expected = set(resources)
                with self.cleanup_engine(root, records, resources, info) as (
                    events,
                    removals,
                ):
                    fixture.cleanup(root)
                    self.assertEqual(set(removals), expected)
                    first_removal = next(
                        index
                        for index, event in enumerate(events)
                        if len(event) > 1 and event[1] == "rm"
                    )
                    self.assertFalse(
                        any(
                            len(event) > 1 and event[1] == "inspect"
                            for event in events[first_removal:]
                        )
                    )
                    fixture.cleanup(root)
                    self.assertEqual(len(removals), len(expected))
                self.assertEqual(resources, {})

    def test_cleanup_reloads_under_all_locks_and_refuses_contention_before_engine(self):
        for name in ("metadata-change", "lock-refusal"):
            with (
                tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as temporary,
                self.subTest(name=name),
            ):
                root = Path(temporary)
                records, resources, info = self.cleanup_state(root, restored=True)
                count = []

                def at_lock(path):
                    count.append(path)
                    if len(count) == len(records):
                        if name == "lock-refusal":
                            raise box.BoxError("private-sentinel lock held")
                        data = box.read_json(path)
                        data["pids"] += 1
                        box.atomic_json(path, data)

                with self.cleanup_engine(
                    root, records, resources, info, lock_action=at_lock
                ) as (events, removals):
                    with self.assertRaises(fixture.CleanupRefusal) as error:
                        fixture.cleanup(root)
                self.assertEqual(events, [])
                self.assertEqual(removals, [])
                self.assertNotIn("private-sentinel", str(error.exception))

    def test_cleanup_late_preflight_errors_are_fixed_and_remove_nothing(self):
        for name in ("invalid-info", "engine-error", "final-store", "final-metadata"):
            with (
                tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as temporary,
                self.subTest(name=name),
            ):
                root = Path(temporary)
                records, resources, info = self.cleanup_state(root, restored=True)

                def intercept(argv, options, events):
                    if argv[1] != "info":
                        return None
                    second = sum(event[0] == "info" for event in events) == 2
                    if name == "invalid-info" or (name == "final-store" and second):
                        return subprocess.CompletedProcess(
                            argv, 0, "private-sentinel malformed JSON", ""
                        )
                    if name == "engine-error":
                        raise box.BoxError("private-sentinel private engine stderr")
                    if name == "final-metadata" and second:
                        path, data = records[-1]
                        changed = copy.deepcopy(data)
                        changed["pids"] += 1
                        box.atomic_json(path, changed)
                    return None

                with self.cleanup_engine(
                    root, records, resources, info, intercept=intercept
                ) as (_events, removals):
                    with self.assertRaises(fixture.CleanupRefusal) as error:
                        fixture.cleanup(root)
                self.assertEqual(removals, [])
                self.assertNotIn("private-sentinel", str(error.exception))

    def test_cleanup_operational_partial_removal_retries_only_known_remaining_resources(
        self,
    ):
        with tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as temporary:
            root = Path(temporary)
            records, resources, info = self.cleanup_state(root, restored=True)
            expected = set(resources)
            journal = (root / "fixture-ids.json").read_bytes()

            def intercept(argv, options, events):
                if (
                    "rm" in argv
                    and sum(len(event) > 1 and event[1] == "rm" for event in events)
                    == 2
                ):
                    return subprocess.CompletedProcess(
                        argv, 1, "private-sentinel", "private-sentinel"
                    )
                return None

            with self.cleanup_engine(
                root, records, resources, info, intercept=intercept
            ) as (_events, first):
                with self.assertRaises(fixture.CleanupRefusal) as error:
                    fixture.cleanup(root)
            self.assertEqual(len(first), 1)
            self.assertNotIn("private-sentinel", str(error.exception))
            with self.cleanup_engine(root, records, resources, info) as (
                _events,
                remaining,
            ):
                fixture.cleanup(root)
            self.assertEqual(set(first + remaining), expected)
            self.assertEqual(len(first + remaining), len(expected))
            self.assertEqual(resources, {})
            self.assertEqual((root / "fixture-ids.json").read_bytes(), journal)

    def test_fresh_cleanup_authorization_is_file_only_and_never_replaces_existing_proofs(
        self,
    ):
        with tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as temporary:
            root = Path(temporary)
            (root / "bin").mkdir()
            fixture.storage_config(root)
            with (
                patch.object(
                    fixture.shutil,
                    "which",
                    return_value=str(Path(sys.executable).resolve()),
                ),
                patch.object(
                    cleanup_guards,
                    "system_boot_id",
                    return_value=b"11111111-2222-3333-4444-555555555555\n",
                ),
                patch.object(
                    box, "run", side_effect=AssertionError("engine must not run")
                ) as engine,
            ):
                fixture.prepare_cleanup_authorization(root)
                purpose = (root / "cleanup-purpose.json").read_bytes()
                self.assertFalse((root / "podman-tmp/alive").exists())
                with self.assertRaises(FileExistsError):
                    fixture.prepare_cleanup_authorization(root)
                self.assertEqual((root / "cleanup-purpose.json").read_bytes(), purpose)
                engine.assert_not_called()
            self.assertEqual(
                (root / "cleanup-purpose.json").stat().st_mode & 0o777, 0o600
            )
            self.assertEqual(
                (root / "bin/podman").read_text(),
                cleanup_guards.podman_wrapper_source(),
            )

    def test_v1_history_check_allows_access_metadata_but_rejects_changed_parts_or_identity(
        self,
    ):
        expected = fixture.legacy_session(Path("/synthetic/project"))
        current = json.loads(json.dumps(expected))
        current["info"]["time"]["updated"] = 100
        current["info"]["title"] = "Metadata updated by native V1"
        fixture.verify_v1_history(current, expected)
        for change in ("text", "message-id", "session-id", "path"):
            current = json.loads(json.dumps(expected))
            if change == "text":
                current["messages"][0]["parts"][0]["text"] = "lost history"
            elif change == "message-id":
                current["messages"][0]["info"]["id"] = "msg_changed"
            elif change == "session-id":
                current["info"]["id"] = "ses_changed"
            else:
                current["info"]["directory"] = "/another/project"
            with (
                self.subTest(change=change),
                self.assertRaises((AssertionError, box.BoxError)),
            ):
                fixture.verify_v1_history(current, expected)

    def test_v2_progress_check_requires_exact_retained_messages_and_allows_new_progress(
        self,
    ):
        expected = {
            "info": {
                "id": fixture.LEGACY_ID,
                "location": {"directory": "/synthetic/project"},
            },
            "messages": [{"id": "msg_original", "type": "user", "text": "original"}],
        }
        current = json.loads(json.dumps(expected))
        current["messages"].append(
            {"id": "msg_new", "type": "user", "text": "new progress"}
        )
        fixture.verify_v2_progress(current, expected)
        current["messages"][0]["text"] = "changed"
        with self.assertRaisesRegex(AssertionError, "retained native history"):
            fixture.verify_v2_progress(current, expected)
        current["messages"] = []
        with self.assertRaisesRegex(AssertionError, "retained native history"):
            fixture.verify_v2_progress(current, expected)

    def test_superseded_v1_reopen_requires_original_unfinished_and_later_messages(self):
        expected = fixture.superseded_legacy_session(Path("/synthetic/project"))
        current = json.loads(json.dumps(expected))
        current["info"]["time"]["updated"] = 100
        fixture.verify_v1_history(current, expected)
        for change in (
            "drop-unfinished",
            "complete-unfinished",
            "change-later",
            "reorder",
        ):
            current = json.loads(json.dumps(expected))
            if change == "drop-unfinished":
                del current["messages"][1]
            elif change == "complete-unfinished":
                current["messages"][1]["info"]["time"]["completed"] = 2
            elif change == "change-later":
                current["messages"][3]["parts"][0]["text"] = "changed retained turn"
            else:
                current["messages"][1:3] = reversed(current["messages"][1:3])
            with (
                self.subTest(change=change),
                self.assertRaisesRegex(AssertionError, "message/part history"),
            ):
                fixture.verify_v1_history(current, expected)

    def superseded_wire(self):
        """Minimal native wire values for testing the host runner's oracles only."""
        data = {
            "id": "a" * 32,
            "project": "/synthetic/project",
            "opencode_version": "2.2.6",
        }
        messages = [
            {"id": "msg_v1_superseded_1", "type": "user", "text": "first turn"},
            {"id": "msg_v1_superseded_2", "type": "assistant", "time": {"created": 2}},
            {"id": "msg_v1_superseded_3", "type": "user", "text": "later turn"},
            {
                "id": "msg_v1_superseded_4",
                "type": "assistant",
                "time": {"completed": 4},
            },
        ]
        page = {"data": messages, "cursor": {"next": None}}
        raw = {
            "info": {
                "id": fixture.SUPERSEDED_ID,
                "location": {"directory": data["project"]},
            },
            "messages": [messages[index] for index in (0, 2, 3)],
        }
        checked = json.loads(json.dumps(raw))
        checked[fixture.history_io.INTERRUPTED] = {"assistant": 1}
        return data, page, raw, checked

    def test_superseded_migration_oracle_requires_original_projection_shape(self):
        data, page, raw, checked = self.superseded_wire()
        with (
            patch.object(
                fixture.workspace_service, "api", side_effect=[page, {"data": raw}]
            ),
            patch.object(
                box, "export_container_session", return_value=checked
            ) as export,
        ):
            self.assertEqual(fixture.verify_superseded_migration(data, {}), checked)
        export.assert_called_once_with(data, fixture.SUPERSEDED_ID)
        for change in (
            "missing-message",
            "completed-first",
            "unfinished-tail",
            "identity",
            "order",
            "next-page",
        ):
            data, page, _, _ = self.superseded_wire()
            if change == "missing-message":
                del page["data"][1]
            elif change == "completed-first":
                page["data"][1]["time"]["completed"] = 2
            elif change == "unfinished-tail":
                page["data"][3]["time"].pop("completed")
            elif change == "identity":
                page["data"][1]["id"] = "msg_foreign"
            elif change == "order":
                page["data"][1:3] = reversed(page["data"][1:3])
            else:
                page["cursor"]["next"] = "more"
            with (
                self.subTest(change=change),
                patch.object(fixture.workspace_service, "api", return_value=page),
                patch.object(box, "export_container_session") as export,
                self.assertRaisesRegex(AssertionError, "projection|native shape"),
            ):
                fixture.verify_superseded_migration(data, {})
            export.assert_not_called()

    def test_superseded_projection_consumes_native_empty_terminal_page(self):
        for split in (False, True):
            data, page, raw, checked = self.superseded_wire()
            messages = page["data"]
            token = "native+/cursor="
            pages = (
                [
                    {"data": messages[:2], "cursor": {"next": "first"}},
                    {"data": messages[2:], "cursor": {"next": token}},
                ]
                if split
                else [{"data": messages, "cursor": {"next": token}}]
            )
            with (
                self.subTest(split=split),
                patch.object(
                    fixture.workspace_service,
                    "api",
                    side_effect=[
                        *pages,
                        {"data": [], "cursor": {"next": None}},
                        {"data": raw},
                    ],
                ) as api,
                patch.object(box, "export_container_session", return_value=checked),
            ):
                self.assertEqual(fixture.verify_superseded_migration(data, {}), checked)
            routes = [call.args[-1] for call in api.call_args_list]
            route = "/api/session/" + fixture.SUPERSEDED_ID + "/message?limit=100"
            self.assertEqual(routes[0], route + "&order=asc")
            self.assertEqual(routes[-2], route + "&cursor=native%2B%2Fcursor%3D")
            self.assertTrue(routes[-1].endswith("/export"))

    def test_superseded_projection_refuses_malformed_or_unbounded_pagination(self):
        data, page, _, _ = self.superseded_wire()
        valid = page["data"]
        cases = {
            "missing-page": [None],
            "missing-cursor": [{"data": valid}],
            "invalid-next": [{"data": valid, "cursor": {"next": 1}}],
            "empty-next": [{"data": valid, "cursor": {"next": ""}}],
            "repeated-cursor": [
                {"data": valid, "cursor": {"next": "repeat"}},
                {"data": [], "cursor": {"next": "repeat"}},
            ],
            "repeated-message": [
                {"data": valid[:2], "cursor": {"next": "more"}},
                {"data": valid, "cursor": {"next": None}},
            ],
            "invalid-message": [{"data": [None], "cursor": {"next": None}}],
            "extra-message": [
                {"data": [*valid, {"id": "msg_extra"}], "cursor": {"next": None}}
            ],
            "unterminated-empty-pages": [
                {"data": [], "cursor": {"next": str(number)}} for number in range(8)
            ],
        }
        for name, pages in cases.items():
            with (
                self.subTest(name=name),
                patch.object(fixture.workspace_service, "api", side_effect=pages),
                patch.object(box, "export_container_session") as export,
                self.assertRaisesRegex(AssertionError, "projection"),
            ):
                fixture.verify_superseded_migration(data, {})
            export.assert_not_called()

    def test_superseded_raw_export_must_omit_exactly_one_known_assistant(self):
        for change in (
            "drop-settled",
            "retain-unfinished",
            "changed-settled",
            "foreign-project",
            "annotation",
        ):
            data, page, raw, checked = self.superseded_wire()
            raw = json.loads(json.dumps(raw))
            if change == "drop-settled":
                del raw["messages"][1]
            elif change == "retain-unfinished":
                raw["messages"].insert(1, page["data"][1])
            elif change == "changed-settled":
                raw["messages"][0]["text"] = "changed"
            elif change == "foreign-project":
                raw["info"]["location"]["directory"] = "/foreign/project"
            else:
                raw[fixture.history_io.INTERRUPTED] = {"assistant": 1}
            with (
                self.subTest(change=change),
                patch.object(
                    fixture.workspace_service, "api", side_effect=[page, {"data": raw}]
                ),
                patch.object(
                    box, "export_container_session", return_value=checked
                ) as export,
                self.assertRaisesRegex(AssertionError, "omit exactly"),
            ):
                fixture.verify_superseded_migration(data, {})
            export.assert_not_called()

    def test_superseded_checked_export_requires_independent_count_and_native_messages(
        self,
    ):
        for count in (
            None,
            {"assistant": 2},
            {"assistant": True},
            {"assistant": 1, "user": 1},
        ):
            data, page, raw, checked = self.superseded_wire()
            checked[fixture.history_io.INTERRUPTED] = count
            with (
                self.subTest(count=count),
                patch.object(
                    fixture.workspace_service, "api", side_effect=[page, {"data": raw}]
                ),
                patch.object(box, "export_container_session", return_value=checked),
                self.assertRaisesRegex(AssertionError, "count or transcript"),
            ):
                fixture.verify_superseded_migration(data, {})
        data, page, raw, checked = self.superseded_wire()
        checked["messages"].pop()
        with (
            patch.object(
                fixture.workspace_service, "api", side_effect=[page, {"data": raw}]
            ),
            patch.object(box, "export_container_session", return_value=checked),
            self.assertRaisesRegex(AssertionError, "count or transcript"),
        ):
            fixture.verify_superseded_migration(data, {})

    def test_superseded_sync_requires_count_checkpoint_and_native_host_verification(
        self,
    ):
        data, _, _, checked = self.superseded_wire()
        for change in (
            None,
            "count",
            "boolean-count",
            "transcript",
            "annotation",
            "checkpoint",
            "host-mapping",
            "host-refusal",
        ):
            with (
                tempfile.TemporaryDirectory() as temporary,
                self.subTest(change=change),
            ):
                path = Path(temporary) / "workspace.json"
                archived = box.archive_session(checked, data)
                ident = archived["info"]["id"]
                target = path.parent / "history" / (ident + ".json")
                if change in ("count", "boolean-count"):
                    archived["info"]["metadata"]["oc-box.archive"]["interrupted"] = {
                        "assistant": True if change == "boolean-count" else 2
                    }
                elif change == "transcript":
                    archived["messages"].pop()
                elif change == "annotation":
                    archived[fixture.history_io.INTERRUPTED] = {"assistant": 1}
                box.atomic_json(target, archived)
                state = {
                    "host_sessions": {fixture.SUPERSEDED_ID: ident},
                    "synced": {
                        fixture.SUPERSEDED_ID: fixture.hashlib.sha256(
                            target.read_bytes()
                        ).hexdigest()
                    },
                }
                if change == "checkpoint":
                    state["synced"].clear()
                elif change == "host-mapping":
                    state["host_sessions"].clear()
                with (
                    patch.object(box, "session_state", return_value=state),
                    patch.object(box, "verify_host_archive") as verify,
                ):
                    if change == "host-refusal":
                        verify.side_effect = box.BoxError(
                            "Native host verification refused"
                        )
                        with self.assertRaisesRegex(
                            box.BoxError, "host verification refused"
                        ):
                            fixture.verify_superseded_sync(path, data, checked)
                        verify.assert_called_once_with(path, data, archived)
                    elif change is not None:
                        with self.assertRaisesRegex(
                            AssertionError, "archive or checkpoint"
                        ):
                            fixture.verify_superseded_sync(path, data, checked)
                        verify.assert_not_called()
                    else:
                        fixture.verify_superseded_sync(path, data, checked)
                        verify.assert_called_once_with(path, data, archived)

    def exercise_downgrade_retry(self, root, *, changed_v1=False):
        path, source, _ = self.retained_downgrade(root)
        native = {
            "info": {
                "id": fixture.LEGACY_ID,
                "location": {"directory": source["project"]},
            },
            "messages": [
                {"id": "msg_seed", "type": "user", "text": "V1_HISTORY_RETAINED"},
                {
                    "id": "msg_progress",
                    "type": "assistant",
                    "text": "HOST_V2_ACCEPTANCE_OK",
                },
            ],
        }
        calls = []
        server = Mock(server_port=12345)
        worker = Mock()

        def launch(arguments):
            major = (root / "host-major").read_text()
            source["opencode_version"] = fixture.NATIVE_VERSIONS[major]
            calls.append("launch-" + major)
            box.run_tui([])

        def export(data, ident):
            if data["opencode_version"].startswith("1."):
                transcript = fixture.legacy_session(root / "source")
                transcript["info"]["time"]["updated"] = 100
                if changed_v1:
                    transcript["messages"][0]["parts"][0]["text"] = "lost original"
                return transcript
            return json.loads(json.dumps(native))

        def model_request(data, ident, state, port):
            calls.append("native-v2-request")
            state["markers"].update(("VAULT_SHELL_OK", "VAULT_MCP_OK"))
            native["messages"].append(
                {"id": "msg_followup", "type": "user", "text": "new progress"}
            )
            return subprocess.CompletedProcess([], 0, "HOST_V2_ACCEPTANCE_OK", "")

        def opened(archive, identities):
            calls.append("authenticate-" + archive.stem)
            manifest = {
                "kind": archive.stem,
                "source_workspace_id": source["id"],
                "project": source["project"],
                "sessions": {"last": fixture.LEGACY_ID},
            }
            return contextlib.nullcontext(
                (manifest, root / "encrypted", b"synthetic-private")
            )

        with (
            patch.object(
                fixture,
                "resume_fixture",
                return_value=(
                    root,
                    Path(sys.executable),
                    Path(sys.executable),
                    "tumbleweed",
                ),
            ),
            patch.object(
                fixture, "prepare_native_cli", return_value=root / "bin/opencode"
            ),
            patch.object(fixture, "workspace", return_value=(path, source)),
            patch.object(fixture, "stopped_container"),
            patch.object(fixture, "make_provider", return_value=(server, worker)),
            patch.object(fixture.crypto, "opened", side_effect=opened),
            patch.object(fixture.backups, "validate_manifest"),
            patch.object(box, "check_backend"),
            patch.object(fixture.vault, "write_encrypted"),
            patch.object(box, "main", side_effect=launch),
            patch.object(box, "export_container_session", side_effect=export),
            patch.object(
                fixture, "digest_home", return_value="unchanged-v2-bytes"
            ) as digest,
            patch.object(fixture, "run_fixture_session", side_effect=model_request),
            patch.object(
                box, "exec_command", side_effect=lambda data, command: command
            ),
            patch.object(
                box, "run", return_value=subprocess.CompletedProcess([], 0, "", "")
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            if changed_v1:
                with self.assertRaisesRegex(AssertionError, "message/part history"):
                    fixture.retry_downgrade(root)
            else:
                fixture.retry_downgrade(root)
        self.assertTrue(
            all(
                call.args[1]
                in (
                    ".local/share/opencode/oc-box-v2.db",
                    ".local/share/opencode/opencode.db",
                )
                for call in digest.call_args_list
            )
        )
        self.assertEqual(len(digest.call_args_list), 1 if changed_v1 else 4)
        server.shutdown.assert_called_once()
        server.server_close.assert_called_once()
        worker.join.assert_called_once()
        for name in ("full.ocbox", "portable.ocbox"):
            self.assertEqual((root / name).read_bytes(), b"retained fixture evidence")
        return calls

    def test_downgrade_retry_checks_native_v1_and_v2_history_and_never_recreates_backups(
        self,
    ):
        with tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as temporary:
            root = Path(temporary)
            calls = self.exercise_downgrade_retry(root)
            self.assertEqual(
                calls,
                [
                    "authenticate-full",
                    "authenticate-portable",
                    "launch-2",
                    "launch-1",
                    "launch-2",
                    "native-v2-request",
                ],
            )
            result = json.loads((root / "downgrade-result.json").read_text())
            self.assertEqual(result["status"], "passed")
            self.assertIn("Targeted", result["scope"])

    def test_downgrade_retry_stops_on_changed_original_history_without_false_pass(self):
        with tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as temporary:
            root = Path(temporary)
            calls = self.exercise_downgrade_retry(root, changed_v1=True)
            self.assertEqual(calls[-1], "launch-1")
            self.assertNotIn("native-v2-request", calls)
            self.assertFalse((root / "downgrade-result.json").exists())

    def retained_downgrade(self, root):
        path, source = self.retained_source(root)
        box.atomic_json(
            path.parent / "imports" / (fixture.LEGACY_ID + ".json"),
            fixture.legacy_session(root / "source"),
        )
        ids = [source["id"]]
        with (
            patch.dict(os.environ, fixture.isolated_env(root), clear=True),
            patch.object(Path, "home", return_value=root / "host-home"),
        ):
            for kind in ("full", "portable"):
                project = root / ("restored-" + kind)
                project.mkdir()
                restored_path, data = box.create_workspace(project, "tumbleweed")
                data.update(
                    container_project=source["project"],
                    restored_from={"workspace_id": source["id"], "kind": kind},
                )
                box.atomic_json(restored_path, data)
                ids.append(data["id"])
        (root / "fixture-ids.json").write_text(json.dumps(ids))
        for name in ("full.ocbox", "portable.ocbox", "age-identity.txt"):
            (root / name).write_bytes(b"retained fixture evidence")
            (root / name).chmod(0o600)
        return path, source, ids

    def test_downgrade_retry_requires_exact_restored_registry_and_preserves_both_archives(
        self,
    ):
        with tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as temporary:
            root = Path(temporary)
            _, _, ids = self.retained_downgrade(root)
            with patch.object(
                fixture, "stopped_container", return_value={"State": {"Running": False}}
            ):
                self.assertEqual(fixture.resume_fixture(root, downgrade=True)[0], root)
            with self.assertRaisesRegex(ValueError, "source fixture"):
                fixture.resume_fixture(root)
            path = root / "registry/workspaces" / ids[-1] / "workspace.json"
            data = box.read_json(path)
            data["restored_from"]["workspace_id"] = "e" * 32
            box.atomic_json(path, data)
            with (
                patch.object(fixture, "stopped_container"),
                self.assertRaisesRegex(
                    ValueError, "retained downgrade fixture evidence"
                ),
            ):
                fixture.resume_fixture(root, downgrade=True)
            for name in ("full.ocbox", "portable.ocbox"):
                self.assertEqual(
                    (root / name).read_bytes(), b"retained fixture evidence"
                )

    def test_downgrade_retry_refuses_changed_seed_history_or_completed_report(self):
        with tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as temporary:
            root = Path(temporary)
            path, _, _ = self.retained_downgrade(root)
            evidence = path.parent / "imports" / (fixture.LEGACY_ID + ".json")
            original = box.read_json(evidence)
            changed = json.loads(json.dumps(original))
            changed["messages"][0]["parts"][0]["text"] = "different history"
            box.atomic_json(evidence, changed)
            with (
                patch.object(fixture, "stopped_container"),
                self.assertRaisesRegex(AssertionError, "message/part history"),
            ):
                fixture.resume_fixture(root, downgrade=True)
            box.atomic_json(evidence, original)
            (root / "downgrade-result.json").write_text("retained completion evidence")
            with self.assertRaisesRegex(
                ValueError, "overwrite backup/restore evidence"
            ):
                fixture.resume_fixture(root, downgrade=True)
            self.assertEqual(
                (root / "downgrade-result.json").read_text(),
                "retained completion evidence",
            )
