import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import oc_box as box
import workspace_service as service
import vault


class WorkspaceServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-live-service-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(
            os.environ,
            {
                "OC_BOX_STATE_HOME": str(self.root / "state"),
                "XDG_RUNTIME_DIR": str(self.root),
                "OC_BOX_CONTAINER": "0",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        project = self.root / "project"
        project.mkdir()
        self.path, self.data = box.create_workspace(project, "tumbleweed")
        self.data["opencode_version"] = "2.2.6"
        box.atomic_json(self.path, self.data)
        self.connection = {
            "url": "http://127.0.0.1:12345",
            "password": "a" * 43,
            "workspace": self.data["id"],
            "session": "ses_shared",
            "version": "2.2.6",
        }
        self.info = {
            "State": {"Running": True},
            "Mounts": [{"Source": str(project), "Destination": str(project)}],
        }

    def test_endpoint_validation_refuses_remote_hosts_embedded_credentials_and_bad_passwords(
        self,
    ):
        self.assertEqual(service.validate_endpoint(self.connection), self.connection)
        for url in (
            "http://localhost:1",
            "http://example.test:1",
            "http://0.0.0.0:1",
            "http://user:password@127.0.0.1:1",
            "http://127.0.0.1:1/path",
            "http://127.0.0.1:1?password=bad",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                service.validate_endpoint({**self.connection, "url": url})
        with self.assertRaises(ValueError):
            service.validate_endpoint({**self.connection, "password": "short"})

    def test_client_environment_and_argv_do_not_inherit_provider_or_vault_values(self):
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "synthetic-key",
                "CUSTOM_SECRET": "synthetic-custom",
                "BASH_ENV": "host-startup",
                "TERM": "xterm\nbad",
            },
        ):
            command = service.client_command(
                box,
                self.data,
                self.connection,
                [
                    "opencode",
                    "mini",
                    "--server",
                    self.connection["url"],
                    "--session",
                    "ses_shared",
                ],
            )
        self.assertNotIn("synthetic-key", " ".join(command))
        self.assertNotIn(self.connection["password"], " ".join(command))
        self.assertNotIn("OPENAI_API_KEY", command.environment)
        self.assertNotIn("CUSTOM_SECRET", command.environment)
        self.assertNotIn("BASH_ENV", command.environment)
        self.assertEqual(
            command.environment["OPENCODE_PASSWORD"], self.connection["password"]
        )
        self.assertIn("TERM", " ".join(command))
        self.assertNotIn("xterm\\nbad", " ".join(command))

    def test_live_terminal_joins_without_lifecycle_lock_unlock_refresh_or_stop(self):
        with (
            contextlib.redirect_stdout(io.StringIO()),
            box.workspace_lock(self.path),
            patch.object(box, "check_backend"),
            patch.object(box.sys.stdin, "isatty", return_value=True),
            patch.object(box.sys.stdout, "isatty", return_value=True),
            patch.object(
                service, "attach", return_value=contextlib.nullcontext(self.connection)
            ),
            patch("terminal_ui.run_tui", return_value=7) as relay,
            patch.object(vault, "hidden") as prompt,
            patch.object(box, "prepare_runtime") as prepare,
            patch.object(box, "stop_container") as stop,
        ):
            self.assertEqual(box.main(["terminal", "--workspace", self.data["id"]]), 7)
        command = relay.call_args.args[0]
        self.assertNotIn("mini", command)
        self.assertNotIn("--server", command)
        self.assertIn("persistent-pty", " ".join(command))
        self.assertIn("ses_shared", command)
        self.assertNotIn("--standalone", command)
        self.assertFalse(relay.call_args.kwargs["show_loading"])
        for operation in (prompt, prepare, stop):
            operation.assert_not_called()

    def test_explicit_maintenance_still_requires_the_exclusive_lifecycle_lock(self):
        with (
            box.workspace_lock(self.path),
            patch.object(box, "check_backend"),
            patch.object(box.sys.stdin, "isatty", return_value=True),
            patch.object(box.sys.stdout, "isatty", return_value=True),
            self.assertRaisesRegex(box.BoxError, "active oc-box process"),
        ):
            box.main(["terminal", "--maintenance", "--workspace", self.data["id"]])

    def test_web_and_full_tui_attach_to_current_session_while_owner_holds_lock(self):
        with (
            box.workspace_lock(self.path),
            patch.object(box, "check_backend"),
            patch.object(box, "detect_opencode_release", return_value="2.2.6"),
            patch.object(service, "controller_running", return_value=True),
            patch.object(
                service, "attach", return_value=contextlib.nullcontext(self.connection)
            ),
            patch.object(service, "web_session", return_value=0) as web,
            patch.object(box, "run_tui", return_value=0) as tui,
            patch.object(box, "prepare_runtime") as prepare,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                box.main(["web", "--workspace", self.data["id"], "--no-open"]), 0
            )
            self.assertEqual(box.main(["start", "--workspace", self.data["id"]]), 0)
        self.assertEqual(web.call_args.args[2]["session"], "ses_shared")
        self.assertIn("ses_shared", tui.call_args.args[0])
        prepare.assert_not_called()

    def test_invalid_web_options_or_v1_fail_before_startup_and_credentials(self):
        with (
            patch.object(box, "detect_opencode_release", return_value="1.19.5"),
            patch.object(box, "check_backend") as backend,
            patch.object(vault, "hidden") as prompt,
        ):
            with self.assertRaisesRegex(box.BoxError, "requires OpenCode 2.x"):
                box.main(["web", "--project", self.data["project"]])
        backend.assert_not_called()
        prompt.assert_not_called()
        with (
            patch.object(box, "detect_opencode_release", return_value="2.2.6"),
            self.assertRaisesRegex(box.BoxError, "--port"),
        ):
            box.main(["web", "--port", "65536"])
        with self.assertRaisesRegex(box.BoxError, "--maintenance"):
            box.main(["start", "--maintenance"])

    def test_busy_web_port_fails_before_workspace_lookup_or_vault_prompt(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            with (
                patch.object(box, "detect_opencode_release", return_value="2.2.6"),
                patch.object(box, "find_workspace") as lookup,
                patch.object(vault, "hidden") as prompt,
                self.assertRaises(OSError),
            ):
                box.main(["web", "--port", str(listener.getsockname()[1])])
            lookup.assert_not_called()
            prompt.assert_not_called()

    def test_browser_launcher_does_not_receive_provider_credentials_or_pairing_link_in_argv(
        self,
    ):
        gateway = SimpleNamespace(url="http://127.0.0.1:5555")
        args = SimpleNamespace(port=0, no_open=False)
        with (
            patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "synthetic-provider",
                    "CUSTOM_SECRET": "synthetic-secret",
                },
            ),
            patch.object(
                service, "Gateway", return_value=contextlib.nullcontext(gateway)
            ),
            patch.object(service, "api", return_value={"code": "synthetic_pair_code"}),
            patch.object(service.subprocess, "run") as browser,
            patch.object(service.time, "sleep", side_effect=KeyboardInterrupt),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                service.web_session(box, self.data, self.connection, args), 0
            )
        call = browser.call_args
        self.assertNotIn("OPENAI_API_KEY", call.kwargs["env"])
        self.assertNotIn("CUSTOM_SECRET", call.kwargs["env"])
        self.assertNotIn("synthetic_pair_code", " ".join(call.args[0]))
        self.assertIn("synthetic_pair_code", call.kwargs["input"])

    def test_v2_history_cli_uses_active_service_without_persisting_control_password(
        self,
    ):
        connection = {**self.connection}
        token = service.CONNECTION.set(connection)
        try:
            command = box.exec_command(
                self.data, [box.CONTAINER_OPENCODE, "export", "ses_shared"]
            )
            self.assertIn("--server", command)
            self.assertNotIn("--standalone", command)
            self.assertNotIn(connection["password"], " ".join(command))
            self.assertEqual(
                command.environment["OPENCODE_PASSWORD"], connection["password"]
            )
            self.assertNotIn(connection["password"], self.path.read_text())
        finally:
            service.CONNECTION.reset(token)

    def test_web_owner_can_start_foreground_and_client_exit_waits_before_sync(self):
        calls = []
        owner = SimpleNamespace(
            connection=self.connection,
            publish=lambda value: calls.append(("publish", value)),
            wait_clients=lambda: calls.append(("wait", None)),
        )
        args = box.parser().parse_args(["start"])
        args.command, args.port, args.no_open = "web", 0, True
        with (
            patch.object(box, "refresh_sessions", return_value={"last": "ses_shared"}),
            patch.object(
                service,
                "web_session",
                side_effect=lambda *_: calls.append(("web", None)) or 0,
            ),
            patch.object(
                box,
                "sync_sessions",
                side_effect=lambda *_: calls.append(("sync", None)),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                box.launch_client(self.path, self.data, args, "2.2.6", None, owner), 0
            )
        self.assertEqual(
            calls,
            [("publish", "ses_shared"), ("web", None), ("wait", None), ("sync", None)],
        )

    def test_failed_active_client_still_waits_for_attached_clients(self):
        owner = SimpleNamespace(
            connection=self.connection,
            publish=lambda value: None,
            wait_clients=Mock(),
        )
        args = box.parser().parse_args(["start"])
        with (
            patch.object(box, "refresh_sessions", return_value={"last": "ses_shared"}),
            patch.object(box, "run_tui", side_effect=OSError("terminal failed")),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(OSError),
        ):
            box.launch_client(self.path, self.data, args, "2.2.6", None, owner)
        owner.wait_clients.assert_called_once()

    def test_local_controller_leases_delay_owner_shutdown_and_preserve_lock(self):
        owner = service.Owner(box, self.path, self.data)
        code = (
            "import json,sys; print(json.dumps("
            + repr(
                {"url": self.connection["url"], "password": self.connection["password"]}
            )
            + "),flush=True); sys.stdin.read()"
        )

        def local_exec(_data, _arguments):
            return box.ContainerCommand(
                [sys.executable, "-B", "-c", code], dict(os.environ)
            )

        with (
            box.workspace_lock(self.path),
            patch.object(box, "exec_command", side_effect=local_exec),
            patch.object(box, "inspect_owned", return_value=self.info),
            owner,
        ):
            owner.publish("ses_shared")
            self.assertTrue(service.controller_running(box, self.data))
            with service.attach(box, self.path, self.data) as attached:
                self.assertEqual(attached["session"], "ses_shared")
                self.assertEqual(owner.clients, 1)
                exited = threading.Event()
                thread = threading.Thread(
                    target=lambda: (owner.wait_clients(), exited.set())
                )
                thread.start()
                time.sleep(0.05)
                self.assertFalse(exited.is_set())
                with self.assertRaises(box.BoxError):
                    with box.workspace_lock(self.path):
                        pass
            thread.join(timeout=2)
            self.assertTrue(exited.is_set())
        self.assertFalse(owner.address.exists())
        self.assertIsNone(service.live_connection())

    def test_runtime_directory_must_be_private_and_generated_socket_never_overwrites_files(
        self,
    ):
        address = service.socket_path(box, self.data)
        address.write_text("unrelated")
        with self.assertRaisesRegex(box.BoxError, "Unexpected file"):
            service.Owner(box, self.path, self.data).__enter__()
        self.assertEqual(address.read_text(), "unrelated")
        address.unlink()
        address.parent.chmod(0o777)
        with self.assertRaisesRegex(box.BoxError, "unsafe"):
            service.socket_path(box, self.data)

    def test_browser_gateway_streams_http_pairing_redirect_and_binary_upgrades(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                if self.path.startswith("/auth/connect/"):
                    self.send_response(302)
                    self.send_header("Location", "/")
                    self.send_header("Set-Cookie", "native-cookie=fixture")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif self.path == "/upgrade":
                    self.send_response(101)
                    self.send_header("Upgrade", "websocket")
                    self.send_header("Connection", "Upgrade")
                    self.end_headers()
                    self.wfile.write(b"binary-frame\x00\xff")
                    self.wfile.flush()
                    self.connection.recv(1)
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", "5")
                    self.end_headers()
                    self.wfile.write(b"hello")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = {
            **self.connection,
            "url": "http://127.0.0.1:" + str(server.server_port),
        }

        def local_relay(_box, _data, port):
            return box.ContainerCommand(
                [sys.executable, "-B", "-c", service.PROXY, str(port)],
                box.maintenance_host_env(),
            )

        with (
            patch.object(service, "relay_command", side_effect=local_relay),
            service.Gateway(box, self.data, connection) as gateway,
        ):
            with urlopen(gateway.url + "/", timeout=5) as response:
                self.assertEqual(response.read(), b"hello")
            with socket.create_connection(
                ("127.0.0.1", urlparse(gateway.url).port), timeout=5
            ) as stream:
                stream.sendall(
                    b"GET /auth/connect/fixture HTTP/1.1\r\nHost: 127.0.0.1:1\r\nAccept: text/html\r\n\r\n"
                )
                data = stream.recv(8192)
                self.assertIn(gateway.session_route().encode(), data)
                self.assertIn(b"native-cookie=fixture", data)
            with socket.create_connection(
                ("127.0.0.1", urlparse(gateway.url).port), timeout=5
            ) as stream:
                stream.sendall(
                    b"GET /upgrade HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
                )
                data = bytearray()
                while b"binary-frame\x00\xff" not in data:
                    data.extend(stream.recv(8192))
                self.assertIn(b"101", data)
                stream.sendall(b"x")
