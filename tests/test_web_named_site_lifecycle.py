"""Synthetic loopback TLS, owner control pair, and listener teardown only."""

import errno
from html import unescape
import json
import os
import re
import socket
import ssl
import unittest
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
from unittest.mock import Mock, patch

import test_web_local_origins as certificates
import test_web_named_account_assets as asset_fixture
import test_web_pake_flow as proof
from test_web_native_pair import native_info

from oc_box import (
    web_accounts,
    web_bootstrap,
    web_local_origins as local,
    web_named_account_assets as assets,
    web_named_bootstrap as entry,
    web_named_handoff as handoff,
    web_named_http as http,
    web_named_listeners as reserve,
    web_named_owner,
    web_named_services,
    web_named_site_lifecycle as lifecycle,
    web_named_studio_chat,
    web_named_studio_selection,
    web_named_transport as wire,
    web_native_pair,
    web_pake_flow,
    web_pake_profile,
    web_studio_read,
    web_studio_send,
)


@unittest.skipUnless(
    certificates.OPENSSL, "openssl is required for synthetic TLS files"
)
class NamedSiteLifecycleTests(unittest.TestCase):
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
        # Two disposable ports. Patch only the test's construction of the
        # defaults, never the bound-address check or the production bind.
        with socket.socket() as first, socket.socket() as second:
            for peer in (first, second):
                peer.bind((local.LOOPBACK, 0))
            ports = tuple(peer.getsockname()[1] for peer in (first, second))
        original_init = local.LocalOrigins.__init__
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}))
        self.stack.enter_context(
            patch.object(
                local.LocalOrigins,
                "__init__",
                lambda origins, *args: original_init(origins, *(args or ports)),
            )
        )
        self.stack.enter_context(
            patch.object(reserve, "_host_allowed", return_value=True)
        )
        self.stack.enter_context(patch.object(lifecycle, "_SWEEP_INTERVAL", 0.05))
        self.profile = self.stack.enter_context(reserve.reserve_profile(**self.paths))
        self.site = self.profile.origins
        control, self.owner = socket.socketpair()
        self.stack.enter_context(control)
        self.stack.enter_context(self.owner)
        identity = web_named_owner.OwnerIdentity(
            "a" * 32, "b" * 32, "c" * 64, "2.3.2", "http://127.0.0.1:51303"
        )
        self.connector = Mock(side_effect=AssertionError("unexpected native connect"))
        self.lease = web_named_owner.VerifiedOwnerLease(
            identity,
            control,
            "/synthetic/project",
            "/synthetic/home",
            "T" * 43,
            lambda: identity,
            self.connector,
        )
        self.approved = Mock(return_value=True)
        self.backend = Mock(sizes=web_pake_flow.MessageSizes(96, 320, 64))
        self.backend.close.return_value = True
        self.worker_factory = self.stack.enter_context(
            patch.object(
                web_pake_profile, "LocalPakeProfile", return_value=self.backend
            )
        )

    def prepare(self, **overrides):
        kwargs = dict(
            profile=self.profile,
            lease=self.lease,
            store=web_accounts.AccountStore(b"s" * 128, ()),
            account_current=self.approved,
            verifier_fd=7,
            expected_sha256="a" * 64,
        )
        return lifecycle.prepare_sites(None, **(kwargs | overrides))

    def use_sites(self):
        sites = self.prepare()
        self.addCleanup(sites.close)
        return sites

    def raw(self, role):
        return socket.create_connection(getattr(self.site, f"{role}_bind"), timeout=2)

    @contextmanager
    def connect(self, role, *, sni=None):
        with self.raw(role) as raw:
            hostname = sni if sni is not None else getattr(self.site, role).hostname
            with self.client.wrap_socket(raw, server_hostname=hostname) as peer:
                self.assertEqual(peer.selected_alpn_protocol(), "http/1.1")
                yield peer

    @staticmethod
    def start(sites, *, account=None, workspace=None, seconds=4):
        account = account or Mock()
        workspace = workspace or Mock()
        sites.start(
            account=account, workspace=workspace, expires_at=monotonic() + seconds
        )
        return account, workspace

    def assert_closed(self, peer):
        try:
            self.assertEqual(peer.recv(1), b"")
        except ConnectionResetError:
            pass

    def assert_not_served(self):
        for role in ("account", "workspace"):
            with self.subTest(role=role), self.assertRaises(ConnectionRefusedError):
                with self.raw(role):
                    self.fail("unexpected listener")

    def assert_stopped(self, sites, reason):
        self.assertTrue(sites.wait_stopped(timeout=2))
        self.assertIs(sites.stop_reason, reason)
        self.assertEqual(self.profile.account.fileno(), -1)
        self.assertEqual(self.profile.workspace.fileno(), -1)
        self.assert_not_served()

    def request(
        self,
        role,
        path,
        *,
        method="GET",
        body=b"",
        content_type=None,
        cookie=None,
        from_role=None,
        document=False,
        extra_headers=(),
        fetch_mode=None,
        fetch_dest=None,
        fetch_site=None,
    ):
        origin = getattr(self.site, role).value
        fields = [
            f"{method} {path} HTTP/1.1",
            f"Host: {origin.removeprefix('https://')}",
            f"Sec-Fetch-Site: {fetch_site or ('cross-site' if from_role else 'same-origin')}",
            f"Sec-Fetch-Mode: {fetch_mode or ('navigate' if document else 'same-origin')}",
            f"Sec-Fetch-Dest: {fetch_dest or ('document' if document else 'empty')}",
        ]
        if not document or from_role:
            fields.append(f"Origin: {getattr(self.site, from_role or role).value}")
        if method == "POST":
            fields.append(f"Content-Length: {len(body)}")
        if content_type:
            fields.append(f"Content-Type: {content_type}")
        if cookie:
            fields.append(f"Cookie: {cookie}")
        fields.extend(f"{key}: {value}" for key, value in extra_headers)
        return ("\r\n".join(fields) + "\r\n\r\n").encode("ascii") + body

    def exchange(self, role, path, **kwargs):
        with self.connect(role) as peer:
            peer.sendall(self.request(role, path, **kwargs))
            with peer.makefile("rb") as response:
                reply = response.read(64 * 1024)
        head, _, body = reply.partition(b"\r\n\r\n")
        return head, body

    def cookie(self, head, name):
        prefix = f"Set-Cookie: {name}=".encode("ascii")
        cookies = [
            line.removeprefix(b"Set-Cookie: ").partition(b";")[0].decode("ascii")
            for line in head.split(b"\r\n")
            if line.startswith(prefix)
        ]
        self.assertEqual(len(cookies), 1)
        return cookies[0]

    def connect_fixture_relay(self, relay):
        def connect(identity, *, track, check, deadline):
            self.assertEqual(identity, self.lease.identity)
            check()
            track(relay)

        self.connector.side_effect = connect

    def begin_proof(self, *, studio=False, studio_ui=False):
        backend = proof.SyntheticBackend()
        backend.sizes = proof.SIZES
        backend.close = Mock(return_value=True)
        self.worker_factory.return_value = backend
        identity = self.lease.identity
        if studio:
            identity = replace(identity, version="2.4.1")
        lease = replace(self.lease, identity=identity, observe=lambda: identity)
        sites = self.prepare(
            lease=lease,
            store=web_accounts.AccountStore(
                b"s" * 128,
                (
                    web_accounts.Account(
                        "fixture", (identity.workspace_id,), b"r" * 192
                    ),
                ),
            ),
        )
        self.addCleanup(sites.close)
        document = b"<h1>Fixture sign-in</h1>"
        native_value = "K" * 10 + "." + "s" * 43
        owner = sites.services._owner
        self.mint = self.stack.enter_context(
            patch.object(
                web_native_pair,
                "pair_cookie",
                return_value=web_native_pair.NativeCookie(
                    owner.binding,
                    identity.endpoint,
                    self.site.workspace.value.removeprefix("https://"),
                    native_value,
                    version=identity.version,
                    owner_current=owner._owner_current,
                ),
            )
        )
        self.application = http.start_named_http(
            sites,
            self.profile,
            login_document=document,
            expires_at=monotonic() + 10,
            studio=studio,
            studio_ui=studio_ui,
        )

        head, page = self.exchange("workspace", entry.WORKSPACE_ENTRY, document=True)
        self.assertTrue(head.startswith(b"HTTP/1.1 200"))
        nonce = self.cookie(head, "__Host-ocbox-bootstrap")
        seed = re.search(rb'name="ticket" value="([A-Za-z0-9_-]+)"', page)[1]
        head, page = self.exchange(
            "account",
            entry.ACCOUNT_ENTRY,
            method="POST",
            document=True,
            from_role="workspace",
            content_type="application/x-www-form-urlencoded",
            body=b"ticket=" + seed,
        )
        self.assertTrue(head.startswith(b"HTTP/1.1 200"))
        self.assertEqual(page, document)
        account = self.cookie(head, "__Host-ocbox-account")
        head, body = self.exchange(
            "account",
            "/start",
            method="POST",
            cookie=account,
            content_type="application/json",
            body=json.dumps({"username": "fixture", "ke1": proof.KE1}).encode("ascii"),
        )
        self.assertTrue(head.startswith(b"HTTP/1.1 200"))
        challenge = json.loads(body)["challenge_id"]
        head, body = self.exchange(
            "account",
            "/finish",
            method="POST",
            cookie=account,
            content_type="application/json",
            body=json.dumps({"challenge_id": challenge, "ke3": proof.PROOF}).encode(
                "ascii"
            ),
        )
        self.assertTrue(head.startswith(b"HTTP/1.1 200"))
        ticket = json.loads(body)["ticket"].encode("ascii")
        head, page = self.exchange(
            "workspace",
            handoff.HANDOFF_PATH,
            method="POST",
            document=True,
            from_role="account",
            content_type="application/x-www-form-urlencoded",
            body=b"ticket=" + ticket,
        )
        self.assertTrue(head.startswith(b"HTTP/1.1 200"))
        self.assertIn(b"Confirm workspace sign-in", page)
        self.assertNotIn(b"Set-Cookie:", head)
        self.mint.assert_not_called()
        confirm = dict(
            method="POST", cookie=nonce, content_type="text/plain", body=ticket
        )
        return sites, confirm, native_value.encode("ascii")

    def test_exact_application_dispatch_and_borrowed_relay_under_one_listener(self):
        sites, confirm, native_value = self.begin_proof()
        head, _ = self.exchange("workspace", "/api/info")
        self.assertEqual(head, b"")
        self.assertIsNone(sites.stop_reason)
        self.connector.assert_not_called()
        self.assertEqual(
            self.exchange(
                "workspace", "/oc-box/studio/conversation?session=ses_fixture"
            ),
            (b"", b""),
        )

        head, body = self.exchange("workspace", handoff.CONFIRM_PATH, **confirm)
        self.assertTrue(head.startswith(b"HTTP/1.1 204"))
        self.assertEqual(body, b"")
        self.assertNotIn(b"Content-Length:", head)
        self.assertNotIn(native_value, head)
        self.assertNotIn(confirm["body"], head)
        cookie = self.cookie(head, "__Host-ocbox-app")
        replay, _ = self.exchange("workspace", handoff.CONFIRM_PATH, **confirm)
        self.assertTrue(replay.startswith(b"HTTP/1.1 403"))
        self.assertNotIn(b"Set-Cookie:", replay)
        self.mint.assert_called_once()

        identity = self.lease.identity
        with ExitStack() as peers:
            relay, native = map(peers.enter_context, socket.socketpair())
            native.settimeout(2)
            self.connect_fixture_relay(relay)
            reply = json.dumps(native_info(identity.endpoint)).encode("ascii")
            native.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Set-Cookie: synthetic-native-rotation=private\r\n"
                + f"Content-Length: {len(reply)}\r\n\r\n".encode("ascii")
                + reply
            )
            native.shutdown(socket.SHUT_WR)
            head, body = self.exchange("workspace", "/api/info", cookie=cookie)
            self.assertTrue(head.startswith(b"HTTP/1.1 200"))
            self.assertEqual(
                json.loads(body),
                {"version": identity.version, "pid": 141, "urls": [identity.endpoint]},
            )
            self.assertNotIn(b"Set-Cookie:", head)
            self.assertNotIn(native_value, head + body)
            self.assertNotIn(b"private-native-hostname", body)
            with native.makefile("rb") as incoming:
                upstream = incoming.read(2048)
            self.assertIn(native_value, upstream)
            self.assertNotIn(cookie.encode("ascii"), upstream)
            self.assertEqual(relay.fileno(), -1)
        self.connector.assert_called_once()
        self.assertIsNone(
            sites.stop_reason
        )  # The relay did not close the site worker's TLS.

        head, _ = self.exchange(
            "workspace", "/oc-box/logout", method="POST", cookie=cookie
        )
        self.assertTrue(head.startswith(b"HTTP/1.1 200"))
        self.assertIn(b"Max-Age=0", head)
        self.assertEqual(
            self.exchange("workspace", "/api/info", cookie=cookie), (b"", b"")
        )
        self.connector.assert_called_once()
        self.assertIsNone(sites.stop_reason)
        self.assertTrue(sites.close())
        self.assertGreaterEqual(self.lease.control.fileno(), 0)

    def test_exact_studio_routes_share_the_tls_app_grant_and_retire_its_page(self):
        sites, confirm, _ = self.begin_proof(studio=True)
        head, _ = self.exchange("workspace", handoff.CONFIRM_PATH, **confirm)
        cookie = self.cookie(head, "__Host-ocbox-app")
        ingress = sites.services.application.ingress
        browser = ingress._session(cookie.partition("=")[2])
        registry = self.application._selection.registry
        page = registry.bootstrap(browser, deadline=monotonic() + 2)
        session = "ses_fixture"

        with patch.object(
            web_named_studio_selection, "_native_selection", return_value=session
        ):
            selection_head, selection_body = self.exchange(
                "workspace",
                web_named_studio_selection.PATH,
                method="POST",
                cookie=cookie,
                content_type="application/json",
                body=json.dumps(
                    {"page": page.nonce, "sequence": 1, "sessionID": session}
                ).encode("ascii"),
            )
        self.assertTrue(selection_head.startswith(b"HTTP/1.1 200"))
        selected = json.loads(selection_body)
        self.assertEqual(
            selected, {"page": page.nonce, "generation": 1, "sessionID": session}
        )
        chat_headers = (
            (web_named_studio_chat.PAGE_HEADER, page.nonce),
            (web_named_studio_chat.GENERATION_HEADER, "1"),
        )
        transcript = {
            "sessionID": session,
            "title": "Synthetic",
            "hasEarlier": False,
            "messages": [
                {
                    "id": f"msg_synthetic_{i}",
                    "role": "assistant",
                    "text": "x" * 4000,
                    "truncated": False,
                }
                for i in range(3)
            ],
        }
        data = json.dumps(transcript).encode("ascii")
        self.assertGreater(len(data), wire._RESPONSE_LIMIT)

        def read(_, request, scope, *, observe, deadline, write, retire, **options):
            self.assertIs(scope.browser, browser)
            self.assertEqual(scope.session_id, session)
            self.assertEqual(
                request.target, web_named_studio_chat.READ_PATH + "?session=" + session
            )
            self.assertIs(observe(deadline), scope)
            write(
                web_studio_read.ReadResponse(data),
                deadline,
                check=lambda: observe(deadline),
            )
            self.assertTrue(retire())

        target = web_named_studio_chat.READ_PATH + "?session=" + session
        with patch.object(web_studio_read, "respond", side_effect=read) as reader:
            response, body = self.exchange(
                "workspace", target, cookie=cookie, extra_headers=chat_headers
            )
            self.assertTrue(response.startswith(b"HTTP/1.1 200"))
            self.assertEqual(json.loads(body), transcript)
            self.assertEqual(
                self.exchange(
                    "workspace",
                    target + "&extra=1",
                    cookie=cookie,
                    extra_headers=chat_headers,
                ),
                (b"", b""),
            )
            reader.assert_called_once()

        with patch.object(
            web_studio_send.StudioSend,
            "execute",
            return_value=web_studio_send.SendResult.QUEUED,
        ) as send:
            response, body = self.exchange(
                "workspace",
                web_named_studio_chat.SEND_PATH,
                method="POST",
                cookie=cookie,
                content_type="application/json",
                extra_headers=chat_headers,
                body=json.dumps(
                    {"sessionID": session, "text": "Synthetic send"}
                ).encode("ascii"),
            )
            self.assertTrue(response.startswith(b"HTTP/1.1 200"))
            self.assertEqual(json.loads(body), {"status": "queued"})
            send.assert_called_once()
            self.assertEqual(send.call_args.kwargs["text"], "Synthetic send")
        self.assertTrue(sites.close())
        self.assertTrue(registry._closed)
        self.assertGreaterEqual(self.lease.control.fileno(), 0)

    def test_authenticated_studio_landing_has_no_iframe_and_serves_only_exact_pinned_assets(
        self,
    ):
        sites, confirm, _ = self.begin_proof(studio=True, studio_ui=True)
        self.assertEqual(self.exchange("workspace", "/", document=True), (b"", b""))
        confirmed, _ = self.exchange("workspace", handoff.CONFIRM_PATH, **confirm)
        cookie = self.cookie(confirmed, "__Host-ocbox-app")
        head, body = self.exchange(
            "workspace", "/", document=True, cookie=cookie, fetch_site="none"
        )
        self.assertTrue(head.startswith(b"HTTP/1.1 200"))
        self.assertNotIn(b"Set-Cookie:", head)
        self.assertIn(b"frame-src 'none'", head)
        self.assertIn(b"Recent workspace sessions", body)
        self.assertNotIn(b"<iframe", body)
        self.assertNotIn(cookie.encode("ascii"), body)
        self.assertIn(b'/oc-box/studio/web_named_studio_entry.mjs"', body)
        match = re.search(rb'data-studio-page="([^"]+)"', body)
        state = json.loads(unescape(match[1].decode("ascii")))
        self.assertEqual(state["generation"], 0)
        self.assertIsNone(state["sessionID"])
        asset = "/oc-box/studio/web_named_studio_entry.mjs"
        head, script = self.exchange(
            "workspace", asset, cookie=cookie, fetch_mode="cors", fetch_dest="script"
        )
        self.assertTrue(head.startswith(b"HTTP/1.1 200"))
        self.assertIn(b"Content-Type: text/javascript; charset=utf-8", head)
        self.assertIn(b"createNamedStudioController", script)
        for path, mode in (
            (asset, "no-cors"),
            (asset + "?reload=1", "cors"),
            ("/api/provider", "cors"),
        ):
            with self.subTest(path=path, mode=mode):
                self.assertEqual(
                    self.exchange(
                        "workspace",
                        path,
                        cookie=cookie,
                        fetch_mode=mode,
                        fetch_dest="script",
                    ),
                    (b"", b""),
                )
        self.assertTrue(sites.close())
        self.assertGreaterEqual(self.lease.control.fileno(), 0)

    def test_shutdown_interrupts_upstream_and_retains_owner_until_drain(self):
        sites, confirm, _ = self.begin_proof()
        head, _ = self.exchange("workspace", handoff.CONFIRM_PATH, **confirm)
        cookie = self.cookie(head, "__Host-ocbox-app")
        with ExitStack() as peers:
            relay, native = map(peers.enter_context, socket.socketpair())
            native.settimeout(2)
            self.connect_fixture_relay(relay)
            peer = peers.enter_context(self.connect("workspace"))
            peer.sendall(self.request("workspace", "/api/info", cookie=cookie))
            with native.makefile("rb") as incoming:
                self.assertEqual(incoming.readline(), b"GET /api/info HTTP/1.1\r\n")
                self.assertTrue(sites.close(timeout=1))
                self.assertEqual(incoming.read(2048)[-4:], b"\r\n\r\n")
            self.assertEqual(relay.fileno(), -1)
            self.assert_closed(peer)
        self.assert_stopped(sites, lifecycle.StopReason.REQUESTED)
        self.assertGreaterEqual(self.lease.control.fileno(), 0)

    def test_failed_cookie_delivery_revokes_the_exact_browser_grant(self):
        sites, confirm, _ = self.begin_proof()
        tokens = []

        def disconnected(stream, token, deadline, *, check):
            check()
            tokens.append(token)
            raise BrokenPipeError()

        with patch.object(wire, "_write_app_grant", side_effect=disconnected):
            self.assertEqual(
                self.exchange("workspace", handoff.CONFIRM_PATH, **confirm), (b"", b"")
            )
        self.assertEqual(len(tokens), 1)
        admission = sites.services.application.sign_in.admission
        binding = sites.services._owner.binding
        self.assertIsNone(admission.lookup(binding, tokens[0]))
        self.assertEqual(
            self.exchange(
                "workspace", "/api/info", cookie="__Host-ocbox-app=" + tokens[0]
            ),
            (b"", b""),
        )
        self.assertIsNone(sites.stop_reason)
        self.connector.assert_not_called()
        self.assertTrue(sites.close())

    def test_exact_pinned_assets_use_only_the_live_account_bootstrap(self):
        pin = asset_fixture.pin(asset_fixture.VERIFIER)
        sites = self.prepare(expected_sha256=pin.sha256)
        self.addCleanup(sites.close)
        asset = "/account-assets/client.wasm"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            wasm = root / "approved.wasm"
            verifier = root / "approved-verifier"
            wasm.write_bytes(asset_fixture.WASM)
            verifier.write_bytes(asset_fixture.VERIFIER)
            with assets.prepare_account_assets(
                origins=self.site,
                wasm_file=wasm,
                verifier_file=verifier,
                wasm_pin=asset_fixture.pin(asset_fixture.WASM),
                verifier_pin=pin,
            ) as prepared:
                options = {"expires_at": monotonic() + 10}
                with self.assertRaisesRegex(ValueError, "Invalid named HTTPS"):
                    http.start_named_http(
                        sites,
                        self.profile,
                        account_assets=replace(prepared, verifier_sha256=b"x" * 32),
                        **options,
                    )
                self.assert_not_served()
                http.start_named_http(
                    sites, self.profile, account_assets=prepared, **options
                )

        self.assertEqual(self.exchange("account", asset), (b"", b""))
        _, page = self.exchange("workspace", entry.WORKSPACE_ENTRY, document=True)
        seed = re.search(rb'name="ticket" value="([A-Za-z0-9_-]+)"', page)[1]
        head, page = self.exchange(
            "account",
            entry.ACCOUNT_ENTRY,
            method="POST",
            document=True,
            from_role="workspace",
            content_type="application/x-www-form-urlencoded",
            body=b"ticket=" + seed,
        )
        self.assertTrue(head.startswith(b"HTTP/1.1 200"))
        self.assertIn(b"X-Frame-Options: DENY", head)
        self.assertIn(b"/account-assets/sign-in.mjs", page)
        cookie = self.cookie(head, "__Host-ocbox-account")
        for role, path, context in (
            ("workspace", asset, {}),
            ("account", asset + "?file=other.wasm", {}),
            ("account", asset, {"document": True}),
        ):
            with self.subTest(role=role, path=path, context=context):
                self.assertEqual(
                    self.exchange(role, path, cookie=cookie, **context), (b"", b"")
                )

        head, content = self.exchange("account", asset, cookie=cookie)
        self.assertTrue(head.startswith(b"HTTP/1.1 200"))
        self.assertIn(b"Content-Type: application/wasm", head)
        self.assertIn(b"Cross-Origin-Resource-Policy: same-origin", head)
        self.assertNotIn(b"Set-Cookie:", head)
        self.assertEqual(content, asset_fixture.WASM)
        self.assertNotIn(asset_fixture.VERIFIER, head + content)
        write = wire._write_wire

        def retired_before_write(stream, data, deadline, *, check=None):
            sites.services._bootstrap.close()
            write(stream, data, deadline, check=check)

        with patch.object(wire, "_write_wire", side_effect=retired_before_write):
            self.assertEqual(self.exchange("account", asset, cookie=cookie), (b"", b""))
        self.assertIsNone(sites.stop_reason)
        self.connector.assert_not_called()
        self.assertTrue(sites.close())

    def test_exact_preflight_sockets_and_tls_dispatch_both_fixed_roles(self):
        sites = self.use_sites()
        self.assert_not_served()
        self.assertIs(type(sites.services), web_named_services.NamedServices)

        def responder(role):
            origin = getattr(self.site, role)

            def dispatch(peer, *, check, deadline):
                self.assertIs(type(peer), ssl.SSLSocket)
                self.assertEqual(peer.getsockname(), getattr(self.site, f"{role}_bind"))
                request = wire._read(peer, role=role, origin=origin, deadline=deadline)
                self.assertEqual(
                    request.target, "/start" if role == "account" else "/api/info"
                )
                wire._write_response(
                    peer,
                    wire.NamedResponse(200, f'"{role}"'.encode()),
                    deadline,
                    check=check,
                )

            return Mock(side_effect=dispatch)

        with patch.object(
            local, "check_tls", side_effect=AssertionError("TLS refetch")
        ):
            account, workspace = self.start(
                sites, account=responder("account"), workspace=responder("workspace")
            )
            for role, line, fields in (
                (
                    "account",
                    "POST /start",
                    "Content-Type: application/json\r\nContent-Length: 2\r\n",
                ),
                ("workspace", "GET /api/info", ""),
            ):
                with self.subTest(role=role), self.connect(role) as peer:
                    origin = getattr(self.site, role).value
                    body = "{}" if role == "account" else ""
                    peer.sendall(
                        (
                            f"{line} HTTP/1.1\r\nHost: {origin.removeprefix('https://')}\r\n"
                            f"Origin: {origin}\r\n{fields}\r\n{body}"
                        ).encode()
                    )
                    result = bytearray()
                    while len(result) < 2048 and (chunk := peer.recv(2048)):
                        result.extend(chunk)
                    self.assertTrue(result.startswith(b"HTTP/1.1 200 OK\r\n"))
                    self.assertTrue(result.endswith(f'\r\n\r\n"{role}"'.encode()))
            for role, wrong in (
                ("account", local.WORKSPACE_HOST),
                ("workspace", local.ACCOUNT_HOST),
            ):
                with self.subTest(role=role), self.assertRaises(ssl.SSLError):
                    with self.connect(role, sni=wrong):
                        self.fail("cross-site SNI accepted")
        account.assert_called_once()
        workspace.assert_called_once()
        self.connector.assert_not_called()
        self.assertTrue(sites.close())
        self.assert_stopped(sites, lifecycle.StopReason.REQUESTED)
        self.assertGreaterEqual(self.lease.control.fileno(), 0)

    def test_activation_rechecks_owner_and_refuses_substituted_reservation(self):
        with self.assertRaises(lifecycle.ListenerError):
            self.prepare(profile=replace(self.profile, account=self.profile.workspace))
        self.worker_factory.assert_not_called()
        with self.assertRaises(web_named_services.ServicePreparationError):
            self.prepare(lease=None)
        self.worker_factory.assert_not_called()
        sites = self.use_sites()
        self.approved.return_value = False
        with self.assertRaises(lifecycle.ListenerError):
            self.start(sites)
        self.assertIs(sites.stop_reason, lifecycle.StopReason.OWNER_OR_ACCOUNT)
        self.assert_not_served()
        self.assertTrue(sites.close())
        # No listen operation borrowed these objects before the owner refusal.
        self.assertGreaterEqual(self.profile.account.fileno(), 0)
        with self.assertRaises(lifecycle.ListenerError):
            self.start(sites)

    def test_idle_account_refusal_fences_both_sockets_and_retains_hold(self):
        sites = self.use_sites()
        self.start(sites)
        self.approved.side_effect = RuntimeError("synthetic private store diagnostic")
        self.assert_stopped(sites, lifecycle.StopReason.OWNER_OR_ACCOUNT)
        self.backend.close.return_value = False
        self.assertFalse(sites.close())
        self.assertGreaterEqual(self.lease.control.fileno(), 0)
        self.backend.close.return_value = True
        self.assertTrue(sites.close())
        self.assertGreaterEqual(self.lease.control.fileno(), 0)

    def test_idle_owner_socket_loss_fences_both_sockets(self):
        sites = self.use_sites()
        self.start(sites)
        self.owner.close()
        self.assert_stopped(sites, lifecycle.StopReason.OWNER_OR_ACCOUNT)
        self.assertTrue(sites.close())

    def test_worker_liveness_check_fences_before_reply_without_waiting_for_sweep(self):
        sites = self.use_sites()
        entered, release = Event(), Event()
        self.addCleanup(release.set)

        def workspace(peer, *, check, deadline):
            entered.set()
            release.wait(2)
            wire._write_response(
                peer, wire.NamedResponse(200, b"{}"), deadline, check=check
            )

        with patch.object(lifecycle, "_SWEEP_INTERVAL", 12):
            self.start(sites, workspace=workspace)
            with self.connect("workspace") as peer:
                self.assertTrue(entered.wait(1))
                self.approved.return_value = False
                release.set()
                self.assert_closed(peer)
            self.assert_stopped(sites, lifecycle.StopReason.OWNER_OR_ACCOUNT)
        self.assertTrue(sites.close())

    def test_lifetime_expiry_cancels_stalled_tls_on_both_sites(self):
        sites = self.use_sites()
        account, workspace = self.start(sites, seconds=0.4)
        with self.raw("account") as a, self.raw("workspace") as b:
            self.assert_stopped(sites, lifecycle.StopReason.EXPIRED)
            for peer in (a, b):
                self.assert_closed(peer)
        account.assert_not_called()
        workspace.assert_not_called()
        self.assertTrue(sites.close(timeout=1))

    def test_connection_deadline_releases_stall_without_exhausting_listener(self):
        sites = self.use_sites()
        workspace_complete = Event()
        with patch.object(lifecycle, "_CONNECTION_SECONDS", 0.35):
            self.start(sites, workspace=lambda *a, **k: workspace_complete.set())
            with self.raw("account") as peer:
                self.assert_closed(peer)
            self.assertIsNone(sites.stop_reason)
            with self.connect("workspace"):
                self.assertTrue(workspace_complete.wait(1))
        self.assertTrue(sites.close())

    def test_worker_limit_counts_both_sites_and_fences_on_first_overflow(self):
        sites = self.use_sites()
        with patch.object(lifecycle, "_WORKER_LIMIT", 2), ExitStack() as peers:
            account, workspace = self.start(sites)
            clients = [
                peers.enter_context(self.raw(role))
                for role in ("account", "workspace", "account")
            ]
            self.assert_stopped(sites, lifecycle.StopReason.WORKER_LIMIT)
            for client in clients:
                self.assert_closed(client)
            account.assert_not_called()
            workspace.assert_not_called()
            self.assertTrue(sites.close())

    def test_post_tls_shutdown_wakes_only_owned_peer_and_requires_worker_exit(self):
        sites = self.use_sites()
        reading, cancelled, release = Event(), Event(), Event()
        self.addCleanup(release.set)

        def workspace(peer, *, check, deadline):
            check()
            peer.settimeout(wire._remaining(deadline))
            reading.set()
            try:
                peer.recv(1)
            except OSError:
                pass
            finally:
                cancelled.set()
            release.wait(2)

        self.start(sites, workspace=workspace)
        with self.connect("workspace") as peer:
            self.assertTrue(reading.wait(1))
            self.assertFalse(sites.close(timeout=0.05))
            self.assert_stopped(sites, lifecycle.StopReason.REQUESTED)
            self.assertTrue(cancelled.wait(1))
            self.assert_closed(peer)
            self.backend.close.assert_not_called()
            # The borrowed control socket and its peer were not shutdown.
            self.owner.sendall(b"x")
            self.assertEqual(self.lease.control.recv(1), b"x")
            release.set()
            self.assertTrue(sites.close())

    def test_uncertain_socket_identity_never_closes_unconfirmed_descriptor(self):
        sites = self.use_sites()
        self.start(sites)
        identity = lifecycle._has_identity
        with patch.object(
            lifecycle,
            "_has_identity",
            side_effect=lambda peer, expected: (
                peer is not self.profile.workspace and identity(peer, expected)
            ),
        ):
            self.assertFalse(sites.close())
        self.assertEqual(self.profile.account.fileno(), -1)
        self.assertGreaterEqual(self.profile.workspace.fileno(), 0)
        self.assertGreaterEqual(self.lease.control.fileno(), 0)
        self.assertFalse(sites.close())

    def test_second_listener_failure_rolls_back_first_without_dispatch(self):
        sites = self.use_sites()
        listen = socket.socket.listen

        def fail_second(peer, backlog):
            if peer is self.profile.workspace:
                raise OSError(errno.EINVAL, "synthetic private listen diagnostic")
            return listen(peer, backlog)

        with (
            patch.object(socket.socket, "listen", fail_second),
            self.assertRaisesRegex(lifecycle.ListenerError, "could not be activated"),
        ):
            self.start(sites)
        self.assert_stopped(sites, lifecycle.StopReason.FAILED)
        self.assertTrue(sites.close())

    def test_idle_expiry_sweep_error_fences_listeners(self):
        sites = self.use_sites()
        self.start(sites)
        with patch.object(
            web_bootstrap.BrowserBootstrap,
            "expire",
            side_effect=RuntimeError("synthetic private expiry diagnostic"),
        ):
            self.assert_stopped(sites, lifecycle.StopReason.FAILED)
        self.assertTrue(sites.close())
