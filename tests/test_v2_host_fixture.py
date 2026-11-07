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
