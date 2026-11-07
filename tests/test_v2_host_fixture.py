import contextlib
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
from unittest.mock import patch

import oc_box as box
import vault

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

    def test_fixture_cleanup_refuses_projects_outside_fixture_root(self):
        with tempfile.TemporaryDirectory(prefix=fixture.PREFIX) as temporary:
            root = Path(temporary)
            (root / "fixture-ids.json").write_text(json.dumps(["a" * 32]))
            path = root / "registry/workspaces" / ("a" * 32) / "workspace.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {"id": "a" * 32, "project": str(root.parent / "real-project")}
                )
            )
            with (
                patch.object(box, "remove_resources") as remove,
                self.assertRaisesRegex(ValueError, "Unrecognized resource"),
            ):
                fixture.cleanup(root)
            remove.assert_not_called()
