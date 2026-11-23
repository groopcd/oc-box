"""One-shot loopback HTTPS smoke for the unserved named ingress components.

The tiny HTTP adapter below is a test fixture, not a runtime HTTP server.
Bootstrap issuance/acceptance is off-wire; there is no browser, cross-site
receiver, native listener, native pairing request, or change to host trust.
"""

import json
import os
import re
import socket
import ssl
import unittest
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.client import HTTPResponse
from http.cookies import SimpleCookie
from threading import Event
from time import monotonic
from unittest.mock import Mock, call, patch

import test_web_local_origins as certificates
import test_web_pake_flow as pake

from oc_box import web_account_ingress as account
from oc_box import web_admission, web_bootstrap, web_named_local, web_native_pair
from oc_box import web_app_ingress as app
from oc_box import web_local_origins as local

_IO_TIMEOUT = 2.0
_HEAD_LIMIT = 4096
_ENDPOINT = "http://127.0.0.1:50100"  # Identity only; no socket connects here.
_NATIVE_VALUE = "a" * 10 + "." + "b" * 43
_START = json.dumps({"username": "in-scope", "ke1": pake.KE1}).encode("ascii")
_METADATA_PAYLOADS = {
    app.MetadataPermit.READY: b'{"metadata":"ready"}',
    app.MetadataPermit.UNAVAILABLE: b'{"metadata":"unavailable"}',
}


class _BadFraming(ValueError):
    pass


def _read_request(stream):
    """Read one small HTTP/1.1 envelope, keeping the raw target and all fields."""
    deadline = monotonic() + _IO_TIMEOUT

    def read(size):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("Fixture request deadline exceeded.")
        stream.settimeout(remaining)
        # A timeout fails the smoke instead of masquerading as an early 400.
        data = stream.recv(size)
        if not data:
            raise _BadFraming
        return data

    wire = bytearray()
    while b"\r\n\r\n" not in wire:
        if len(wire) == _HEAD_LIMIT:
            raise _BadFraming
        wire.extend(read(_HEAD_LIMIT - len(wire)))
    head, _, body = wire.partition(b"\r\n\r\n")
    line, *fields = head.split(b"\r\n")
    start = re.fullmatch(rb"([A-Za-z]+) ([!-~]+) HTTP/1\.1", line)
    if start is None:
        raise _BadFraming
    method, target = (item.decode("ascii") for item in start.groups())
    headers = []
    lengths = []
    for field in fields:
        match = re.fullmatch(rb"([A-Za-z0-9-]+):([ -~]*)", field)
        if match is None:
            raise _BadFraming
        name, value = (item.decode("ascii") for item in match.groups())
        lower = name.lower()
        if lower in {
            "transfer-encoding",
            "content-encoding",
            "te",
            "trailer",
            "expect",
        }:
            raise _BadFraming
        if lower == "content-length":
            lengths.append(value.strip(" "))
        headers.append((name, value))
    if (
        len(lengths) > 1
        or (method == "POST" and not lengths)
        or (lengths and not re.fullmatch(r"0|[1-9][0-9]{0,3}", lengths[0]))
    ):
        raise _BadFraming
    size = int(lengths[0]) if lengths else 0
    if (
        size > account.MAX_BODY_BYTES
        or (method != "POST" and size != 0)
        or len(body) > size
    ):
        raise _BadFraming
    while len(body) < size:
        body.extend(read(size - len(body)))
    # The fixture always closes. It never parses a second request from this socket.
    return method, target, headers, bytes(body)


@unittest.skipUnless(certificates.OPENSSL, "openssl required for disposable TLS")
class NamedIngressHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        certs = certificates.LocalTLSFileTests()
        cls.addClassCleanup(certs.doCleanups)
        certs.setUp()
        cls.certs = certs
        # Unlike create_default_context(), this loads only the fixture trust roots.
        cls.client_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        for cert in (certs.cert, certs.app_cert):
            cls.client_tls.load_verify_locations(cafile=cert)

    def setUp(self):
        listeners = {}
        for site in ("account", "workspace"):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.addCleanup(listener.close)
            listener.settimeout(_IO_TIMEOUT)
            listener.bind((local.LOOPBACK, 0))
            listener.listen(1)
            listeners[site] = listener
        self.origins = local.LocalOrigins(
            listeners["account"].getsockname()[1],
            listeners["workspace"].getsockname()[1],
        )
        contexts = local.check_tls(
            self.origins,
            account_cert=self.certs.cert,
            account_key=self.certs.key,
            workspace_cert=self.certs.app_cert,
            workspace_key=self.certs.app_key,
        )
        self.servers = {
            site: (listener, getattr(contexts, site))
            for site, listener in listeners.items()
        }

        auth_origin = self.origins.account.value
        app_origin = self.origins.workspace.value
        binding = web_admission.WebBinding(
            pake.WORKSPACE, "synthetic-owner", app_origin
        )
        self.owner = Mock(side_effect=lambda candidate: candidate == binding)
        authority = self._host("workspace")
        native = web_native_pair.NativeCookie(
            binding, _ENDPOINT, authority, _NATIVE_VALUE, owner_current=self.owner
        )
        self.pair = Mock(return_value=native)
        self.native_cookie_name = f"opencode_session_{self.origins.workspace_port}"
        record = pake.accounts.Account("in-scope", (pake.WORKSPACE,), pake.REAL)
        flow = pake.pake.PakeFlow(
            binding,
            store=pake.accounts.AccountStore(pake.SETUP, (record,)),
            backend=pake.SyntheticBackend(),
            sizes=pake.SIZES,
            auth_origin=auth_origin,
        )
        sign_in = web_named_local.LocalNamedSignIn(
            origins=self.origins,
            binding=binding,
            flow=flow,
            owner_current=self.owner,
            pair_native=self.pair,
        )
        self.addCleanup(sign_in.close)
        bootstrap = web_bootstrap.BrowserBootstrap(
            origins=self.origins, binding=binding
        )
        self.addCleanup(bootstrap.close)
        self.translate = Mock(
            side_effect=lambda nonce: bootstrap.app_nonce(
                binding, auth_origin=auth_origin, account_nonce=nonce
            )
        )
        self.account = Mock(
            wraps=account.AccountIngress(
                origins=self.origins, sign_in=sign_in, translate_nonce=self.translate
            )
        )
        # Only the in-memory fixture represents the host role; no host state is used.
        with patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}):
            ingress = app.NamedAppIngress(
                sign_in,
                endpoint=_ENDPOINT,
                claim_bootstrap=lambda nonce: bootstrap.claim_app(binding, nonce),
            )
        self.confirm = Mock(wraps=ingress.confirm)
        self.dispatch = Mock(wraps=ingress.dispatch)
        self.logout = Mock(wraps=ingress.logout)
        self.run_native = Mock(return_value=app.MetadataPermit.READY)

        ticket = bootstrap.issue(binding)
        self.assertIsNotNone(ticket)
        nonce = bootstrap.accept(binding, auth_origin=auth_origin, ticket=ticket.value)
        self.assertIsNotNone(nonce)
        self.account_cookie = "__Host-ocbox-account=" + nonce
        self.bootstrap_cookie = "__Host-ocbox-bootstrap=" + ticket.app_nonce
        self.worker = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.worker.shutdown)

    def _host(self, site):
        return getattr(self.origins, site).value.removeprefix("https://")

    def _headers(self, site, cookie=None, **overrides):
        fields = {"Host": self._host(site), "Origin": getattr(self.origins, site).value}
        if cookie is not None:
            fields["Cookie"] = cookie
        fields.update(overrides)
        return [(name, value) for name, value in fields.items() if value is not None]

    def _serve_one(self, site):
        listener, context = self.servers[site]
        raw, _ = listener.accept()
        with raw:
            raw.settimeout(_IO_TIMEOUT)
            try:
                stream = context.wrap_socket(raw, server_side=True)
            except ssl.SSLError:
                return  # SNI refusals have no HTTP response.
            with stream:
                try:
                    method, target, headers, body = _read_request(stream)
                except _BadFraming:
                    status, fields, payload = 400, (), b"Bad fixture framing."
                else:
                    status, fields, payload = self._dispatch(
                        site,
                        method=method,
                        target=target,
                        headers=headers,
                        body=body,
                        actual_tls=isinstance(stream, ssl.SSLSocket),
                    )
                if not any(name == "Content-Length" for name, _ in fields):
                    fields += (("Content-Length", str(len(payload))),)
                response = [
                    f"HTTP/1.1 {status} {HTTPStatus(status).phrase}",
                    "Connection: close",
                    *(f"{name}: {value}" for name, value in fields),
                ]
                stream.settimeout(_IO_TIMEOUT)
                stream.sendall(
                    ("\r\n".join(response) + "\r\n\r\n").encode("ascii") + payload
                )

    def _dispatch(self, site, **request):
        if site == "account":
            reply = self.account(**request)
            return reply.status, reply.headers, reply.body
        try:
            if request["target"] == app.CONFIRM_PATH:
                token = self.confirm(**request)
                cookie = (
                    f"{app.APP_COOKIE_NAME}={token}; "
                    "Secure; HttpOnly; SameSite=Strict; Path=/"
                )
                return 200, (("Set-Cookie", cookie),), b""
            if request["target"] == app.LOGOUT_PATH:
                return (200 if self.logout(timeout=0, **request) else 503), (), b""
            # Render only the fixture's fixed payload for the closed permit enum.
            permit = self.dispatch(cancel=Event().set, run=self.run_native, **request)
            return 200, (), _METADATA_PAYLOADS[permit]
        except app.AppRequestError:
            return 403, (), b"Named workspace app request refused."

    def _https(self, site, method, target, headers, body=b"", *, server_name=None):
        lines = [f"{method} {target} HTTP/1.1", *(f"{k}: {v}" for k, v in headers)]
        wire = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body
        listener, _ = self.servers[site]
        pending = self.worker.submit(self._serve_one, site)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as raw:
                raw.settimeout(2 * _IO_TIMEOUT)
                raw.connect(listener.getsockname())  # Literal IPv4; no DNS or proxy.
                name = server_name or getattr(self.origins, site).hostname
                with self.client_tls.wrap_socket(raw, server_hostname=name) as stream:
                    stream.sendall(wire)
                    with HTTPResponse(stream) as response:
                        response.begin()
                        payload = response.read(account.MAX_BODY_BYTES + 1)
                        self.assertEqual(response.getheader("Connection"), "close")
                        self.assertEqual(
                            response.getheader("Content-Length"), str(len(payload))
                        )
                        self.assertEqual(stream.recv(1), b"")
                        return response.status, response.headers, payload
        finally:
            pending.result(timeout=3 * _IO_TIMEOUT + 1)

    def _account_post(self, target, body, *, headers=None):
        if headers is None:
            headers = self._headers("account", self.account_cookie)
        return self._https(
            "account",
            "POST",
            target,
            [
                *headers,
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
            ],
            body,
        )

    def _app_post(self, target, cookie, body):
        headers = [
            *self._headers("workspace", cookie),
            ("Content-Length", str(len(body))),
        ]
        return self._https("workspace", "POST", target, headers, body)

    def _sign_in(self):
        nonce = self.account_cookie.partition("=")[2]
        status, headers, body = self._account_post(account.START_PATH, _START)
        self.assertEqual(status, 200)
        self.assertIsNone(headers.get("Set-Cookie"))
        self.assertEqual(self.translate.call_args_list, [call(nonce)] * 2)
        challenge = json.loads(body)
        self.assertEqual(challenge["ke2"], pake.KE2)
        finish = {"challenge_id": challenge["challenge_id"], "ke3": pake.PROOF}
        status, headers, body = self._account_post(
            account.FINISH_PATH, json.dumps(finish).encode("ascii")
        )
        self.assertEqual(status, 200)
        self.assertIsNone(headers.get("Set-Cookie"))
        self.assertIsNone(headers.get("Location"))
        self.assertEqual(self.translate.call_args_list, [call(nonce)] * 4)
        ticket = json.loads(body)["ticket"].encode("ascii")
        self.assertEqual(len(ticket), 54)
        status, _, _ = self._app_post(
            app.CONFIRM_PATH, self.bootstrap_cookie, ticket[:-1]
        )
        self.assertEqual(status, 403)  # Well-framed but not a complete ticket.
        self.pair.assert_not_called()
        status, headers, body = self._app_post(
            app.CONFIRM_PATH, self.bootstrap_cookie, ticket
        )
        self.assertEqual((status, body), (200, b""))
        self.assertIsNone(headers.get("Location"))
        self.assertNotIn(_NATIVE_VALUE, headers["Set-Cookie"])
        token = SimpleCookie(headers["Set-Cookie"])[app.APP_COOKIE_NAME].value
        self.assertEqual(self.pair.call_count, 1)
        return app.APP_COOKIE_NAME + "=" + token

    def test_verified_https_sign_in_passes_only_private_headers_to_metadata_stub(self):
        cookie = self._sign_in()
        fields = self._headers(
            "workspace", f"{cookie}; {self.native_cookie_name}=browser-supplied"
        )
        fields += [("X-Extra", "do-not-forward")]
        routes = ("/api/info", "/api/session/active")
        browser_get = [(name, value) for name, value in fields if name != "Origin"]
        browser_get.append(("Sec-Fetch-Site", "same-origin"))
        permits = (app.MetadataPermit.READY, app.MetadataPermit.UNAVAILABLE)
        self.run_native.side_effect = permits
        for route, headers, permit in zip(routes, (fields, browser_get), permits):
            with self.subTest(route=route):
                status, response, body = self._https("workspace", "GET", route, headers)
                self.assertEqual((status, body), (200, _METADATA_PAYLOADS[permit]))
                self.assertIsNone(response.get("Set-Cookie"))
        upstream = (
            ("Host", self._host("workspace")),
            ("Origin", self.origins.workspace.value),
            ("Cookie", f"{self.native_cookie_name}={_NATIVE_VALUE}"),
        )
        self.assertEqual(
            [call.args[:4] for call in self.run_native.call_args_list],
            [(_ENDPOINT, "GET", route, upstream) for route in routes],
        )
        self.pair.assert_called_once()

    def test_logout_requires_empty_body_and_revokes_app_cookie(self):
        cookie = self._sign_in()
        headers = self._headers("workspace", cookie, Origin=None)
        headers.append(("Sec-Fetch-Site", "same-origin"))
        status, _, _ = self._app_post(app.LOGOUT_PATH, cookie, b"body")
        self.assertEqual(status, 403)
        status, _, _ = self._https("workspace", "GET", "/api/info", headers)
        self.assertEqual(status, 200)
        status, _, body = self._app_post(app.LOGOUT_PATH, cookie, b"")
        self.assertEqual((status, body), (200, b""))
        status, _, _ = self._https("workspace", "GET", "/api/info", headers)
        self.assertEqual(status, 403)
        self.run_native.assert_called_once()
        self.pair.assert_called_once()

    def test_ambiguous_framing_refused_before_either_ingress(self):
        cases = (
            ("duplicate length", (("Content-Length", "4"), ("content-length", "4"))),
            ("conflicting lengths", (("Content-Length", "4"), ("content-length", "8"))),
            (
                "transfer encoding with length",
                (("Transfer-Encoding", "chunked"), ("Content-Length", "4")),
            ),
            (
                "content encoding",
                (("Content-Encoding", "gzip"), ("Content-Length", "4")),
            ),
            (
                "expect interim reply",
                (("Expect", "100-continue"), ("Content-Length", "4")),
            ),
            ("oversized", (("Content-Length", str(account.MAX_BODY_BYTES + 1)),)),
            ("noncanonical length", (("Content-Length", "04"),)),
            ("missing length", ()),
        )
        for site, target, cookie in (
            ("account", account.START_PATH, self.account_cookie),
            ("workspace", app.CONFIRM_PATH, self.bootstrap_cookie),
        ):
            for label, framing in cases:
                with self.subTest(site=site, framing=label):
                    # Headers only: an early refusal must not wait for body bytes.
                    headers = [
                        *self._headers(site, cookie),
                        ("Content-Type", "application/json"),
                    ]
                    status, _, _ = self._https(
                        site, "POST", target, [*headers, *framing]
                    )
                    self.assertEqual(status, 400)
        headers = [*self._headers("workspace"), ("Content-Length", "4")]
        status, _, _ = self._https("workspace", "GET", "/api/info", headers, b"body")
        self.assertEqual(status, 400)
        self.account.assert_not_called()
        self.confirm.assert_not_called()
        self.dispatch.assert_not_called()
        self.logout.assert_not_called()
        self.owner.assert_not_called()

    def test_sni_mismatch_stops_before_http(self):
        for site, wrong in (
            ("account", local.WORKSPACE_HOST),
            ("workspace", local.ACCOUNT_HOST),
            ("workspace", local.LOOPBACK),  # IP literals send no SNI.
        ):
            with (
                self.subTest(site=site, sni=wrong),
                self.assertRaises(ssl.SSLError) as failure,
            ):
                self._https(
                    site, "GET", "/api/info", self._headers(site), server_name=wrong
                )
            self.assertNotIsInstance(failure.exception, ssl.SSLCertVerificationError)
        self.account.assert_not_called()
        self.confirm.assert_not_called()
        self.dispatch.assert_not_called()
        self.logout.assert_not_called()

    def test_wrong_http_authority_refused_on_verified_tls(self):
        for site, other in (("account", "workspace"), ("workspace", "account")):
            cookie = self.account_cookie if site == "account" else self._sign_in()
            self.translate.reset_mock()
            self.owner.reset_mock()
            headers = self._headers(site, cookie)
            origin, other_origin = (
                getattr(self.origins, site),
                getattr(self.origins, other),
            )
            wrong_port = f"{origin.hostname}:{other_origin.port}"
            fetch_site = "same-origin" if site == "account" else "none"
            wrong = (
                ("Host", self._headers(site, cookie, Host=self._host(other))),
                ("Host port", self._headers(site, cookie, Host=wrong_port)),
                ("Origin", self._headers(site, cookie, Origin=other_origin.value)),
                (
                    "Origin port",
                    self._headers(site, cookie, Origin=f"https://{wrong_port}"),
                ),
                (
                    "Origin scheme",
                    self._headers(site, cookie, Origin=f"http://{self._host(site)}"),
                ),
                ("duplicate Host", [*headers, ("host", self._host(site))]),
                (
                    "missing Origin",
                    [
                        *self._headers(site, cookie, Origin=None),
                        ("Sec-Fetch-Site", fetch_site),
                    ],
                ),
            )
            for label, fields in wrong:
                with self.subTest(site=site, authority=label):
                    if site == "account":
                        status, _, _ = self._account_post(
                            account.START_PATH, _START, headers=fields
                        )
                    else:
                        status, _, _ = self._https(site, "GET", "/api/info", fields)
                    self.assertEqual(status, 403)
            self.translate.assert_not_called()
            self.owner.assert_not_called()
        self.run_native.assert_not_called()

    def test_raw_targets_native_auth_and_native_cookie_are_not_app_authority(self):
        cookie = self._sign_in()
        for method, target in (
            ("GET", "/auth/"),
            ("GET", "/auth/connect/synthetic"),
            ("POST", "/auth/logout"),
            ("GET", "/api/pair"),
            ("POST", "/api/pair"),
            ("GET", "/api/auth"),
            ("GET", "//api/info"),
            ("GET", "/api/%69nfo"),
            ("GET", "/api/info?"),
            ("GET", self.origins.workspace.value + "/api/info"),
        ):
            with self.subTest(method=method, target=target):
                headers = self._headers("workspace", cookie)
                if method == "POST":
                    headers.append(("Content-Length", "0"))
                status, response, _ = self._https("workspace", method, target, headers)
                self.assertEqual(status, 403)
                self.assertIsNone(response.get("Set-Cookie"))
                self.assertIsNone(response.get("Location"))
        fields = self._headers(
            "workspace", f"{self.native_cookie_name}={_NATIVE_VALUE}"
        )
        status, _, _ = self._https("workspace", "GET", "/api/info", fields)
        self.assertEqual(status, 403)
        self.run_native.assert_not_called()
        self.pair.assert_called_once()
