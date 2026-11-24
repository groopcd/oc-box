"""Lease consumer contract; synthetic owner observation, no Podman or listener."""

import os
import socket
import unittest
from dataclasses import replace
from time import monotonic
from unittest.mock import Mock, patch

import test_web_pake_flow as pake

from oc_box import (
    oc_box as box,
    web_app_ingress as ingress,
    web_bootstrap,
    web_local_origins,
    web_named_owner as owner,
    web_named_transport,
    web_native_pair,
)


_PASSWORD = "x" * 43  # Synthetic native control secret, not an account password.
_VALUE = "K" * 10 + "." + "s" * 43
_TIMEOUT = 2
_TARGET = "/api/session/active"
_BODY = b'{"data":{"ses_synthetic":{"type":"running"}}}'
_REPLY = (
    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
    + str(len(_BODY)).encode("ascii")
    + b"\r\n\r\n"
    + _BODY
)


class NamedOwnerTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"})
        environment.start()
        self.addCleanup(environment.stop)
        self.origins = web_local_origins.LocalOrigins()
        self.origin = self.origins.workspace.value
        self.identity = owner.OwnerIdentity(
            "1" * 32, "2" * 32, "3" * 64, "2.3.2", "http://127.0.0.1:51303"
        )
        self.observe = Mock(return_value=self.identity)
        self.control, self.controller = self.socketpair()
        self.connector = Mock()
        self.lease = owner.VerifiedOwnerLease(
            self.identity,
            self.control,
            "/synthetic/project",
            "/synthetic/home",
            _PASSWORD,
            self.observe,
            self.connector,
        )
        self.assertNotIn(_PASSWORD, repr(self.lease))

    def socketpair(self):
        peers = socket.socketpair()
        for peer in peers:
            peer.settimeout(_TIMEOUT)
            self.addCleanup(peer.close)
        return peers

    def bind(self, lease=None):
        bound = owner.NamedOwnerBinding(
            box, origins=self.origins, lease=self.lease if lease is None else lease
        )
        self.addCleanup(bound.close)
        return bound

    def compose(self, bound):
        binding = bound.binding
        record = pake.accounts.Account("in-scope", (binding.workspace_id,), pake.REAL)
        flow = pake.pake.PakeFlow(
            binding,
            store=pake.accounts.AccountStore(pake.SETUP, (record,)),
            backend=pake.SyntheticBackend(),
            sizes=pake.SIZES,
            auth_origin=self.origins.account.value,
        )
        self.addCleanup(flow.close)
        bootstrap = web_bootstrap.BrowserBootstrap(
            origins=self.origins, binding=binding
        )
        self.addCleanup(bootstrap.close)
        app = bound.compose(
            flow=flow, claim_bootstrap=lambda nonce: bootstrap.claim_app(binding, nonce)
        )
        grant = bootstrap.issue(binding)
        self.assertTrue(
            bootstrap.accept(
                binding, ticket=grant.value, auth_origin=self.origins.account.value
            )
        )
        challenge = app.sign_in.start(
            browser_nonce=grant.app_nonce, username="in-scope", ke1=pake.KE1
        )
        ticket = app.sign_in.finish(
            browser_nonce=grant.app_nonce,
            challenge_id=challenge.challenge_id,
            ke3=pake.PROOF,
        )
        self.assertIsNotNone(ticket)
        return app, grant.app_nonce, ticket.value

    def confirm(self, app, nonce, ticket):
        body = ticket.encode("ascii")
        return app.ingress.confirm(
            method="POST",
            target=ingress.CONFIRM_PATH,
            actual_tls=True,
            headers=(
                ("Host", self.origin.removeprefix("https://")),
                ("Origin", self.origin),
                ("Cookie", "__Host-ocbox-bootstrap=" + nonce),
                ("Content-Length", str(len(body))),
            ),
            body=body,
        )

    def request(self, token):
        return web_named_transport.NamedRequest(
            "workspace",
            "GET",
            _TARGET,
            (
                ("Host", self.origin.removeprefix("https://")),
                ("Origin", self.origin),
                ("Cookie", f"{ingress.APP_COOKIE_NAME}={token}"),
            ),
            b"",
        )

    def relay_peers(self, *, reply=True, on_connect=lambda: None):
        upstream, native_peer = self.socketpair()
        stream, browser_peer = self.socketpair()
        if reply:
            native_peer.sendall(_REPLY)
            native_peer.shutdown(socket.SHUT_WR)

        def connect(identity, *, track, check, deadline):
            self.assertIs(identity, self.identity)
            self.assertGreater(deadline, monotonic())
            check()
            track(upstream)
            on_connect()

        self.connector.side_effect = connect
        return stream, browser_peer, native_peer

    def cookie(self, bound, session):
        return session.native_cookie.upstream_cookie(
            binding=bound.binding,
            endpoint=self.identity.endpoint,
            origin=self.origins.workspace,
        )

    def test_confirm_and_metadata_use_same_pins_and_upstream_only_native_cookie(self):
        bound = self.bind()
        with (
            patch.object(web_native_pair, "_read_bridge", return_value=_VALUE) as mint,
            patch.dict(os.environ, {"SYNTHETIC_VAULT_TOKEN": "not-forwarded"}),
        ):
            app, nonce, ticket = self.compose(bound)
            with self.assertRaises(owner.OwnerLeaseError):
                bound.compose(flow=None, claim_bootstrap=Mock())
            mint.assert_not_called()
            token = self.confirm(app, nonce, ticket)
            with self.assertRaises(ingress.AppRequestError):
                self.confirm(app, nonce, ticket)
            mint.assert_called_once()
            command = mint.call_args.args[0]
            i = command.index(self.identity.container_id)
            self.assertEqual(
                command[:i],
                [
                    "podman",
                    "exec",
                    "-i",
                    "--workdir",
                    "/synthetic/project",
                    "--env",
                    "OPENCODE_PASSWORD",
                ],
            )
            self.assertEqual(
                command[-2:],
                [
                    self.identity.endpoint,
                    self.origin.removeprefix("https://"),
                ],
            )
            self.assertNotIn(_PASSWORD, repr(command))
            self.assertEqual(command.environment["OPENCODE_PASSWORD"], _PASSWORD)
            self.assertNotIn("SYNTHETIC_VAULT_TOKEN", command.environment)

        stream, browser_peer, native_peer = self.relay_peers()
        app.relay._respond(stream, self.request(token), monotonic() + _TIMEOUT)
        self.connector.assert_called_once()
        with native_peer.makefile("rb") as source:
            wire = source.read(8192)
        self.assertIn(b"GET /api/session/active HTTP/1.1\r\n", wire)
        self.assertIn(("Cookie: opencode_session_9444=" + _VALUE).encode(), wire)
        self.assertNotIn(token.encode(), wire)
        with browser_peer.makefile("rb") as source:
            response = source.read(8192)
        self.assertTrue(response.endswith(_BODY))
        self.assertNotIn(_VALUE.encode(), response)
        self.assertNotIn(b"Set-Cookie", response)

    def test_generation_workspace_container_and_endpoint_changes_fence_permanently(
        self,
    ):
        for drift in (
            {"workspace_id": "4" * 32},
            {"generation": "4" * 32},
            {"container_id": "4" * 64},
            {"endpoint": "http://127.0.0.1:51304"},
        ):
            with self.subTest(drift=tuple(drift)):
                self.observe.return_value = self.identity
                control, _ = self.socketpair()
                bound = self.bind(replace(self.lease, control=control))
                app, nonce, ticket = self.compose(bound)
                with patch.object(web_native_pair, "_read_bridge", return_value=_VALUE):
                    token = self.confirm(app, nonce, ticket)
                session = app.sign_in.admission.lookup(bound.binding, token)
                self.assertIn(_VALUE, self.cookie(bound, session))
                self.observe.return_value = replace(self.identity, **drift)
                with self.assertRaises(web_native_pair.NativePairError):
                    self.cookie(bound, session)
                self.observe.return_value = self.identity
                with self.assertRaises(web_native_pair.NativePairError):
                    self.cookie(bound, session)
                with self.assertRaises(ingress.StaleAppStateError):
                    app.ingress.expire()
                self.assertIsNone(app.sign_in.admission.lookup(bound.binding, token))
        self.connector.assert_not_called()

    def test_eof_during_pairing_prevents_publication_even_if_observation_agrees(self):
        bound = self.bind()
        app, nonce, ticket = self.compose(bound)

        def mint(_command):
            self.controller.close()
            return _VALUE

        with patch.object(web_native_pair, "_read_bridge", side_effect=mint) as attempt:
            with self.assertRaises(ingress.StaleAppStateError):
                self.confirm(app, nonce, ticket)
            with self.assertRaises(ingress.StaleAppStateError):
                self.confirm(app, nonce, ticket)
            attempt.assert_called_once()
        self.connector.assert_not_called()

    def test_owner_loss_during_connect_closes_tracked_stream_before_writing(self):
        bound = self.bind()
        app, nonce, ticket = self.compose(bound)
        with patch.object(web_native_pair, "_read_bridge", return_value=_VALUE):
            token = self.confirm(app, nonce, ticket)
        stream, browser_peer, native_peer = self.relay_peers(
            reply=False, on_connect=self.controller.close
        )
        with self.assertRaises(ingress.StaleAppStateError):
            app.relay._respond(stream, self.request(token), monotonic() + _TIMEOUT)
        self.assertEqual(browser_peer.recv(8192), b"")
        self.assertEqual(native_peer.recv(8192), b"")
        self.assertIsNone(app.sign_in.admission.lookup(bound.binding, token))

    def test_close_cancels_admission_not_the_callers_attach_socket(self):
        bound = self.bind()
        app, nonce, ticket = self.compose(bound)
        with patch.object(web_native_pair, "_read_bridge", return_value=_VALUE):
            token = self.confirm(app, nonce, ticket)
        session = app.sign_in.admission.lookup(bound.binding, token)
        cancel = Mock()
        with app.sign_in.admission.connection(
            bound.binding, session, cancel=cancel
        ) as use:
            self.assertIsNotNone(use)
            bound.close()
            bound.close()
            self.assertTrue(use.cancelled)
            cancel.assert_called_once()
            with self.assertRaises(web_native_pair.NativePairError):
                self.cookie(bound, session)
            self.assertGreaterEqual(self.control.fileno(), 0)
        self.assertIsNone(app.sign_in.admission.lookup(bound.binding, token))
        self.connector.assert_not_called()

    def test_unverified_handles_and_unaudited_versions_refuse(self):
        for changes in (
            {"version": "2.3.3"},
            {"version": "2.3.2-pre.0"},
            {"container_id": "mutable-name"},
            {"endpoint": "http://127.0.0.2:50100"},
        ):
            with (
                self.subTest(changes=tuple(changes)),
                self.assertRaises(owner.OwnerLeaseError),
            ):
                replace(self.identity, **changes)
        with self.assertRaises(owner.OwnerLeaseError):
            self.bind({"url": self.identity.endpoint, "password": _PASSWORD})
        with patch.dict(os.environ, {"OC_BOX_CONTAINER": "1"}):
            with self.assertRaises(owner.OwnerLeaseError):
                self.bind()
        self.observe.assert_not_called()

        for actual in (True, None, OSError("private diagnostic")):
            self.observe.reset_mock(return_value=True, side_effect=True)
            if isinstance(actual, Exception):
                self.observe.side_effect = actual
            else:
                self.observe.return_value = actual
            with self.assertRaisesRegex(owner.OwnerLeaseError, "could not be verified"):
                self.bind()
        self.connector.assert_not_called()

    def test_unexpected_post_reply_control_bytes_are_never_consumed(self):
        bound = self.bind()
        self.controller.sendall(b"unexpected")
        self.observe.reset_mock()
        with self.assertRaises(owner.OwnerLeaseError):
            bound.compose(flow=None, claim_bootstrap=Mock())
        self.observe.assert_not_called()
        self.assertEqual(self.control.recv(20), b"unexpected")
        with self.assertRaises(owner.OwnerLeaseError):
            bound.compose(flow=None, claim_bootstrap=Mock())
        self.connector.assert_not_called()


if __name__ == "__main__":
    unittest.main()
