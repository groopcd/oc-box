"""Open means native TUI membership, not active drains or historical roots."""

import base64
import contextlib
from copy import deepcopy
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import socket
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

from oc_box import native_tabs, oc_box as box, opencode_v2_io as bridge
from oc_box import web_sessions as web, workspace_service as service


class WebSessionsTests(unittest.TestCase):
    def setUp(self):
        self.data = {
            "id": "a" * 32,
            "project": "/host/restored",
            "container_project": "/original/project",
            "opencode_version": "2.3.0",
        }
        self.connection = {
            "workspace": self.data["id"],
            "url": "http://127.0.0.1:12345",
            "password": "a" * 43,
            "session": "ses_one",
        }
        self.inventory = [
            {
                "id": "ses_" + name,
                "title": title,
                "location": {"directory": "/original/project"},
                "time": {"updated": 1},
            }
            for name, title in (
                ("one", "First"),
                ("two", '<img src=x onerror="bad">'),
                ("three", "Third"),
                ("four", "Fourth"),
                ("closed", "Closed newest root"),
                ("archived", "Archived"),
                ("child", "Child"),
                ("foreign", "Foreign"),
            )
        ]
        self.inventory[4]["time"]["updated"] = 100
        self.inventory[5]["time"]["archived"] = 0
        self.inventory[6]["parentID"] = "ses_one"
        self.inventory[7]["location"]["directory"] = "/original/project-sibling"
        self.members = (
            "ses_four",
            "ses_one",
            "ses_two",
            "ses_three",
            "ses_archived",
            "ses_child",
            "ses_foreign",
            "ses_absent",
        )

    def collect(self, snapshot, inventory=None, connection=None):
        with (
            patch.object(native_tabs, "inspect", return_value=snapshot),
            patch.object(
                service,
                "tab_inventory",
                return_value=self.inventory if inventory is None else inventory,
            ),
        ):
            return web.open_sessions(
                box, self.data, self.connection if connection is None else connection
            )

    def test_four_idle_tabs_use_membership_order_exact_restored_directory_and_scope(
        self,
    ):
        before = deepcopy(self.inventory)
        for scope in ("cwd", "global"):
            with self.subTest(scope=scope):
                sessions, status = self.collect(
                    native_tabs.Snapshot("valid", self.members, scope)
                )
                self.assertEqual(status, "valid")
                self.assertEqual(
                    [item["id"] for item in sessions],
                    ["ses_four", "ses_one", "ses_two", "ses_three"],
                )
        self.assertEqual(self.inventory, before)

    def test_missing_empty_invalid_and_changed_state_never_reopen_history(self):
        for reason in ("absent", "disabled", "scope-absent"):
            with self.subTest(reason=reason):
                sessions, status = self.collect(
                    native_tabs.Snapshot("missing", reason=reason)
                )
                self.assertEqual(status, "missing")
                self.assertEqual([item["id"] for item in sessions], ["ses_one"])
        self.assertEqual(self.collect(native_tabs.Snapshot("valid")), ([], "valid"))
        with self.assertRaises(box.BoxError):
            self.collect(native_tabs.Snapshot("invalid", reason="transport"))
        for selected in (
            None,
            "ses_archived",
            "ses_child",
            "ses_foreign",
            "ses_absent",
        ):
            self.assertEqual(
                self.collect(
                    native_tabs.Snapshot("missing", reason="absent"),
                    connection={**self.connection, "session": selected},
                ),
                ([], "missing"),
            )
        with (
            patch.object(
                native_tabs,
                "inspect",
                side_effect=[
                    native_tabs.Snapshot("valid", ("ses_one",)),
                    native_tabs.Snapshot("valid"),
                ],
            ),
            patch.object(service, "tab_inventory", return_value=self.inventory),
            self.assertRaisesRegex(box.BoxError, "changed"),
        ):
            web.open_sessions(box, self.data, self.connection)

    def test_foreign_source_malformed_or_duplicate_catalog_refuses(self):
        with self.assertRaises(box.BoxError):
            self.collect(
                native_tabs.Snapshot("valid", self.members),
                connection={**self.connection, "workspace": "b" * 32},
            )
        for inventory in ({}, self.inventory + self.inventory[:1], [{}]):
            with self.subTest(inventory=type(inventory).__name__):
                with self.assertRaises(native_tabs.TabError):
                    self.collect(native_tabs.Snapshot("valid", self.members), inventory)

    def test_complete_native_catalog_pagination_retains_all_four_not_first_page_only(
        self,
    ):
        calls = []

        def request(method, route):
            self.assertEqual(method, "GET")
            self.assertIn("directory=%2Foriginal%2Fproject", route)
            self.assertNotIn("/active", route)
            index = len(calls)
            calls.append(route)
            return {
                "data": self.inventory[index : index + 1],
                "cursor": {"next": "page+" + str(index + 1) if index < 3 else None},
            }

        catalog = bridge.session_inventory(request, "/original/project")
        self.assertEqual(len(calls), 4)
        self.assertIn("cursor=page%2B1", calls[1])
        sessions, _ = self.collect(native_tabs.Snapshot("valid", self.members), catalog)
        self.assertEqual(len(sessions), 4)

    def test_document_escapes_titles_and_uses_public_native_routes_without_secrets(
        self,
    ):
        sessions, status = self.collect(native_tabs.Snapshot("valid", self.members))
        sessions[3]["title"] = "\ud800"  # Native JSON can contain a lone surrogate.
        url = "http://127.0.0.1:23456"
        text = web.document(url, sessions, status).decode()
        self.assertEqual(text.count('target="_blank"'), 4)
        self.assertIn("&lt;img", text)
        self.assertNotIn("<img", text)
        self.assertIn("independent of TUI tabs", text)
        self.assertIn(web.INDEX, text)
        self.assertNotIn("ses_closed", text)
        self.assertNotIn(self.connection["password"], text)
        self.assertEqual(
            base64.urlsafe_b64decode(
                web.session_route(url, "ses_one").split("/")[2] + "=="
            ).decode(),
            url,
        )

    def test_web_can_pair_at_native_home_without_fabricating_a_selected_session(self):
        gateway = SimpleNamespace(url="http://127.0.0.1:23456")
        with (
            patch.object(
                service, "Gateway", return_value=contextlib.nullcontext(gateway)
            ),
            patch.object(service, "api", return_value={"code": "synthetic_code"}),
            patch.object(service.time, "sleep", side_effect=KeyboardInterrupt),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                service.web_session(
                    box,
                    self.data,
                    {**self.connection, "session": None},
                    SimpleNamespace(port=0, no_open=True),
                ),
                0,
            )
        self.assertIn("workspace session index", output.getvalue())

    def test_owning_web_honors_saved_empty_or_open_tabs_instead_of_newest_history(self):
        state = {"last": "ses_closed"}
        for snapshot, expected in (
            (native_tabs.Snapshot("valid"), None),
            (native_tabs.Snapshot("valid", ("ses_two", "ses_one")), "ses_two"),
        ):
            with self.subTest(snapshot=snapshot):
                owner = SimpleNamespace(
                    connection=self.connection, publish=Mock(), wait_clients=Mock()
                )
                args = SimpleNamespace(
                    new=False, model=None, resume=False, command="web"
                )
                with (
                    patch.object(box, "refresh_sessions", return_value=state),
                    patch.object(native_tabs, "inspect", return_value=snapshot),
                    patch.object(
                        box, "list_container_sessions", return_value=self.inventory
                    ),
                    patch.object(service, "api") as native,
                    patch.object(service, "web_session", return_value=0) as browser,
                    patch.object(service, "wait_history"),
                    patch.object(box, "sync_sessions"),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(
                        box.launch_client(
                            None,
                            {**self.data, "distro": "synthetic"},
                            args,
                            "2.3.0",
                            None,
                            owner,
                        ),
                        0,
                    )
                owner.publish.assert_called_once_with(expected)
                native.assert_not_called()
                self.assertEqual(browser.call_args.args[2]["session"], expected)

    def test_authenticated_gateway_lists_four_without_weakening_native_cookie_origin_checks(
        self,
    ):
        observed = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(handler):
                observed.append(
                    (
                        handler.path,
                        {key.lower(): value for key, value in handler.headers.items()},
                    )
                )
                if handler.path.startswith("/auth/connect/"):
                    handler.send_response(302)
                    handler.send_header("Location", "/")
                    handler.send_header(
                        "Set-Cookie", "native=fixture; HttpOnly; Path=/"
                    )
                    body = b""
                elif handler.path == "/api/info":
                    allowed = (
                        handler.headers.get("Cookie") == "native=fixture"
                        and handler.headers.get(
                            "Origin", "http://" + handler.headers["Host"]
                        )
                        == "http://" + handler.headers["Host"]
                    )
                    handler.send_response(200 if allowed else 401)
                    body = b"{}"
                else:
                    handler.send_response(200)
                    body = b"native-unchanged"
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)

        def local_relay(_box, _data, port):
            return box.ContainerCommand(
                [sys.executable, "-B", "-c", service.PROXY, str(port)],
                box.maintenance_host_env(),
            )

        opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
        with (
            patch.object(service, "relay_command", side_effect=local_relay),
            patch.object(
                native_tabs,
                "inspect",
                return_value=native_tabs.Snapshot("valid", self.members),
            ),
            patch.object(
                service, "tab_inventory", return_value=self.inventory
            ) as inventory,
            service.Gateway(
                box,
                self.data,
                {
                    **self.connection,
                    "url": "http://127.0.0.1:" + str(upstream.server_port),
                },
            ) as gateway,
        ):
            with self.assertRaises(HTTPError) as refusal:
                opener.open(gateway.url + web.INDEX, timeout=5)
            self.assertEqual(refusal.exception.code, 401)
            inventory.assert_not_called()
            authority = gateway.url.removeprefix("http://")
            for headers, method, code in (
                ("Host: 127.0.0.1:1\r\nCookie: native=fixture", "GET", b"401"),
                ("Cookie: native=fixture", "GET", b"401"),
                (
                    "Host: "
                    + authority
                    + "\r\nCookie: native=fixture\r\nCookie: native=fixture",
                    "GET",
                    b"401",
                ),
                ("Host: " + authority + "\r\nCookie: native=fixture", "POST", b"400"),
            ):
                with socket.create_connection(
                    ("127.0.0.1", int(authority.rsplit(":", 1)[1])), timeout=5
                ) as stream:
                    stream.sendall(
                        (
                            method
                            + " "
                            + web.INDEX
                            + " HTTP/1.1\r\n"
                            + headers
                            + "\r\n\r\n"
                        ).encode()
                    )
                    self.assertIn(code, stream.recv(4096).split(b"\r\n", 1)[0])
            inventory.assert_not_called()
            with opener.open(
                gateway.url + "/auth/connect/fixture", timeout=5
            ) as response:
                self.assertEqual(response.geturl(), gateway.url + web.INDEX)
                self.assertEqual(response.read().count(b'target="_blank"'), 4)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertTrue(
                all(
                    headers["host"] == gateway.url.removeprefix("http://")
                    and "authorization" not in headers
                    for path, headers in observed
                    if path == "/api/info"
                )
            )
            with self.assertRaises(HTTPError) as refusal:
                opener.open(
                    Request(
                        gateway.url + web.INDEX,
                        headers={"Origin": "http://foreign.invalid"},
                    ),
                    timeout=5,
                )
            self.assertEqual(refusal.exception.code, 401)
            for route in (
                "/",
                "/api/session/active",
                "/server/example/session/ses_one",
            ):
                with opener.open(gateway.url + route, timeout=5) as response:
                    self.assertEqual(response.read(), b"native-unchanged")
            with patch.object(
                service, "tab_inventory", side_effect=ValueError("private")
            ):
                with self.assertRaises(HTTPError) as refusal:
                    opener.open(gateway.url + web.INDEX, timeout=5)
                self.assertEqual(refusal.exception.code, 503)
                self.assertNotIn(b"private", refusal.exception.read())


if __name__ == "__main__":
    unittest.main()
