"""Synthetic loopback TLS, owner control pair, and listener teardown only."""

import errno
import os
import socket
import ssl
import unittest
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from threading import Event
from time import monotonic
from unittest.mock import Mock, patch

import test_web_local_origins as certificates

from oc_box import (
    web_accounts,
    web_bootstrap,
    web_local_origins as local,
    web_named_listeners as reserve,
    web_named_owner,
    web_named_services,
    web_named_site_lifecycle as lifecycle,
    web_named_transport as wire,
    web_pake_flow,
    web_pake_profile,
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
                lambda origins: original_init(origins, *ports),
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
