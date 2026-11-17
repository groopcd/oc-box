import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import build_opener, ProxyHandler, Request

from oc_box import (
    oc_box as box,
    web_attachments as adapter,
    web_sessions,
    workspace_service as service,
)


ROOT = Path(__file__).resolve().parents[1]
SELECTED = adapter.session_link("ses_synthetic")


class RequestTests(unittest.TestCase):
    origin = "http://127.0.0.1:5555"

    def raw(self, *, method="GET", target=SELECTED, headers=()):
        return (
            f"{method} {target} HTTP/1.1\r\nHost: 127.0.0.1:5555\r\n"
            + "".join(key + ": " + value + "\r\n" for key, value in headers)
            + "\r\n"
        ).encode()

    def test_native_auth_probe_preserves_only_browser_credentials(self):
        raw = self.raw(
            headers=(
                ("Cookie", "native=synthetic"),
                ("Authorization", "Basic synthetic"),
                ("Origin", self.origin),
                ("Referer", self.origin + "/private-pair-code"),
                ("Sec-Fetch-Site", "same-origin"),
                ("X-Ignore", "private"),
            )
        )
        method, target, probe = adapter.request(raw, self.origin)
        self.assertEqual((method, target), ("GET", SELECTED))
        self.assertIn(b"GET /api/info HTTP/1.1", probe)
        self.assertIn(b"cookie: native=synthetic", probe)
        self.assertIn(b"authorization: Basic synthetic", probe)
        self.assertNotIn(b"private", probe)
        self.assertNotIn(b"X-Ignore", probe)

    def test_read_only_same_origin_request_matrix(self):
        cases = [
            self.raw(method="POST"),
            self.raw(method="OPTIONS"),
            self.raw(target=adapter.PATH + "?url=https://outside.invalid"),
            self.raw(target=adapter.PATH),
            self.raw(target=SELECTED + "&session=ses_other"),
            self.raw(target=SELECTED + "&other=1"),
            self.raw(target=adapter.PATH + "?session="),
            self.raw(target=adapter.PATH + "?session=../../private"),
            self.raw(target=adapter.PATH + "?session=ses_%73ynthetic"),
            self.raw(target=adapter.PATH + "?session=ses_synthetic&"),
            self.raw(target=adapter.SCRIPT + "?session=ses_synthetic"),
            self.raw(target=adapter.PATH + "/../private"),
            self.raw(target="http://outside.invalid" + adapter.PATH),
            self.raw(headers=(("Origin", "http://127.0.0.1:5556"),)),
            self.raw(headers=(("Origin", "null"),)),
            self.raw(headers=(("Referer", "https://outside.invalid/"),)),
            self.raw(headers=(("Sec-Fetch-Site", "same-site"),)),
            self.raw(headers=(("Sec-Fetch-Site", "cross-site"),)),
            self.raw(headers=(("Host", "127.0.0.1:5555"),)),
            self.raw(headers=(("Cookie", "a=1"), ("cookie", "b=2"))),
            self.raw(headers=(("Transfer-Encoding", "chunked"),)),
            self.raw(headers=(("Content-Length", "1"),)),
            self.raw(headers=(("Upgrade", "websocket"),)),
            self.raw() + b"GET /api/info HTTP/1.1\r\n\r\n",
            self.raw().replace(b"127.0.0.1:5555", b"outside.invalid"),
            self.raw(headers=(("Cookie", "x" * adapter.MAX_HEADERS),)),
            self.raw().replace(b"\r\n", b"\n"),
        ]
        for raw in cases:
            with (
                self.subTest(raw=raw[:90]),
                self.assertRaises((ValueError, UnicodeError)),
            ):
                adapter.request(raw, self.origin)

    def test_html_escapes_native_route_and_does_not_contain_secrets(self):
        html = adapter.page(
            '/server/local/session/ses_test"><script>bad</script>'
        ).decode()
        self.assertNotIn('ses_test"><script>', html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("password", html)
        self.assertIn("IndexedDB", html)

    def test_adapter_navigation_uses_native_service_worker_exclusion(self):
        # Source-bound native vite.pwa.ts contract in both supported versions.
        # Ordinary app navigation falls back to cached index.html; /auth/ does not.
        denied = re.compile(r"^/(?:api|auth)(?:/|$)")
        self.assertRegex(adapter.PATH, denied)
        self.assertRegex(adapter.SCRIPT, denied)
        self.assertIn(
            adapter.SCRIPT.encode(), adapter.page("/server/local/session/ses_test")
        )

    def test_opt_in_and_native_version_preflight_before_pairing(self):
        self.assertFalse(service.web_parser().parse_args([]).attachments)
        self.assertTrue(service.web_parser().parse_args(["--attachments"]).attachments)
        args = SimpleNamespace(attachments=True, port=0, no_open=True)
        for version in ("2.2.7", "2.3.1", "2.3.0-beta", None):
            with (
                self.subTest(version=version),
                patch.object(service, "require_session", return_value="ses_synthetic"),
                patch.object(service, "api") as native,
                patch.object(service, "Gateway") as gateway,
                self.assertRaisesRegex(box.BoxError, "requires native"),
            ):
                service.web_session(box, {}, {"version": version}, args)
            native.assert_not_called()
            gateway.assert_not_called()
        with (
            patch.object(service, "require_session", return_value="ses_synthetic"),
            patch.object(service, "api", return_value={"version": "2.3.1"}) as native,
            patch.object(service, "Gateway") as gateway,
            self.assertRaisesRegex(box.BoxError, "verification failed"),
        ):
            service.web_session(box, {}, {"version": "2.3.0"}, args)
        self.assertEqual(native.call_args.args[3:], ("GET", "/api/info"))
        gateway.assert_not_called()

    def test_cli_unsupported_version_refuses_before_workspace_or_credentials(self):
        with (
            patch.object(box, "detect_opencode_release", return_value="2.2.7"),
            patch.object(box, "find_workspace") as workspace,
            patch.object(service, "preflight_port") as port,
            patch.object(box, "check_backend") as backend,
            patch.object(box.vault, "hidden") as unlock,
            self.assertRaisesRegex(box.BoxError, "requires native"),
        ):
            # Unit tests run inside oc-box; this tests the host CLI path only.
            with patch.dict("os.environ", {"OC_BOX_CONTAINER": "0"}):
                box.main(["web", "--attachments", "--project", "/synthetic/project"])
        for operation in (workspace, port, backend, unlock):
            operation.assert_not_called()

    def test_supported_versions_enable_only_the_selected_gateway(self):
        for version in ("2.2.6", "2.3.0"):
            with (
                self.subTest(version=version),
                patch.object(service, "require_session", return_value="ses_synthetic"),
                patch.object(
                    service,
                    "api",
                    side_effect=[{"version": version}, {"code": "synthetic"}],
                ) as native,
                patch.object(service, "Gateway") as gateway,
                patch.object(service.time, "sleep", side_effect=KeyboardInterrupt),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                gateway.return_value.__enter__.return_value.url = (
                    "http://127.0.0.1:5555"
                )
                self.assertEqual(
                    service.web_session(
                        box,
                        {},
                        {"version": version, "password": "synthetic"},
                        SimpleNamespace(attachments=True, port=0, no_open=True),
                    ),
                    0,
                )
                self.assertTrue(gateway.call_args.kwargs["attachments"])
                self.assertEqual(
                    [call.args[3] for call in native.call_args_list], ["GET", "POST"]
                )

    @unittest.skipUnless(
        shutil.which("node"), "Node is needed for synthetic browser events"
    )
    def test_synthetic_browser_event_contract(self):
        result = subprocess.run(
            ["node", str(ROOT / "tests/web_attachments_browser.mjs")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS synthetic browser", result.stdout)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.probes = []
        probes = self.probes

        class Native(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                probes.append((self.path, dict(self.headers)))
                if self.path.startswith("/auth/connect/"):
                    self.send_response(302)
                    self.send_header("Location", "/")
                    self.send_header(
                        "Set-Cookie",
                        "native=synthetic; HttpOnly; SameSite=Strict; Path=/",
                    )
                elif self.path == "/api/info":
                    valid = self.headers.get("Cookie") == "native=synthetic"
                    self.send_response(200 if valid else 401)
                else:
                    self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Native)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.opener = build_opener(ProxyHandler({}))
        self.relay_patch = patch.object(
            service,
            "relay_command",
            side_effect=lambda _box, _data, port: box.ContainerCommand(
                [sys.executable, "-B", "-c", service.PROXY, str(port)],
                box.maintenance_host_env(),
            ),
        )
        self.relay_patch.start()
        self.addCleanup(self.relay_patch.stop)
        self.session_patch = patch.object(
            service, "require_session", side_effect=AssertionError("owner fallback")
        )
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)
        self.gateway = service.Gateway(
            box,
            {},
            {"url": "http://127.0.0.1:" + str(self.server.server_port)},
            attachments=True,
        ).__enter__()
        self.addCleanup(self.gateway.__exit__)

    def get(self, target=SELECTED, **headers):
        return self.opener.open(
            Request(self.gateway.url + target, headers=headers), timeout=10
        )

    def test_authenticated_static_routes_and_native_auth_remains_authoritative(self):
        with self.assertRaises(HTTPError) as failed:
            self.get()
        self.assertEqual(failed.exception.code, 401)
        with self.get(Cookie="native=synthetic") as reply:
            body = reply.read()
            self.assertIn(b"oc-box attachment adapter", body)
            self.assertIn(
                self.gateway.native_session_route("ses_synthetic").encode(), body
            )
            self.assertNotIn(b'data-route="' + web_sessions.INDEX.encode(), body)
            self.assertEqual(reply.headers["Cache-Control"], "no-store")
            self.assertEqual(reply.headers["Referrer-Policy"], "no-referrer")
            self.assertIn(
                "frame-ancestors 'none'", reply.headers["Content-Security-Policy"]
            )
        with self.get(adapter.SCRIPT, Cookie="native=synthetic") as reply:
            self.assertIn(b"DataTransfer", reply.read())
            self.assertIn("text/javascript", reply.headers["Content-Type"])
        for target, headers in self.probes:
            self.assertEqual(target, "/api/info")
            self.assertEqual(headers["Origin"], self.gateway.url)
            self.assertNotIn("Authorization", headers)

    def test_foreign_origins_and_body_routes_fail_before_any_native_probe(self):
        for extra in (
            {"Origin": "https://outside.invalid"},
            {"Sec-Fetch-Site": "same-site"},
            {"Referer": "http://127.0.0.1:1/private"},
        ):
            with self.subTest(extra=extra), self.assertRaises(HTTPError) as failed:
                self.get(Cookie="native=synthetic", **extra)
            self.assertEqual(failed.exception.code, 403)
        self.assertEqual(self.probes, [])

    def test_four_chooser_links_open_their_selected_wrappers_not_owner_or_index(self):
        sessions = [
            {"id": "ses_" + name, "title": name}
            for name in ("archived", "worktree", "moved", "current")
        ]
        with patch.object(
            web_sessions, "open_sessions", return_value=(sessions, "valid")
        ):
            with self.get(web_sessions.INDEX, Cookie="native=synthetic") as reply:
                body = reply.read()
            self.assertIn(b"Open native OpenCode Home", body)
            for item in sessions:
                link = adapter.session_link(item["id"])
                self.assertIn(('href="' + link + '"').encode(), body)
                with self.get(link, Cookie="native=synthetic") as reply:
                    wrapper = reply.read()
                expected = self.gateway.native_session_route(item["id"])
                self.assertIn(('data-route="' + expected + '"').encode(), wrapper)
                self.assertNotIn(b'data-route="' + web_sessions.INDEX.encode(), wrapper)
        with patch.object(web_sessions, "open_sessions", return_value=([], "valid")):
            with self.get(web_sessions.INDEX, Cookie="native=synthetic") as reply:
                body = reply.read()
            self.assertIn(b'href="/"', body)
            self.assertNotIn(b"?session=", body)

    def test_invalid_selection_queries_refuse_before_native_auth_probe(self):
        for target in (
            adapter.PATH,
            adapter.PATH + "?session=",
            SELECTED + "&session=ses_other",
            SELECTED + "&unknown=1",
            adapter.SCRIPT + "?version=1",
        ):
            with self.subTest(target=target), self.assertRaises(HTTPError) as failed:
                self.get(target, Cookie="native=synthetic")
            self.assertEqual(failed.exception.code, 403)
        self.assertEqual(self.probes, [])

    def test_head_has_no_body_and_pairing_redirect_selects_chooser(self):
        request = Request(
            self.gateway.url + SELECTED,
            headers={"Cookie": "native=synthetic"},
            method="HEAD",
        )
        with self.opener.open(request, timeout=10) as reply:
            self.assertEqual(reply.read(), b"")
            self.assertGreater(int(reply.headers["Content-Length"]), 0)
        with socket.create_connection(
            ("127.0.0.1", self.gateway.listener.getsockname()[1]), timeout=5
        ) as stream:
            stream.sendall(
                (
                    "GET /auth/connect/synthetic HTTP/1.1\r\nHost: "
                    + self.gateway.url.removeprefix("http://")
                    + "\r\n\r\n"
                ).encode()
            )
            reply = stream.recv(8192)
        self.assertIn(b"location: " + web_sessions.INDEX.encode(), reply.lower())
        self.assertIn(b"native=synthetic", reply)


if __name__ == "__main__":
    unittest.main()
