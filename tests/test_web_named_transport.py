"""Local synthetic wire checks for the unserved HTTPS request reader."""

import socket
import ssl
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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
            origin, _START, _origin(origin), b"CoNtEnT-LeNgTh:  4 ", body=b"fake"
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
                head = _wire(origin, _START, _origin(origin), *fields)
                stream = _chunks(head, b"fake")
                with self.assertRaises(NamedRequestError):
                    self.read(stream)
                self.assertEqual(stream.recv.call_count, (len(head) + 4095) // 4096)

    def test_truncation_surplus_and_pipelining_are_not_admitted(self):
        origin = self.sites.account
        head = _wire(origin, _START, _origin(origin), b"Content-Length: 4")
        pipelined = _wire(origin, _START, _origin(origin), b"Content-Length: 0")
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
        head = _wire(origin, _START, _origin(origin), b"Content-Length: 8192")
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
        for count in (61, 62):  # Three fixed fields, plus the synthetic fields.
            fields = tuple(f"X-{i}: x".encode("ascii") for i in range(count))
            wire = _wire(origin, _START, _origin(origin), b"Content-Length: 0", *fields)
            if count == 61:
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

    def test_absolute_deadline_does_not_restart_when_head_or_body_progresses(self):
        origin = self.sites.account
        head = _wire(origin, _START, _origin(origin), b"Content-Length: 4")
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

    def start(self, listener):
        def receive():
            peer, _ = listener.accept()
            with peer:
                try:
                    return self.reader.read_one(peer)
                finally:
                    self.assertEqual(peer.fileno(), -1)

        return self.worker.submit(receive)

    @contextmanager
    def connect(self, listener):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as peer:
            peer.settimeout(_RECEIVE_TIMEOUT)
            peer.connect(listener.getsockname())
            yield peer

    def send(self, role, wire):
        listener = self.listeners[role]
        pending = self.start(listener)
        with self.connect(listener) as peer:
            hostname = getattr(self.sites, role).hostname
            with self.client.wrap_socket(peer, server_hostname=hostname) as stream:
                self.assertEqual(stream.selected_alpn_protocol(), "http/1.1")
                stream.sendall(wire)
                try:
                    return pending.result(timeout=_RECEIVE_TIMEOUT)
                finally:
                    try:
                        self.assertEqual(stream.recv(1), b"")
                    except ConnectionResetError:
                        # Closing with unread bytes can cause TCP reset, also
                        # conclusive that the peer is gone without a response.
                        pass

    def test_each_endpoint_reads_only_known_role_routes_over_verified_tls(self):
        for role, method, target, body in (
            ("account", "POST", "/start", b"fake"),
            ("account", "POST", "/finish", b""),
            ("workspace", "GET", "/api/info", b""),
            ("workspace", "GET", "/api/session/active", b""),
            ("workspace", "POST", "/oc-box/confirm", b"fake"),
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
                wire = _wire(site, line, _origin(site), *fields, body=body)
                request = self.send(role, wire)
                self.assertEqual(
                    (request.role, request.method, request.target, request.body),
                    (role, method, target, body),
                )

    def test_bad_sni_refuses_before_http(self):
        for role, hostname in (
            ("account", local.WORKSPACE_HOST),
            ("workspace", local.ACCOUNT_HOST),
            ("workspace", local.LOOPBACK),  # IP literal omits SNI.
            ("account", local.ACCOUNT_HOST.upper()),
            ("account", local.ACCOUNT_HOST + "."),
        ):
            with self.subTest(role=role, name=hostname):
                listener = self.listeners[role]
                pending = self.start(listener)
                with self.connect(listener) as peer:
                    with self.assertRaises(ssl.SSLError) as refusal:
                        self.client.wrap_socket(peer, server_hostname=hostname)
                    self.assertNotIsInstance(
                        refusal.exception, ssl.SSLCertVerificationError
                    )
                with self.assertRaises(NamedRequestError):
                    pending.result(timeout=_RECEIVE_TIMEOUT)

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
                head = _wire(account, _START, _origin(account), b"Content-Length: 4")
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
