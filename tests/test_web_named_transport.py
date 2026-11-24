"""Local synthetic wire checks for the unserved HTTPS request reader."""

import socket
import ssl
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event
from time import monotonic
from unittest.mock import Mock, call, patch

import test_web_local_origins as certificates

from oc_box import web_local_origins as local
from oc_box import web_named_transport as transport
from oc_box.web_named_headers import NamedRequestError
from oc_box.web_tls import WebTLSError

_RECEIVE_TIMEOUT = 7
_START = b"POST /start HTTP/1.1"
_INFO = b"GET /api/info HTTP/1.1"
_JSON = b"Content-Type: application/json"
_CONFIRM = b"POST /oc-box/confirm HTTP/1.1"
_LOGOUT = b"POST /oc-box/logout HTTP/1.1"


def _wire(origin, start, *fields, body=b""):
    authority = origin.value.removeprefix("https://").encode("ascii")
    return b"\r\n".join((start, b"Host: " + authority, *fields, b"", b"")) + body


def _origin(origin):
    return b"Origin: " + origin.value.encode("ascii")


def _chunks(*pieces):
    """Model bounded recv, preserving any later chunk for the surplus probe."""
    stream = Mock(spec=ssl.SSLSocket)
    remaining = list(pieces)

    def recv(size):
        if not remaining:
            if stream.setblocking.called:
                raise ssl.SSLWantReadError
            return b""
        data, remaining[0] = remaining[0][:size], remaining[0][size:]
        if not remaining[0]:
            remaining.pop(0)
        return data

    stream.recv.side_effect = recv
    return stream


class RequestFramingTests(unittest.TestCase):
    sites = local.LocalOrigins()

    def read(self, stream, role="account"):
        return transport._read(
            stream,
            role=role,
            origin=getattr(self.sites, role),
            deadline=monotonic() + 1,
        )

    def test_fragmented_header_and_body_preserve_raw_request(self):
        origin = self.sites.account
        wire = _wire(
            origin, _START, _origin(origin), _JSON, b"CoNtEnT-LeNgTh:  4 ", body=b"fake"
        )
        chunks = [wire[:20], wire[20:-7], wire[-7:-2], wire[-2:]]
        request = self.read(_chunks(*chunks))
        self.assertEqual(
            (request.role, request.method, request.target),
            ("account", "POST", "/start"),
        )
        self.assertEqual(request.headers[-1], ("CoNtEnT-LeNgTh", "  4 "))
        self.assertEqual(request.body, b"fake")
        self.assertNotIn("fake", repr(request))
        self.assertNotIn(origin.hostname, repr(request))

    def test_ambiguous_or_unsupported_framing_refused_before_body_read(self):
        origin = self.sites.account
        for label, fields in (
            ("missing length", ()),
            ("duplicate length", (b"Content-Length: 4", b"content-length: 4")),
            ("conflicting lengths", (b"Content-Length: 4", b"content-length: 8")),
            ("combined length", (b"Content-Length: 4, 4",)),
            ("noncanonical length", (b"Content-Length: 04",)),
            ("signed length", (b"Content-Length: +4",)),
            ("oversized length", (b"Content-Length: 8193",)),
            ("overlong number", (b"Content-Length: " + b"9" * 8000,)),
            (
                "transfer encoding",
                (b"Content-Length: 4", b"Transfer-Encoding: chunked"),
            ),
            ("trailers", (b"Content-Length: 4", b"Trailer: Content-Length")),
            ("te", (b"Content-Length: 4", b"TE: trailers")),
            ("compression", (b"Content-Length: 4", b"Content-Encoding: gzip")),
            ("expect", (b"Content-Length: 4", b"Expect: 100-continue")),
            (
                "connection tokens",
                (b"Content-Length: 4", b"Connection: close, x-extra"),
            ),
            ("folded value", (b"Content-Length: 4", b" leading: spaces")),
            ("space before colon", (b"Content-Length : 4",)),
            ("tab value", (b"Content-Length:\t4",)),
            ("bare LF", (b"Content-Length: 4\nOther: x",)),
            ("bare CR", (b"Content-Length: 4\rOther: x",)),
            ("control", (b"Content-Length: 4", b"X-Extra: \x00")),
            ("obs-text", (b"Content-Length: 4", b"X-Extra: \xff")),
            ("no colon", (b"Content-Length",)),
            ("duplicate host", (b"Content-Length: 4", b"hOSt: another")),
            ("duplicate origin", (b"Content-Length: 4", _origin(origin))),
            ("forwarding", (b"Content-Length: 4", b"X-Forwarded-Proto: https")),
        ):
            with self.subTest(case=label):
                head = _wire(origin, _START, _origin(origin), _JSON, *fields)
                stream = _chunks(head, b"fake")
                with self.assertRaises(NamedRequestError):
                    self.read(stream)
                self.assertEqual(stream.recv.call_count, (len(head) + 4095) // 4096)

    def test_truncation_surplus_and_pipelining_are_not_admitted(self):
        origin = self.sites.account
        head = _wire(origin, _START, _origin(origin), _JSON, b"Content-Length: 4")
        pipelined = head + b"fake"
        for label, chunks in (
            ("incomplete head", (head[:-1],)),
            ("bare LF terminator", (head.replace(b"\r\n", b"\n"),)),
            ("truncated body", (head, b"abc")),
            ("same-read surplus", (head + b"abcde",)),
            ("later buffered surplus", (head, b"abcd", b"e")),
            ("pipelined request", (head, b"abcd" + pipelined)),
        ):
            with self.subTest(case=label), self.assertRaises(NamedRequestError):
                self.read(_chunks(*chunks))

    def test_header_bytes_field_count_and_body_have_hard_limits(self):
        origin = self.sites.account
        head = _wire(origin, _START, _origin(origin), _JSON, b"Content-Length: 8192")
        self.assertEqual(len(self.read(_chunks(head, b"x" * 8192)).body), 8192)
        padded_head = head[:-2] + b"X-Pad: \r\n\r\n"
        pad = transport._HEAD_LIMIT - len(padded_head)
        for extra in (0, 1):
            wire = padded_head[:-4] + b"x" * (pad + extra) + b"\r\n\r\n"
            stream = _chunks(wire, b"x" * 8192)
            if extra:
                with self.assertRaises(NamedRequestError):
                    self.read(stream)
                self.assertEqual(
                    sum(args[0] for args, _ in stream.recv.call_args_list),
                    transport._HEAD_LIMIT,
                )
            else:
                self.assertEqual(len(self.read(stream).body), 8192)
        for count in (60, 61):  # Four fixed fields, plus the synthetic fields.
            fields = tuple(f"X-{i}: x".encode("ascii") for i in range(count))
            wire = _wire(
                origin,
                _START,
                _origin(origin),
                _JSON,
                b"Content-Length: 1",
                *fields,
                body=b"x",
            )
            if count == 60:
                self.assertEqual(len(self.read(_chunks(wire)).headers), 64)
            else:
                with self.assertRaises(NamedRequestError):
                    self.read(_chunks(wire))

    def test_raw_method_path_role_and_site_are_not_rewritten(self):
        site = self.sites.workspace
        exact = _wire(site, _INFO, b"Sec-Fetch-Site: same-origin", b"Content-Length: 0")
        self.assertEqual(self.read(_chunks(exact), "workspace").target, "/api/info")
        cases = (
            (b"HEAD /api/info HTTP/1.1", ()),
            (b"get /api/info HTTP/1.1", ()),
            (b"GET\t/api/info HTTP/1.1", ()),
            (b"GET  /api/info HTTP/1.1", ()),
            (b"GET /api/info HTTP/1.0", ()),
            (b"GET /api/info HTTP/2.0", ()),
            (b"GET //api/info HTTP/1.1", ()),
            (b"GET https://" + site.hostname.encode() + b"/api/info HTTP/1.1", ()),
            (b"GET /api/info? HTTP/1.1", ()),
            (b"GET /api/%69nfo HTTP/1.1", ()),
            (b"GET /auth/connect/not-a-pairing-link HTTP/1.1", ()),
            (_START, (b"Content-Length: 0",)),
            (_INFO, (b"Content-Length: 1",)),
            (_INFO, (b"Content-Length: 00",)),
            (_INFO, (b"Upgrade: websocket", b"Connection: Upgrade")),
            (_INFO, (b"Sec-WebSocket-Version: 13",)),
            (_INFO, (b"Proxy-Connection: keep-alive",)),
            (_INFO, (b"HTTP2-Settings: ignored",)),
            (_INFO, (b"Sec-Fetch-Site: same-site",)),
            (_INFO, (b"Sec-Fetch-Site: none",)),
        )
        for start, fields in cases:
            with (
                self.subTest(start=start, fields=fields),
                self.assertRaises(NamedRequestError),
            ):
                self.read(
                    _chunks(_wire(site, start, _origin(site), *fields)), "workspace"
                )

    def test_route_specific_framing_refuses_before_reading_declared_body(self):
        for role, line, length, fields in (
            ("account", _START, 0, (_JSON,)),
            ("account", _START, 4, ()),
            ("account", _START, 4, (b"Content-Type: text/plain",)),
            ("account", _START, 4, (_JSON + b"; charset=utf-8",)),
            ("workspace", _CONFIRM, 53, ()),
            ("workspace", _CONFIRM, 55, ()),
            ("workspace", _LOGOUT, 1, ()),
        ):
            with self.subTest(role=role, line=line, length=length, fields=fields):
                site = getattr(self.sites, role)
                declaration = f"Content-Length: {length}".encode("ascii")
                stream = _chunks(
                    _wire(site, line, _origin(site), declaration, *fields),
                    b"x" * length,
                )
                with self.assertRaises(NamedRequestError):
                    self.read(stream, role)
                stream.recv.assert_called_once()

        app = self.sites.workspace
        head = _wire(app, _CONFIRM, _origin(app), b"Content-Length: 54")
        with self.assertRaises(NamedRequestError):
            self.read(_chunks(head, b"A" * 53 + b"\xff"), "workspace")

    def test_absolute_deadline_does_not_restart_when_head_or_body_progresses(self):
        origin = self.sites.account
        head = _wire(origin, _START, _origin(origin), _JSON, b"Content-Length: 4")
        stream = _chunks(head[:2], head[2:], b"a", b"bcd")
        recv = stream.recv.side_effect
        now = 100

        def delayed(size):
            nonlocal now
            now += 1
            return recv(size)

        stream.recv.side_effect = delayed
        with patch.object(transport, "monotonic", lambda: now):
            with self.assertRaises(NamedRequestError):
                transport._read(stream, role="account", origin=origin, deadline=103)
        self.assertEqual(stream.settimeout.call_args_list, [call(3), call(2), call(1)])
        self.assertEqual(stream.recv.call_count, 3)


class ResponseFramingTests(unittest.TestCase):
    def test_response_must_be_a_bounded_final_envelope(self):
        for label, reply in (
            ("informational", transport.NamedResponse(101, b"")),
            ("redirect", transport.NamedResponse(302, b"")),
            ("invalid status", transport.NamedResponse("200", b"")),
            ("not bytes", transport.NamedResponse(200, bytearray(b"{}"))),
            ("oversized", transport.NamedResponse(200, b"x" * 8193)),
            ("unstructured", (200, b"{}")),
        ):
            with self.subTest(case=label):
                stream = Mock(spec=ssl.SSLSocket)
                with self.assertRaises(NamedRequestError):
                    transport._write_response(stream, reply, monotonic() + 1)
                stream.send.assert_not_called()

    def test_partial_writes_share_one_deadline_and_are_not_retried_after_expiry(self):
        stream = Mock(spec=ssl.SSLSocket)
        now = 100

        def partial_send(_):
            nonlocal now
            now += 1
            return 5

        stream.send.side_effect = partial_send
        with patch.object(transport, "monotonic", lambda: now):
            with self.assertRaises(NamedRequestError):
                transport._write_response(
                    stream, transport.NamedResponse(200, b"{}"), 102
                )
        self.assertEqual(stream.settimeout.call_args_list, [call(2), call(1)])
        self.assertEqual(stream.send.call_count, 2)


@unittest.skipUnless(
    certificates.OPENSSL, "openssl is required for synthetic TLS files"
)
class LocalTLSRequestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        certs = certificates.LocalTLSFileTests()
        cls.addClassCleanup(certs.doCleanups)
        certs.setUp()
        cls.paths = {
            "account_cert": certs.cert,
            "account_key": certs.key,
            "workspace_cert": certs.app_cert,
            "workspace_key": certs.app_key,
        }
        cls.client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        cls.client.set_alpn_protocols(["h2", "http/1.1"])
        for cert in (certs.cert, certs.app_cert):
            cls.client.load_verify_locations(cafile=cert)

    def setUp(self):
        self.listeners = {role: self.listener() for role in ("account", "workspace")}
        self.sites = local.LocalOrigins(
            *(
                self.listeners[role].getsockname()[1]
                for role in ("account", "workspace")
            )
        )
        self.reader = transport.NamedHTTPSReader(self.sites, **self.paths)
        self.worker = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.worker.shutdown)

    def listener(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.settimeout(_RECEIVE_TIMEOUT)
        listener.bind((local.LOOPBACK, 0))
        listener.listen(1)
        return listener

    def start(self, listener, *, handlers=None):
        def receive():
            peer, _ = listener.accept()
            with peer:
                try:
                    if handlers is None:
                        return self.reader.read_one(peer)
                    return self.reader.respond_one(peer, **handlers)
                finally:
                    self.assertEqual(peer.fileno(), -1)

        return self.worker.submit(receive)

    @contextmanager
    def connect(self, listener):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as peer:
            peer.settimeout(_RECEIVE_TIMEOUT)
            peer.connect(listener.getsockname())
            yield peer

    @contextmanager
    def exchange(self, role, wire, *, handlers=None):
        listener = self.listeners[role]
        pending = self.start(listener, handlers=handlers)
        with self.connect(listener) as peer:
            hostname = getattr(self.sites, role).hostname
            with self.client.wrap_socket(peer, server_hostname=hostname) as stream:
                self.assertEqual(stream.selected_alpn_protocol(), "http/1.1")
                stream.sendall(wire)
                yield stream, pending

    def assert_closed_without_bytes(self, stream):
        try:
            self.assertEqual(stream.recv(1), b"")
        except ConnectionResetError:
            # Closing with unread bytes can cause TCP reset, also conclusive
            # that the peer is gone without returning a byte to this client.
            pass

    def response_bytes(self, stream):
        seen = bytearray()
        try:
            while len(seen) < 2048 and (part := stream.recv(2048 - len(seen))):
                seen.extend(part)
        except ConnectionResetError:
            # An unread second request can cause a TCP reset.
            pass
        self.assertLess(len(seen), 2048)
        return bytes(seen)

    def send(self, role, wire):
        with self.exchange(role, wire) as (stream, pending):
            try:
                return pending.result(timeout=_RECEIVE_TIMEOUT)
            finally:
                self.assert_closed_without_bytes(stream)

    def test_each_endpoint_reads_only_known_role_routes_over_verified_tls(self):
        for role, method, target, body in (
            ("account", "POST", "/start", b"fake"),
            ("account", "POST", "/finish", b"fake"),
            ("workspace", "GET", "/api/info", b""),
            ("workspace", "GET", "/api/session/active", b""),
            ("workspace", "POST", "/oc-box/confirm", b"A" * 54),
            ("workspace", "POST", "/oc-box/logout", b""),
        ):
            with self.subTest(role=role, target=target):
                site = getattr(self.sites, role)
                line = f"{method} {target} HTTP/1.1".encode("ascii")
                fields = (
                    (f"Content-Length: {len(body)}".encode("ascii"),)
                    if method == "POST"
                    else ()
                )
                if role == "account":
                    fields += (_JSON,)
                wire = _wire(site, line, _origin(site), *fields, body=body)
                request = self.send(role, wire)
                self.assertEqual(
                    (request.role, request.method, request.target, request.body),
                    (role, method, target, body),
                )

    def test_one_fixed_length_final_reply_uses_the_selected_handler(self):
        handlers = {
            "account": Mock(return_value=transport.NamedResponse(403, b'{"ok":false}')),
            "workspace": Mock(
                return_value=transport.NamedResponse(200, b'{"ok":true}')
            ),
        }
        account, app = self.sites.account, self.sites.workspace
        for role, wire, status, payload in (
            (
                "account",
                _wire(
                    account,
                    _START,
                    _origin(account),
                    _JSON,
                    b"Content-Length: 2",
                    body=b"{}",
                ),
                403,
                b'{"ok":false}',
            ),
            ("workspace", _wire(app, _INFO, _origin(app)), 200, b'{"ok":true}'),
        ):
            with (
                self.subTest(role=role),
                self.exchange(role, wire, handlers=handlers) as (stream, pending),
            ):
                self.assertIsNone(pending.result(timeout=_RECEIVE_TIMEOUT))
                head, divider, body = self.response_bytes(stream).partition(b"\r\n\r\n")
                self.assertEqual(divider, b"\r\n\r\n")
                self.assertEqual(body, payload)  # Includes any unexpected tail.
                status_line, *fields = head.split(b"\r\n")
                self.assertEqual(
                    status_line.split(b" ", 2)[:2],
                    [b"HTTP/1.1", str(status).encode("ascii")],
                )
                lengths = [
                    line for line in fields if line.startswith(b"Content-Length:")
                ]
                self.assertEqual(
                    lengths, [f"Content-Length: {len(payload)}".encode("ascii")]
                )
                self.assertIn(b"Connection: close", fields)
                self.assertIn(b"Cache-Control: no-store", fields)
                for forbidden in (b"Set-Cookie:", b"Transfer-Encoding:"):
                    self.assertFalse(any(line.startswith(forbidden) for line in fields))
                handler = handlers[role]
                handler.assert_called_once()
                self.assertEqual(handler.call_args.args[0].role, role)
                self.assertIsInstance(handler.call_args.kwargs["deadline"], float)
                other = "workspace" if role == "account" else "account"
                handlers[other].assert_not_called()
                handler.reset_mock()

    def test_no_handler_or_reply_for_refused_https_requests(self):
        handlers = {"account": Mock(), "workspace": Mock()}
        account, app = self.sites.account, self.sites.workspace
        info = _wire(app, _INFO, _origin(app))
        for role, wire in (
            ("workspace", info * 2),
            ("workspace", _wire(app, _INFO, _origin(account))),
            (
                "workspace",
                _wire(app, _START, _origin(app), _JSON, b"Content-Length: 4"),
            ),
            ("workspace", _wire(app, _CONFIRM, _origin(app), b"Content-Length: 53")),
            ("workspace", _wire(app, _LOGOUT, _origin(app), b"Content-Length: 1")),
            ("account", _wire(account, _START, _origin(account), b"Content-Length: 4")),
        ):
            with (
                self.subTest(role=role),
                self.exchange(role, wire, handlers=handlers) as (stream, pending),
            ):
                with self.assertRaises(NamedRequestError):
                    pending.result(timeout=3)
                self.assert_closed_without_bytes(stream)
        for handler in handlers.values():
            handler.assert_not_called()

    def test_incomplete_route_body_times_out_without_a_handler_or_response(self):
        handlers = {"account": Mock(), "workspace": Mock()}
        app = self.sites.workspace
        wire = _wire(app, _CONFIRM, _origin(app), b"Content-Length: 54", body=b"A")
        with (
            patch.object(transport, "_REQUEST_TIMEOUT", 1.0),
            self.exchange("workspace", wire, handlers=handlers) as (stream, pending),
        ):
            with self.assertRaises(NamedRequestError):
                pending.result(timeout=3)
            self.assert_closed_without_bytes(stream)
        for handler in handlers.values():
            handler.assert_not_called()

    def test_expired_or_raising_handler_does_not_send_a_reply_or_retry(self):
        app = self.sites.workspace
        wire = _wire(app, _INFO, _origin(app))
        for failure in ("late", "exception"):
            fired = Event()

            def dispatch(_request, *, deadline):
                self.assertGreater(deadline, monotonic())
                fired.set()
                if failure == "exception":
                    raise ValueError("private synthetic handler detail")
                return transport.NamedResponse(200, b"{}")

            def clock():
                return monotonic() + (6 if failure == "late" and fired.is_set() else 0)

            handlers = {"account": Mock(), "workspace": Mock(side_effect=dispatch)}
            with (
                self.subTest(failure=failure),
                patch.object(transport, "monotonic", clock),
                self.exchange("workspace", wire, handlers=handlers) as (
                    stream,
                    pending,
                ),
            ):
                with self.assertRaises(NamedRequestError) as refusal:
                    pending.result(timeout=_RECEIVE_TIMEOUT)
                self.assertNotIn("synthetic handler detail", str(refusal.exception))
                self.assertIsNone(refusal.exception.__context__)
                self.assert_closed_without_bytes(stream)
            self.assertTrue(fired.is_set())
            handlers["workspace"].assert_called_once()
            handlers["account"].assert_not_called()

    def test_later_request_during_dispatch_is_never_parsed(self):
        entered, release = Event(), Event()

        def dispatch(_request, *, deadline):
            entered.set()
            if not release.wait(max(0, deadline - monotonic())):
                raise TimeoutError
            return transport.NamedResponse(200, b"{}")

        handlers = {"account": Mock(), "workspace": Mock(side_effect=dispatch)}
        app = self.sites.workspace
        wire = _wire(app, _INFO, _origin(app))
        with self.exchange("workspace", wire, handlers=handlers) as (stream, pending):
            try:
                self.assertTrue(entered.wait(2))
                stream.sendall(wire)  # Sent after the one surplus probe.
            finally:
                release.set()
            self.assertIsNone(pending.result(timeout=_RECEIVE_TIMEOUT))
            seen = self.response_bytes(stream)
            self.assertLessEqual(seen.count(b"HTTP/1.1 200 OK\r\n"), 1)
        handlers["workspace"].assert_called_once()
        handlers["account"].assert_not_called()

    def test_bad_sni_refuses_before_http(self):
        handlers = {"account": Mock(), "workspace": Mock()}
        for role, hostname in (
            ("account", local.WORKSPACE_HOST),
            ("workspace", local.ACCOUNT_HOST),
            ("workspace", local.LOOPBACK),  # IP literal omits SNI.
            ("account", local.ACCOUNT_HOST.upper()),
            ("account", local.ACCOUNT_HOST + "."),
        ):
            with self.subTest(role=role, name=hostname):
                listener = self.listeners[role]
                pending = self.start(listener, handlers=handlers)
                with self.connect(listener) as peer:
                    with self.assertRaises(ssl.SSLError) as refusal:
                        self.client.wrap_socket(peer, server_hostname=hostname)
                    self.assertNotIsInstance(
                        refusal.exception, ssl.SSLCertVerificationError
                    )
                with self.assertRaises(NamedRequestError):
                    pending.result(timeout=_RECEIVE_TIMEOUT)
        for handler in handlers.values():
            handler.assert_not_called()

    def test_tls_sni_does_not_override_http_authority_role_or_origin(self):
        app, account = self.sites.workspace, self.sites.account
        for reason, site, line, fields in (
            ("wrong host", account, _INFO, (_origin(app),)),
            ("wrong origin", app, _INFO, (_origin(account),)),
            ("wrong role", app, _START, (_origin(app), b"Content-Length: 0")),
            (
                "cross-site handoff",
                app,
                b"POST /oc-box/confirm HTTP/1.1",
                (
                    _origin(account),
                    b"Sec-Fetch-Site: cross-site",
                    b"Sec-Fetch-Mode: navigate",
                    b"Sec-Fetch-Dest: document",
                    b"Content-Length: 0",
                ),
            ),
        ):
            with self.subTest(reason=reason), self.assertRaises(NamedRequestError):
                self.send("workspace", _wire(site, line, *fields))

    def test_plain_http_unexpected_endpoint_and_silent_handshake_are_closed(self):
        listener = self.listeners["workspace"]
        pending = self.start(listener)
        with self.connect(listener) as peer:
            peer.sendall(_wire(self.sites.workspace, _INFO))
            with self.assertRaises(NamedRequestError):
                pending.result(timeout=_RECEIVE_TIMEOUT)

        stranger = self.listener()
        pending = self.start(stranger)
        with self.connect(stranger) as peer:
            with self.assertRaises(NamedRequestError):
                pending.result(timeout=_RECEIVE_TIMEOUT)
            self.assertEqual(peer.recv(1), b"")

        with patch.object(transport, "_REQUEST_TIMEOUT", 0.1):
            pending = self.start(listener)
            with self.connect(listener) as peer:
                with self.assertRaises(NamedRequestError):
                    pending.result(timeout=2)
                self.assertEqual(peer.recv(1), b"")

    def test_real_tls_closes_on_pipelined_or_incomplete_request(self):
        app = self.sites.workspace
        wire = _wire(app, _INFO, _origin(app))
        with self.assertRaises(NamedRequestError):
            self.send("workspace", wire * 2)
        listener = self.listeners["account"]
        pending = self.start(listener)
        with self.connect(listener) as peer:
            with self.client.wrap_socket(
                peer, server_hostname=local.ACCOUNT_HOST
            ) as stream:
                account = self.sites.account
                head = _wire(
                    account, _START, _origin(account), _JSON, b"Content-Length: 4"
                )
                stream.sendall(head + b"abc")
            with self.assertRaises(NamedRequestError):
                pending.result(timeout=_RECEIVE_TIMEOUT)

    def test_transport_refuses_mismatched_validated_file_before_use(self):
        with self.assertRaises(WebTLSError) as refusal:
            transport.NamedHTTPSReader(
                self.sites,
                **{
                    **self.paths,
                    "workspace_cert": self.paths["account_cert"],
                    "workspace_key": self.paths["account_key"],
                },
            )
        self.assertEqual(refusal.exception.code, "certificate_name")


if __name__ == "__main__":
    unittest.main()
