"""Synthetic native wire cases; only the outer Podman execution is stubbed."""

import base64
import contextlib
import json
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from unittest.mock import patch

from oc_box import oc_box as box
from oc_box import web_native_pair as pair
from oc_box.web_admission import WebBinding
from oc_box.web_local_origins import LocalOrigins, WORKSPACE_HOST
from oc_box.web_tls import HTTPSOrigin


PASSWORD = "P" * 43
AUTH = "Basic " + base64.b64encode(("opencode:" + PASSWORD).encode()).decode()
CODE = "c" * 21 + "Q"
VALUE = "K" * 10 + "." + "v" * 42 + "A"
ORIGIN = LocalOrigins().workspace
HOST = ORIGIN.value.removeprefix("https://")
ATTRS = "Max-Age=2592000; Path=/; HttpOnly; SameSite=Lax"
ERROR = r"^Native workspace pairing could not be verified\.$"


def _wire(status, headers, body=b""):
    fields = [f"HTTP/1.1 {status} Synthetic", *[f"{k}: {v}" for k, v in headers]]
    return ("\r\n".join(fields) + "\r\n\r\n").encode() + body


def _json_wire(value, status=200):
    body = json.dumps(value).encode()
    return _wire(
        status,
        [("Content-Type", "application/json"), ("Content-Length", len(body))],
        body,
    )


def _cookie(origin, value=VALUE):
    suffix = f"_{origin.port}" if origin.port != 443 else ""
    return f"opencode_session{suffix}={value}"


def _redirect(origin=ORIGIN, value=VALUE):
    return _wire(
        302,
        [
            ("Location", "/"),
            ("Set-Cookie", _cookie(origin, value) + "; " + ATTRS),
            ("Transfer-Encoding", "chunked"),
        ],
        b"0\r\n\r\n",
    )


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(2)
        request_line = self.rfile.readline(1024).decode().strip()
        headers = {}
        for _ in range(16):
            line = self.rfile.readline(1024)
            if line in (b"\r\n", b""):
                break
            key, value = line.decode().strip().split(":", 1)
            headers[key.lower()] = value.strip()
        self.server.requests.append((request_line, headers))
        self.wfile.write(self.server.reply(len(self.server.requests) - 1))


@contextlib.contextmanager
def _peer(origin, override, version):
    with socketserver.TCPServer(("127.0.0.1", 0), _Handler) as server:
        endpoint = f"http://127.0.0.1:{server.server_address[1]}"
        info = native_info(endpoint, version)
        replies = (
            _json_wire({"error": "synthetic unauthorized"}, 401),
            _json_wire(info),
            _json_wire({"code": CODE, "expires_in": 300}),
            _redirect(origin),
            _json_wire(info),
        )
        server.requests = []

        def respond(index):
            reply = override.get(
                index, replies[index] if index < len(replies) else replies[0]
            )
            return reply(info) if callable(reply) else reply

        server.reply = respond
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        thread.start()
        try:
            yield endpoint, server.requests
        finally:
            server.shutdown()
            thread.join(timeout=2)


def native_info(endpoint, version="2.3.2", pid=141):
    return {
        "version": version,
        "pid": pid,
        "hostname": "private-native-hostname",
        "urls": [endpoint],
        "paths": {"tmp": "/synthetic/private-native-tmp"},
        "capabilities": {"persistentPty": True},
    }


def _workspace(home, endpoint, version="2.3.2"):
    return (
        {
            "id": "synthetic-workspace",
            "container": "synthetic-no-podman",
            "project": home,
            "home": home,
            "opencode_version": version,
        },
        {
            "workspace": "synthetic-workspace",
            "version": version,
            "url": endpoint,
            "password": PASSWORD,
            "session": None,
        },
    )


class NativePairTests(unittest.TestCase):
    def _pair(self, data, connection, origin=ORIGIN, owner_current=None):
        binding = WebBinding(data["id"], "fixture-owner", origin.value)
        if owner_current is None:
            owner_current = lambda candidate: candidate == binding
        return pair.pair_cookie(
            box,
            data,
            connection,
            origin=origin,
            binding=binding,
            owner_current=owner_current,
        )

    @contextlib.contextmanager
    def _fixture(self, origin=ORIGIN, *, override=None, version="2.3.2"):
        with _peer(origin, override or {}, version) as (endpoint, requests):
            with tempfile.TemporaryDirectory() as home:
                data, connection = _workspace(home, endpoint, version)
                spawn = subprocess.Popen

                def local_exec(command, **options):
                    # Execute the real providerless environment trampoline and
                    # worker. The host fixture merely replaces podman exec.
                    self.assertEqual(command[:3], ["podman", "exec", "-i"])
                    marker = command.index(data["container"])
                    self.assertEqual(
                        command[marker + 1 : marker + 5], ["python3", "-I", "-S", "-c"]
                    )
                    self.assertEqual(
                        command[-3:], [endpoint, origin.value[8:], version]
                    )
                    self.assertTrue(
                        not any(
                            secret in arg
                            for secret in (CODE, VALUE, AUTH, PASSWORD)
                            for arg in command
                        )
                    )
                    self.assertIs(options["stdin"], subprocess.DEVNULL)
                    self.assertIs(options["stdout"], subprocess.PIPE)
                    self.assertIs(options["stderr"], subprocess.DEVNULL)
                    self.assertNotIn("SYNTHETIC_PROVIDER_TOKEN", options["env"])
                    return spawn(command[marker + 1 :], cwd=home, **options)

                with (
                    patch.dict(
                        "os.environ", {"SYNTHETIC_PROVIDER_TOKEN": "not-forwarded"}
                    ),
                    patch.object(pair.subprocess, "Popen", side_effect=local_exec),
                ):
                    yield data, connection, requests

    def test_round_trip_keeps_origin_and_native_cookie_scope_distinct_from_tcp(self):
        for origin in (ORIGIN, HTTPSOrigin(WORKSPACE_HOST, 443)):
            with self.subTest(port=origin.port), self._fixture(origin) as fixture:
                data, connection, requests = fixture
                binding = WebBinding(data["id"], "fixture-owner", origin.value)
                live = True
                token = self._pair(
                    data,
                    connection,
                    origin,
                    owner_current=lambda candidate: live and candidate == binding,
                )
                self.assertNotIn(VALUE, repr(token))
                args = {
                    "binding": binding,
                    "endpoint": connection["url"],
                    "origin": origin,
                    "version": connection["version"],
                }
                self.assertEqual(token.upstream_cookie(**args), _cookie(origin))
                for wrong in (
                    {"binding": replace(binding, workspace_id="other")},
                    {"binding": replace(binding, owner_generation="replacement-owner")},
                    {"endpoint": connection["url"] + "/"},
                    {"origin": HTTPSOrigin(WORKSPACE_HOST, 9999)},
                    {"version": "2.4.1"},
                ):
                    with self.assertRaisesRegex(pair.NativePairError, ERROR):
                        token.upstream_cookie(**(args | wrong))
                live = False
                with self.assertRaisesRegex(pair.NativePairError, ERROR):
                    token.upstream_cookie(**args)

                expected = [
                    ("GET /api/info HTTP/1.1", None, None),
                    ("GET /api/info HTTP/1.1", AUTH, None),
                    ("POST /api/pair HTTP/1.1", AUTH, None),
                    (f"GET /auth/connect/{CODE} HTTP/1.1", None, None),
                    ("GET /api/info HTTP/1.1", None, _cookie(origin)),
                ]
                self.assertEqual(
                    [
                        (line, headers.get("authorization"), headers.get("cookie"))
                        for line, headers in requests
                    ],
                    expected,
                )
                self.assertTrue(
                    all(
                        headers["host"] == origin.value[8:]
                        and headers["origin"] == origin.value
                        and headers["connection"] == "close"
                        for _, headers in requests
                    )
                )
                self.assertEqual(requests[3][1]["accept"], "text/html")

    def test_unverified_lease_or_untrusted_origin_refuses_before_exec(self):
        data, connection = _workspace("/synthetic", "http://127.0.0.1:52142")
        for update in (
            {"workspace": "other"},
            {"url": "http://127.0.0.2:52142"},
            {"url": "http://127.0.0.1:52142?x=1"},
            {"version": "2.3.2-dev"},
            {"version": "2.3.3"},
            {
                "version": "2.4.1"
            },  # Even another supported release must match the lease.
            {"password": ""},
        ):
            with (
                self.subTest(fields=tuple(update)),
                patch.object(pair.subprocess, "Popen") as spawn,
                self.assertRaisesRegex(pair.NativePairError, ERROR),
            ):
                self._pair(data, connection | update)
            spawn.assert_not_called()

        for origin in (
            ORIGIN.value,
            LocalOrigins().account,
            HTTPSOrigin("attacker.invalid", 9444),
            HTTPSOrigin(WORKSPACE_HOST, 80),
            HTTPSOrigin(WORKSPACE_HOST, True),
        ):
            with (
                patch.object(pair.subprocess, "Popen") as spawn,
                self.assertRaisesRegex(pair.NativePairError, ERROR),
            ):
                pair.pair_cookie(
                    box,
                    data,
                    connection,
                    origin=origin,
                    binding=WebBinding(data["id"], "fixture-owner", ORIGIN.value),
                    owner_current=lambda _: True,
                )
            spawn.assert_not_called()

    def test_owner_loss_before_or_during_pairing_never_returns_the_credential(self):
        for checks, expected_requests in (([False], 0), ([True, False], 5)):
            with (
                self.subTest(checks=checks),
                self._fixture() as (data, connection, requests),
                self.assertRaisesRegex(pair.NativePairError, ERROR),
            ):
                responses = iter(checks)
                try:
                    self._pair(
                        data, connection, owner_current=lambda _: next(responses)
                    )
                finally:
                    self.assertEqual(len(requests), expected_requests)

    def test_refuses_unauthenticated_wrong_owner_or_failed_cookie_without_retry(self):
        for name, index, reply in (
            ("anonymous info", 0, _json_wire({"version": "2.3.2"})),
            ("wrong native version", 1, _json_wire({"version": "2.3.3"})),
            (
                "other audited native version",
                1,
                lambda info: _json_wire(info | {"version": "2.4.1"}),
            ),
            ("bad mint", 2, _json_wire({"code": "unsafe/../", "expires_in": 300})),
            ("invalid native expiry", 2, _json_wire({"code": CODE, "expires_in": 301})),
            (
                "mint reply with extra fields",
                2,
                _json_wire({"code": CODE, "expires_in": 300, "password": PASSWORD}),
            ),
            (
                "cookie includes pairing code",
                3,
                _redirect(value=VALUE[:11] + CODE + "v" * 21),
            ),
            ("cookie includes password", 3, _redirect(value=VALUE[:11] + PASSWORD)),
            ("cookie unauthorized", 4, _json_wire({"error": VALUE}, 401)),
            (
                "wrong loopback scope",
                4,
                _json_wire(native_info("http://127.0.0.1:1")),
            ),
            (
                "different native PID on same port",
                4,
                lambda info: _json_wire(info | {"pid": 142}),
            ),
        ):
            with (
                self.subTest(name=name),
                self._fixture(override={index: reply}) as (data, connection, requests),
                self.assertRaisesRegex(pair.NativePairError, ERROR) as refusal,
            ):
                self._pair(data, connection)
            self.assertIsNone(refusal.exception.__context__)
            self.assertEqual(len(requests), index + 1)

    def test_cookie_parser_rejects_ambiguous_or_unexpected_wire_contract(self):
        valid = _redirect()
        self.assertEqual(pair._parse_cookie(pair._reply(valid), HOST), VALUE)
        length_framed = valid.replace(
            b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n", b"Content-Length: 0\r\n\r\n"
        )
        self.assertEqual(pair._parse_cookie(pair._reply(length_framed), HOST), VALUE)
        for name, raw in (
            ("wrong status", valid.replace(b" 302 ", b" 301 ")),
            ("absolute redirect", valid.replace(b"Location: /", b"Location: //evil")),
            (
                "more locations",
                valid.replace(b"Location: /", b"Location: /\r\nlocation: /"),
            ),
            (
                "foreign cookie",
                valid.replace(b"opencode_session_9444", b"opencode_session_9443"),
            ),
            (
                "default cookie",
                valid.replace(b"opencode_session_9444", b"opencode_session"),
            ),
            (
                "second cookie",
                valid.replace(b"Location: /", b"Location: /\r\nset-cookie: other=x"),
            ),
            (
                "legacy cookie header",
                valid.replace(b"Location: /", b"Location: /\r\nSet-Cookie2: other=x"),
            ),
            ("duplicate attr", valid.replace(b"HttpOnly;", b"HttpOnly; HttpOnly;")),
            ("domain attr", valid.replace(b"HttpOnly;", b"HttpOnly; Domain=evil;")),
            ("unexpected Secure", valid.replace(b"HttpOnly;", b"HttpOnly; Secure;")),
            ("no HttpOnly", valid.replace(b"HttpOnly; ", b"")),
            ("bad SameSite", valid.replace(b"SameSite=Lax", b"SameSite=None")),
            ("bad max-age", valid.replace(b"2592000", b"2592001")),
            ("bad path", valid.replace(b"Path=/", b"Path=/nested")),
            ("unsafe value", valid.replace(VALUE.encode(), b"multicookie,other")),
            ("oversized value", valid.replace(VALUE.encode(), b"x" * 512)),
            ("header fold", valid.replace(b"; Path=/", b";\r\n Path=/")),
            ("too many headers", valid.replace(b"Location: /", b"X: " + b"X" * 8192)),
            ("unexpected body", valid.replace(b"0\r\n\r\n", b"1\r\nx\r\n0\r\n\r\n")),
            (
                "invalid framing",
                valid.replace(b"chunked", b"chunked\r\nContent-Length: 0"),
            ),
            ("trailers", valid.replace(b"0\r\n\r\n", b"0\r\nX-Trace: secret\r\n\r\n")),
        ):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(pair.NativePairError, ERROR),
            ):
                pair._parse_cookie(pair._reply(raw), HOST)

    def test_bridge_requires_capped_output_eof_success_and_a_deadline(self):
        for name, program in (
            ("overflow", "import os; os.write(1, b'x' * 10000)"),
            ("partial", "import os; os.write(1, b'x' * 53)"),
            ("failed exit", "import os; os.write(1, b'x' * 54); os._exit(1)"),
            ("stalled EOF", "import os,time; os.write(1, b'x' * 54); time.sleep(20)"),
        ):
            with self.subTest(name=name):
                command = box.ContainerCommand(
                    [sys.executable, "-I", "-S", "-c", program],
                    box.maintenance_host_env(),
                )
                with (
                    patch.object(pair, "_HOST_TIMEOUT", 0.5),
                    self.assertRaisesRegex(pair.NativePairError, ERROR),
                ):
                    pair._read_bridge(command)


if __name__ == "__main__":
    unittest.main()
