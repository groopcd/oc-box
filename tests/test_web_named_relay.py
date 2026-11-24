"""Synthetic one-shot HTTPS and private upstream; no listener in the component."""

import os
import socket
import ssl
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from unittest.mock import Mock, patch

import test_web_local_origins as certificates
import test_web_pake_flow as pake

from oc_box import (
    oc_box as box,
    web_admission,
    web_bootstrap,
    web_local_origins,
    web_named_local,
    web_named_owner,
    web_named_transport,
    web_native_pair,
    workspace_service,
)
from oc_box import web_app_ingress as app
from oc_box import web_named_relay as relay
from oc_box.web_named_headers import NamedRequestError


_TIMEOUT = 3
_ACTIVE = b'{ "data" : { "ses_synthetic" : {"type":"running"} } }\n'
_NATIVE_VALUE = "f" * 10 + "." + "s" * 43
_INFO = "/api/info"


def _reply(body, *headers, status=b"200 Synthetic", length=None):
    length = str(len(body)).encode("ascii") if length is None else length
    fields = (
        b"HTTP/1.1 " + status,
        b"Content-Type: application/json",
        b"Content-Length: " + length,
        *headers,
    )
    return b"\r\n".join((*fields, b"", b"")) + body


@unittest.skipUnless(certificates.OPENSSL, "openssl required for disposable TLS")
class NamedNativeRelayTests(unittest.TestCase):
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
        cls.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        cls.tls.load_verify_locations(cafile=certs.app_cert)

    def listener(self):
        peer = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(peer.close)
        peer.settimeout(_TIMEOUT)
        peer.bind(("127.0.0.1", 0))
        peer.listen(1)
        return peer

    def setUp(self):
        self.native = self.listener()
        self.endpoint = f"http://127.0.0.1:{self.native.getsockname()[1]}"
        self.browser = self.listener()
        account = self.listener()
        self.origins = web_local_origins.LocalOrigins(
            account.getsockname()[1], self.browser.getsockname()[1]
        )
        self.origin = self.origins.workspace.value
        self.authority = self.origin.removeprefix("https://")
        self.reader = web_named_transport.NamedHTTPSReader(self.origins, **self.paths)
        binding = self.binding = web_admission.WebBinding(
            pake.WORKSPACE, "synthetic-owner", self.origin
        )
        self.owner = Mock(side_effect=lambda candidate: candidate == binding)
        native = self.native_cookie = web_native_pair.NativeCookie(
            binding,
            self.endpoint,
            self.authority,
            _NATIVE_VALUE,
            owner_current=self.owner,
        )
        self.pair = Mock(return_value=native)
        record = pake.accounts.Account("in-scope", (pake.WORKSPACE,), pake.REAL)
        flow = pake.pake.PakeFlow(
            binding,
            store=pake.accounts.AccountStore(pake.SETUP, (record,)),
            backend=pake.SyntheticBackend(),
            sizes=pake.SIZES,
            auth_origin=self.origins.account.value,
        )
        self.sign_in = web_named_local.LocalNamedSignIn(
            origins=self.origins,
            binding=binding,
            flow=flow,
            owner_current=self.owner,
            pair_native=self.pair,
        )
        self.admission = self.sign_in.admission
        self.boot = web_bootstrap.BrowserBootstrap(
            origins=self.origins, binding=binding
        )
        self.addCleanup(self.boot.close)
        with patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}):
            self.ingress = app.NamedAppIngress(
                self.sign_in,
                endpoint=self.endpoint,
                claim_bootstrap=lambda nonce: self.boot.claim_app(binding, nonce),
            )
        self.addCleanup(self.ingress.close)
        self.dial = Mock(side_effect=self.connect_native)
        self.relay = relay.NamedNativeRelay(self.ingress, connect_native=self.dial)
        for purpose in ("https", "upstream"):
            pool = ThreadPoolExecutor(max_workers=1)
            self.addCleanup(pool.shutdown)
            setattr(self, purpose, pool)

    def connect_native(self, endpoint, *, track, check, deadline):
        self.assertEqual(endpoint, self.endpoint)
        check()
        peer = track(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        check()
        peer.settimeout(deadline - monotonic())
        peer.connect(self.native.getsockname())
        self.upstream_socket = peer
        check()

    @contextmanager
    def managed(self, *, before=None, launched=None, worker=None, executable=None):
        """Replace only the Podman invocation, not the socket/process transport."""
        identity = web_named_owner.OwnerIdentity(
            self.binding.workspace_id,
            "b" * 32,
            "d" * 64,
            web_native_pair._VERSION,
            self.endpoint,
        )

        def connect(endpoint, **request):
            self.assertEqual(endpoint, identity.endpoint)
            if before is not None:
                before()
            web_named_owner.connect_workspace_native(box, identity, **request)

        self.dial.side_effect = connect
        children = []
        popen = subprocess.Popen

        def launch(command, **options):
            self.assertEqual(
                command,
                [
                    "podman",
                    "exec",
                    "-i",
                    "--workdir",
                    "/",
                    identity.container_id,
                    "/usr/bin/env",
                    "-i",
                    "PATH=/usr/bin:/bin",
                    "python3",
                    "-I",
                    "-S",
                    "-B",
                    "-c",
                    workspace_service.PROXY,
                    str(self.native.getsockname()[1]),
                ],
            )
            self.assertEqual(options["env"], box.maintenance_host_env())
            self.assertNotIn("SYNTHETIC_PROVIDER_TOKEN", options["env"])
            self.assertNotIn("OPENCODE_PASSWORD", options["env"])
            self.assertNotIn("OC_BOX_CONTAINER", options["env"])
            self.assertEqual(options["stderr"], subprocess.DEVNULL)
            self.assertTrue(options["close_fds"])
            self.assertIs(options["stdin"], options["stdout"])
            local = command[6:]
            if worker is not None:
                local[-2] = worker
            child = popen(local, executable=executable, **options)
            children.append(child)
            if launched is not None:
                launched()
            return child

        with (
            patch.dict(
                os.environ,
                {
                    "OC_BOX_CONTAINER": "0",
                    "SYNTHETIC_PROVIDER_TOKEN": "synthetic-not-for-podman",
                    "OPENCODE_PASSWORD": "synthetic-not-for-relay",
                },
            ),
            patch.object(relay.subprocess, "Popen", side_effect=launch) as start,
        ):
            yield start, children

    def assert_reaped(self, child, status=0):
        self.assertEqual(child.returncode, status)
        with self.assertRaises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)

    def issue(self, native=None):
        return self.admission.issue(
            self.binding,
            principal="in-scope",
            native_cookie=self.native_cookie if native is None else native,
        )

    def bootstrap(self):
        ticket = self.boot.issue(self.binding)
        cookie = self.boot.accept(
            self.binding, auth_origin=self.origins.account.value, ticket=ticket.value
        )
        self.assertTrue(cookie)
        return ticket.app_nonce

    def confirm(self, ticket, nonce):
        return self.ingress.confirm(
            method="POST",
            target=app.CONFIRM_PATH,
            headers=(
                ("Host", self.authority),
                ("Origin", self.origin),
                ("Cookie", f"__Host-ocbox-bootstrap={nonce}"),
                ("Content-Length", str(len(ticket))),
            ),
            actual_tls=True,
            body=ticket.encode("ascii"),
        )

    def cookie(self, token):
        return f"{app.APP_COOKIE_NAME}={token}"

    def session(self, token):
        return self.admission.lookup(self.binding, token)

    def info(self):
        return (
            f'{{ "version" : "2.3.2", "pid" : 42, "urls" : ["{self.endpoint}"] }}\n'
        ).encode()

    def native_response(self, reply, *, ready=None, hold=False):
        def serve():
            peer, _ = self.native.accept()
            with peer:
                peer.settimeout(_TIMEOUT)
                request = bytearray()
                while not request.endswith(b"\r\n\r\n") and len(request) < 1024:
                    fragment = peer.recv(1024 - len(request))
                    self.assertTrue(fragment)
                    request.extend(fragment)
                self.assertTrue(request.endswith(b"\r\n\r\n"))
                if reply is not None:
                    peer.sendall(reply)
                if ready is not None:
                    ready.set()
                if reply is None or hold:
                    self.assertEqual(peer.recv(1), b"")  # Cancellation, no retry.
                return bytes(request)

        return self.upstream.submit(serve)

    @contextmanager
    def exchange(
        self, *, target=_INFO, fields=(), token=None, method="GET", origin=True
    ):
        def serve():
            peer, _ = self.browser.accept()
            try:
                self.relay.respond_one(self.reader, peer)
            finally:
                self.assertEqual(peer.fileno(), -1)

        pending = self.https.submit(serve)
        headers = [f"Host: {self.authority}"]
        if origin:
            headers.append("Origin: " + self.origin)
        if token is not None:
            headers.append("Cookie: " + self.cookie(token))
        wire = "\r\n".join(
            (f"{method} {target} HTTP/1.1", *headers, *fields, "", "")
        ).encode("ascii")
        with socket.create_connection(
            self.browser.getsockname(), timeout=_TIMEOUT
        ) as peer:
            with self.tls.wrap_socket(
                peer, server_hostname=self.origins.workspace.hostname
            ) as stream:
                stream.sendall(wire)
                yield stream, pending

    def read_response(self, stream):
        seen = bytearray()
        while len(seen) < 9000:
            try:
                chunk = stream.recv(9000 - len(seen))
            except ConnectionResetError:
                break
            if not chunk:
                break
            seen.extend(chunk)
        self.assertLess(len(seen), 9000)
        return bytes(seen)

    def assert_refused(self, stream, pending):
        with self.assertRaisesRegex(
            NamedRequestError, r"^Named web request was not admitted\.$"
        ):
            pending.result(timeout=_TIMEOUT)
        self.assertEqual(self.read_response(stream), b"")

    def assert_no_upstream(self):
        self.native.setblocking(False)
        try:
            with self.assertRaises(BlockingIOError):
                self.native.accept()
        finally:
            self.native.settimeout(_TIMEOUT)

    def test_only_confirmed_grant_receives_exact_native_metadata_with_host_headers(
        self,
    ):
        matched, other = self.bootstrap(), self.bootstrap()
        challenge = self.sign_in.start(
            browser_nonce=matched, username="in-scope", ke1=pake.KE1
        )
        ticket = self.sign_in.finish(
            browser_nonce=matched, challenge_id=challenge.challenge_id, ke3=pake.PROOF
        ).value
        with self.assertRaises(app.AppRequestError):
            self.confirm(ticket, other)
        self.pair.assert_not_called()
        with self.exchange(
            fields=(f"Cookie: __Host-ocbox-bootstrap={other}",)
        ) as result:
            self.assert_refused(*result)
        self.dial.assert_not_called()
        token = self.confirm(ticket, matched)
        self.pair.assert_called_once_with(self.binding, self.origins.workspace)
        native = self.native_cookie.upstream_cookie(
            binding=self.binding, endpoint=self.endpoint, origin=self.origins.workspace
        )
        for route, body in ((_INFO, self.info()), ("/api/session/active", _ACTIVE)):
            with self.subTest(route=route):
                pending = self.native_response(
                    _reply(
                        body,
                        b"Set-Cookie: " + native.encode(),
                        b"Set-Cookie2: another=native",
                        b"Cache-Control: public",
                        b"X-Native: synthetic",
                    )
                )
                fields = (
                    f"Cookie: {self.cookie(token)}; {native.split('=')[0]}=browser",
                    "Accept: text/html",
                    "X-Browser: do-not-forward",
                )
                with self.exchange(target=route, fields=fields) as (stream, exchanged):
                    response = self.read_response(stream)
                    exchanged.result(timeout=_TIMEOUT)
                head, _, payload = response.partition(b"\r\n\r\n")
                self.assertEqual(payload, body)
                self.assertEqual(
                    head.split(b"\r\n"),
                    [
                        b"HTTP/1.1 200 OK",
                        *web_named_transport._RESPONSE_HEADERS.rstrip(b"\r\n").split(
                            b"\r\n"
                        ),
                        f"Content-Length: {len(body)}".encode(),
                    ],
                )
                self.assertNotIn(_NATIVE_VALUE.encode(), response)
                self.assertEqual(
                    pending.result(timeout=_TIMEOUT),
                    (
                        f"GET {route} HTTP/1.1\r\nHost: {self.authority}\r\n"
                        f"Origin: {self.origin}\r\nCookie: {native}\r\n"
                        "Accept: application/json\r\nAccept-Encoding: identity\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode(),
                )
                self.assertEqual(self.upstream_socket.fileno(), -1)

    def test_refused_context_route_and_session_do_not_dial(self):
        token = self.issue()
        wrong_scope = self.admission.issue(
            replace(self.binding, owner_generation="another"),
            principal="in-scope",
            native_cookie=self.native_cookie,
        )
        self.assertIsNone(wrong_scope)
        for label, tweaks in (
            ("method", {"method": "HEAD"}),
            ("query", {"target": _INFO + "?x=1"}),
            ("normalized path", {"target": "/api/%69nfo"}),
            ("pairing", {"target": "/api/pair"}),
            ("asset", {"target": "/"}),
            (
                "logout",
                {
                    "target": app.LOGOUT_PATH,
                    "method": "POST",
                    "fields": ("Content-Length: 0",),
                },
            ),
            (
                "missing exact Origin",
                {"origin": False, "fields": ("Sec-Fetch-Site: same-origin",)},
            ),
            (
                "foreign Origin",
                {"origin": False, "fields": ("Origin: " + self.origins.account.value,)},
            ),
            ("no cookie", {"token": None}),
            (
                "browser native cookie",
                {
                    "token": None,
                    "fields": ("Cookie: opencode_session=" + _NATIVE_VALUE,),
                },
            ),
            (
                "duplicate browser cookie",
                {
                    "token": None,
                    "fields": (f"Cookie: {self.cookie(token)}; {self.cookie(token)}",),
                },
            ),
            ("native Basic", {"fields": ("Authorization: Basic synthetic",)}),
            ("upgrade", {"fields": ("Upgrade: websocket", "Connection: Upgrade")}),
        ):
            with (
                self.subTest(case=label),
                self.exchange(**({"token": token} | tweaks)) as result,
            ):
                self.assert_refused(*result)
        self.dial.assert_not_called()
        self.owner.assert_not_called()
        with self.exchange(token="q" * 42 + "A") as result:
            self.assert_refused(*result)
        self.dial.assert_not_called()
        self.assert_no_upstream()

    def test_wrong_native_cookie_scope_never_dials_or_repairs(self):
        with self.exchange(token=self.issue(native=False)) as result:
            self.assert_refused(*result)
        mismatched = web_native_pair.NativeCookie(
            replace(self.binding, owner_generation="retired"),
            self.endpoint,
            self.authority,
            _NATIVE_VALUE,
            owner_current=self.owner,
        )
        token = self.issue(native=mismatched)
        with self.exchange(token=token) as result:
            self.assert_refused(*result)
        self.assertIsNone(self.session(token))
        self.pair.assert_not_called()
        self.dial.assert_not_called()
        self.assert_no_upstream()

    def test_lost_owner_never_dials_or_rebinds(self):
        revoked = self.issue()
        self.owner.side_effect = lambda _: False
        with self.exchange(token=revoked) as result:
            self.assert_refused(*result)
        self.assertIsNone(self.session(revoked))
        self.owner.side_effect = lambda _: True
        self.assertIsNone(self.issue())
        self.dial.assert_not_called()
        self.assert_no_upstream()

    def test_unverified_native_reply_never_reaches_browser(self):
        good = self.info()
        token = self.issue()
        for label, wire in (
            (
                "challenge",
                _reply(good, b"WWW-Authenticate: Basic", status=b"401 Private"),
            ),
            ("challenge on success", _reply(good, b"WWW-Authenticate: Basic")),
            ("redirect", _reply(good, b"Location: /auth/login", status=b"302 Private")),
            ("redirect on success", _reply(good, b"Location: /auth/login")),
            ("untrusted reason", _reply(good, status=b"101 Private")),
            ("short", _reply(good, length=b"8192")),
            ("overlong body", _reply(good, length=b"8193")),
            ("surplus", _reply(good) + b"x"),
            (
                "duplicate length",
                _reply(good, b"Content-Length: " + str(len(good)).encode()),
            ),
            ("duplicate type", _reply(good, b"content-type: application/json")),
            ("ambiguous length", _reply(good, length=b"12, 12")),
            ("chunked", _reply(good, b"Transfer-Encoding: chunked")),
            ("compressed", _reply(good, b"Content-Encoding: gzip")),
            ("folded header", _reply(good, b" continued: text")),
            ("overlong head", _reply(good, b"X-Extra: " + b"a" * 8192)),
            ("field count", _reply(good, *([b"X-Extra: a"] * 63))),
            ("extra metadata", _reply(good.replace(b"{", b'{"extra":"private",', 1))),
            ("foreign metadata", _reply(good.replace(b"127.0.0.1", b"127.0.0.2"))),
            ("wrong native patch", _reply(good.replace(b"2.3.2", b"2.3.3"))),
            ("duplicate key", _reply(good.replace(b"{", b'{"pid":42,', 1))),
            ("invalid JSON", _reply(b'{"version":"\xff"}')),
            ("invalid active shape represented as info", _reply(_ACTIVE)),
        ):
            with self.subTest(case=label):
                pending = self.native_response(wire)
                with self.exchange(token=token) as result:
                    self.assert_refused(*result)
                pending.result(timeout=_TIMEOUT)
                self.assertEqual(self.upstream_socket.fileno(), -1)

    def test_content_length_without_upstream_eof_cannot_publish(self):
        complete = Event()
        token = self.issue()
        with patch.object(web_named_transport, "_REQUEST_TIMEOUT", 1.0):
            upstream = self.native_response(
                _reply(self.info()), ready=complete, hold=True
            )
            with self.exchange(token=token) as result:
                self.assertTrue(complete.wait(_TIMEOUT))
                self.assert_refused(*result)
            upstream.result(timeout=_TIMEOUT)
        self.assertEqual(self.upstream_socket.fileno(), -1)

    def test_revocation_winning_before_connection_registration_never_dials(self):
        looked_up, proceed = Event(), Event()
        token = self.issue()
        session = self.session(token)
        lookup = self.admission.lookup

        def delayed(*args):
            cached = lookup(*args)
            looked_up.set()
            self.assertTrue(proceed.wait(_TIMEOUT))
            return cached

        with patch.object(self.admission, "lookup", side_effect=delayed):
            with self.exchange(token=token) as result:
                try:
                    self.assertTrue(looked_up.wait(_TIMEOUT))
                    revoked = self.admission.begin_revocation(self.binding, session)
                    self.assertTrue(revoked.wait_drained(0))
                finally:
                    proceed.set()
                self.assert_refused(*result)
        self.dial.assert_not_called()
        self.assert_no_upstream()

    def test_registered_startup_is_cancelled_before_it_can_connect(self):
        starting, proceed = Event(), Event()
        token = self.issue()
        session = self.session(token)
        sockets = []

        def delayed(endpoint, *, track, check, deadline):
            starting.set()
            self.assertTrue(proceed.wait(_TIMEOUT))
            peer = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sockets.append(peer)
            peer = track(peer)
            check()
            peer.settimeout(deadline - monotonic())
            peer.connect(self.native.getsockname())

        self.dial.side_effect = delayed
        with self.exchange(token=token) as result:
            try:
                self.assertTrue(starting.wait(_TIMEOUT))
                revoked = self.admission.begin_revocation(self.binding, session)
                self.assertTrue(revoked.revoked)
                self.assertFalse(revoked.wait_drained(0))
            finally:
                proceed.set()
            self.assert_refused(*result)
        self.assertTrue(revoked.wait_drained(0))
        self.assertEqual([peer.fileno() for peer in sockets], [-1])
        self.assert_no_upstream()

    def test_cookie_resolution_is_tracked_before_any_dial(self):
        resolving, proceed = Event(), Event()
        token = self.issue()
        session = self.session(token)
        upstream_cookie = web_native_pair.NativeCookie.upstream_cookie

        def delayed(*args, **kwargs):
            resolving.set()
            self.assertTrue(proceed.wait(_TIMEOUT))
            return upstream_cookie(*args, **kwargs)

        with patch.object(web_native_pair.NativeCookie, "upstream_cookie", delayed):
            with self.exchange(token=token) as result:
                try:
                    self.assertTrue(resolving.wait(_TIMEOUT))
                    revoked = self.admission.begin_revocation(self.binding, session)
                    self.assertTrue(revoked.revoked)
                    self.assertFalse(revoked.wait_drained(0))
                finally:
                    proceed.set()
                self.assert_refused(*result)
        self.assertTrue(revoked.wait_drained(0))
        self.dial.assert_not_called()
        self.assert_no_upstream()

    def test_failed_connector_closes_the_socket_it_already_handed_over(self):
        token = self.issue()
        sockets = []

        def failed(endpoint, *, track, check, deadline):
            sockets.append(track(socket.socket(socket.AF_INET, socket.SOCK_STREAM)))
            raise OSError("private diagnostic")

        self.dial.side_effect = failed
        with self.exchange(token=token) as result:
            self.assert_refused(*result)
        self.dial.assert_called_once()
        self.assertEqual([peer.fileno() for peer in sockets], [-1])
        self.assert_no_upstream()

    def test_browser_revocation_interrupts_io_and_waits_for_cleanup(self):
        requested, closing, proceed = Event(), Event(), Event()
        token, independent = self.issue(), self.issue()
        session = self.session(token)
        handles = []
        close = relay._RegisteredIO.close

        def delayed(io):
            handles.extend((io.browser, io.upstream))
            closing.set()
            try:
                self.assertTrue(proceed.wait(_TIMEOUT))
            finally:
                close(io)

        upstream = self.native_response(None, ready=requested)
        with (
            patch.object(relay._RegisteredIO, "close", delayed),
            patch.object(ssl.SSLSocket, "shutdown", side_effect=AssertionError),
        ):
            with self.exchange(token=token) as result:
                try:
                    self.assertTrue(requested.wait(_TIMEOUT))
                    revoked = self.admission.begin_revocation(self.binding, session)
                    self.assertTrue(revoked.revoked)
                    self.assertTrue(closing.wait(_TIMEOUT))
                    upstream.result(timeout=_TIMEOUT)  # Private peer saw shutdown.
                    self.assertFalse(revoked.wait_drained(0))
                finally:
                    proceed.set()
                self.assert_refused(*result)
        self.assertTrue(revoked.wait_drained(0))
        self.assertEqual([peer.fileno() for peer in handles], [-1, -1])

        self.dial.reset_mock()
        with self.exchange(token=token) as result:
            self.assert_refused(*result)
        self.dial.assert_not_called()
        # A distinct app grant may have the identical private native cookie.
        upstream = self.native_response(_reply(self.info()))
        with self.exchange(token=independent) as (stream, exchanged):
            self.assertTrue(self.read_response(stream).endswith(self.info()))
            exchanged.result(timeout=_TIMEOUT)
        upstream.result(timeout=_TIMEOUT)

    def test_owner_timer_fences_an_already_connected_request(self):
        requested = Event()
        token = self.issue()
        upstream = self.native_response(None, ready=requested)
        with self.exchange(token=token) as result:
            self.assertTrue(requested.wait(_TIMEOUT))
            self.owner.side_effect = lambda _: False
            with self.assertRaises(app.StaleAppStateError):
                self.ingress.expire()
            self.assert_refused(*result)
        upstream.result(timeout=_TIMEOUT)
        self.assertEqual(self.upstream_socket.fileno(), -1)
        self.assertIsNone(self.session(token))

    def test_validated_response_does_not_escape_after_revocation(self):
        ready, proceed = Event(), Event()
        token = self.issue()
        session = self.session(token)
        write_response = web_named_transport._write_response

        def delayed(*args, **kwargs):
            ready.set()
            self.assertTrue(proceed.wait(_TIMEOUT))
            write_response(*args, **kwargs)

        upstream = self.native_response(_reply(self.info()))
        with patch.object(web_named_transport, "_write_response", side_effect=delayed):
            with self.exchange(token=token) as result:
                try:
                    self.assertTrue(ready.wait(_TIMEOUT))
                    revoked = self.admission.begin_revocation(self.binding, session)
                    self.assertTrue(revoked.revoked)
                    self.assertFalse(revoked.wait_drained(0))
                finally:
                    proceed.set()
                self.assert_refused(*result)
        upstream.result(timeout=_TIMEOUT)
        self.assertTrue(revoked.wait_drained(0))

    def test_managed_relay_waits_for_exact_child_after_complete_upstream_eof(self):
        # Run the real proxy, but let the subprocess close both of its stdio
        # descriptors and pause before exiting. EOF alone must not publish.
        with tempfile.TemporaryDirectory() as root:
            ready, exit_now = Path(root) / "ready", Path(root) / "exit"
            worker = f"""
import os, time
from pathlib import Path
try:
    exec({workspace_service.PROXY!r})
except SystemExit as result:
    s.close()
    os.close(0)
    os.close(1)
    Path({str(ready)!r}).touch()
    deadline = time.monotonic() + 2
    while not Path({str(exit_now)!r}).exists():
        if time.monotonic() >= deadline:
            raise SystemExit(4)
        time.sleep(0.01)
    raise
"""
            with self.managed(worker=worker) as (start, children):
                upstream = self.native_response(_reply(self.info()))
                with self.exchange(token=self.issue()) as (stream, pending):
                    try:
                        end = monotonic() + 1.5
                        while not ready.exists() and monotonic() < end:
                            sleep(0.01)
                        self.assertTrue(ready.exists())
                        stream.settimeout(0.1)
                        with self.assertRaises(TimeoutError):
                            stream.recv(1)
                        self.assertFalse(pending.done())
                        self.assertIsNone(children[0].returncode)
                    finally:
                        exit_now.touch()
                        stream.settimeout(_TIMEOUT)
                    self.assertTrue(self.read_response(stream).endswith(self.info()))
                    pending.result(timeout=_TIMEOUT)
                start.assert_called_once()
                self.assert_reaped(children[0])
                self.assertIn(b"Connection: close\r\n", upstream.result(_TIMEOUT))
        self.assertFalse(self.relay.wait_drained(0))  # Not yet fenced for shutdown.
        self.ingress.close()
        self.assertTrue(self.relay.wait_drained(0))

    def test_managed_logout_before_launch_does_not_start_a_child(self):
        token = self.issue()
        session = self.session(token)
        registered, release = Event(), Event()

        def before():
            registered.set()
            self.assertTrue(release.wait(_TIMEOUT))

        with self.managed(before=before) as (start, _):
            with self.exchange(token=token) as result:
                try:
                    self.assertTrue(registered.wait(_TIMEOUT))
                    revoked = self.admission.begin_revocation(self.binding, session)
                    self.assertTrue(revoked.revoked)
                    self.assertFalse(revoked.wait_drained(0))
                finally:
                    release.set()
                self.assert_refused(*result)
        start.assert_not_called()
        self.assertTrue(revoked.wait_drained(0))
        self.assert_no_upstream()

    def test_managed_logout_during_spawn_reaps_without_sending_request_bytes(self):
        token = self.issue()
        session = self.session(token)
        started, return_child = Event(), Event()

        def launched():
            started.set()
            self.assertTrue(return_child.wait(_TIMEOUT))

        def observe():
            peer, _ = self.native.accept()
            with peer:
                peer.settimeout(_TIMEOUT)
                return peer.recv(1)

        upstream = self.upstream.submit(observe)
        with self.managed(launched=launched) as (start, children):
            with self.exchange(token=token) as result:
                try:
                    self.assertTrue(started.wait(_TIMEOUT))
                    revoked = self.admission.begin_revocation(self.binding, session)
                    self.assertTrue(revoked.revoked)
                    self.assertFalse(revoked.wait_drained(0))
                    self.assertEqual(upstream.result(_TIMEOUT), b"")
                finally:
                    return_child.set()
                self.assert_refused(*result)
        start.assert_called_once()
        self.assert_reaped(children[0])
        self.assertTrue(revoked.wait_drained(0))
        self.assertIsNotNone(self.issue())

    def test_managed_deadline_reaps_cooperative_proxy_without_fencing_others(self):
        requested = Event()
        token = self.issue()
        with (
            patch.object(web_named_transport, "_REQUEST_TIMEOUT", 1.0),
            self.managed() as (start, children),
        ):
            upstream = self.native_response(None, ready=requested)
            with self.exchange(token=token) as result:
                self.assertTrue(requested.wait(_TIMEOUT))
                self.assert_refused(*result)
            upstream.result(_TIMEOUT)  # Upstream got EOF from real proxy.
        start.assert_called_once()
        self.assert_reaped(children[0])
        self.assertIsNotNone(self.issue())

    def test_managed_nonzero_exit_refuses_a_valid_reply_and_fences_drain(self):
        worker = f"try:\n    exec({workspace_service.PROXY!r})\nexcept SystemExit:\n    raise SystemExit(7)"
        with self.managed(worker=worker) as (start, children):
            upstream = self.native_response(_reply(self.info()))
            with self.exchange(token=self.issue()) as result:
                self.assert_refused(*result)
            upstream.result(_TIMEOUT)
        start.assert_called_once()
        self.assert_reaped(children[0], status=7)
        self.assertIsNone(self.issue())
        self.assertFalse(self.relay.wait_drained(0))

    def test_managed_spawn_failure_without_handle_cannot_confirm_cleanup(self):
        with (
            tempfile.TemporaryDirectory() as root,
            self.managed(executable=Path(root) / "absent") as (start, children),
        ):
            with self.exchange(token=self.issue()) as result:
                self.assert_refused(*result)
        start.assert_called_once()
        self.assertEqual(children, [])
        self.assertIsNone(self.issue())
        self.assertFalse(self.relay.wait_drained(0))
        self.assert_no_upstream()

    def test_managed_forced_host_reap_is_not_confirmed_workspace_drain(self):
        token, independent = self.issue(), self.issue()
        session = self.session(token)
        started = Event()
        worker = "import time; time.sleep(10)"
        with self.managed(worker=worker, launched=started.set) as (start, children):
            with self.exchange(token=token) as result:
                self.assertTrue(started.wait(_TIMEOUT))
                revoked = self.admission.begin_revocation(self.binding, session)
                self.assertTrue(revoked.revoked)
                self.assertFalse(revoked.wait_drained(0))
                self.assert_refused(*result)
            self.assertFalse(revoked.wait_drained(0))
            self.assertFalse(self.relay.wait_drained(0))
            with self.exchange(token=independent) as result:
                self.assert_refused(*result)
        start.assert_called_once()
        self.assert_reaped(children[0], status=-9)
        self.assertIsNone(self.issue())

    def test_managed_supplied_endpoint_must_equal_the_verified_endpoint(self):
        def mismatch(endpoint, *, track, check, deadline):
            track.start_workspace_relay(
                box, container_id="d" * 64, endpoint=endpoint + "/"
            )

        with self.managed() as (start, _):
            self.dial.side_effect = mismatch
            with self.exchange(token=self.issue()) as result:
                self.assert_refused(*result)
        start.assert_not_called()
        self.assert_no_upstream()
