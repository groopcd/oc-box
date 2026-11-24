"""Synthetic named admission and native selection wire only."""

from contextlib import ExitStack
import json
import os
import socket
import ssl
from threading import Thread
from time import monotonic
import unittest
from unittest.mock import Mock, patch

from oc_box import (
    web_accounts,
    web_local_origins,
    web_named_headers,
    web_named_listeners,
    web_named_owner,
    web_named_services,
    web_named_studio_selection as studio,
    web_named_transport,
    web_native_pair,
    web_pake_flow,
    web_pake_profile,
)


_SESSION = "ses_synthetic_workspace"
_NATIVE = "K" * 10 + "." + "s" * 43


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}))
        account, workspace = Mock(), Mock()
        origins = web_local_origins.LocalOrigins()
        for stream, address in (
            (account, origins.account_bind),
            (workspace, origins.workspace_bind),
        ):
            stream.getsockname.return_value = address
            stream.getsockopt.return_value = 0
        tls = web_local_origins.LocalTLS(
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER),
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER),
        )
        profile = web_named_listeners.ReservedProfile(origins, tls, account, workspace)
        control, peer = map(self.stack.enter_context, socket.socketpair())
        self.identity = web_named_owner.OwnerIdentity(
            "a" * 32, "b" * 32, "c" * 64, "2.4.1", "http://127.0.0.1:51303"
        )
        self.origin = origins.workspace
        self.authority = self.origin.value.removeprefix("https://")
        self.connect = Mock(side_effect=AssertionError("unexpected proof request"))
        lease = web_named_owner.VerifiedOwnerLease(
            self.identity,
            control,
            "/synthetic/project",
            "/synthetic/home",
            "P" * 43,
            lambda: self.identity,
            self.connect,
        )
        backend = Mock(sizes=web_pake_flow.MessageSizes(96, 320, 64))
        backend.close.return_value = True
        with patch.object(web_pake_profile, "LocalPakeProfile", return_value=backend):
            services = web_named_services.prepare_services(
                None,
                profile=profile,
                lease=lease,
                store=web_accounts.AccountStore(b"s" * 128, ()),
                account_current=lambda: True,
                verifier_fd=7,
                expected_sha256=b"d" * 32,
            )
        self.addCleanup(services.close, timeout=0)
        self.app = services.application.ingress
        self.selection = studio.NamedStudioSelection(services)
        self.addCleanup(self.selection.registry.close)
        self.binding = self.app._binding
        native = web_native_pair.NativeCookie(
            self.binding,
            self.identity.endpoint,
            self.authority,
            _NATIVE,
            version=self.identity.version,
            owner_current=lambda candidate: candidate == self.binding,
        )
        self.token = self.app._admission.issue(
            self.binding,
            principal="synthetic",
            native_cookie=native,
        )
        self.browser = self.app._session(self.token)
        self.deadline = monotonic() + 8
        self.page = self.selection.registry.bootstrap(
            self.browser, deadline=self.deadline
        )

    def request(self, *, session=_SESSION, sequence=1, header_edits=None, body=None):
        if body is None:
            body = json.dumps(
                {
                    "page": self.page.nonce,
                    "sequence": sequence,
                    "sessionID": session,
                }
            ).encode("ascii")
        fields = {
            "Host": self.authority,
            "Origin": self.origin.value,
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "same-origin",
            "Sec-Fetch-Dest": "empty",
            "Cookie": "__Host-ocbox-app=" + self.token,
        } | (header_edits or {})
        return web_named_transport.NamedRequest(
            "workspace",
            "POST",
            studio.PATH,
            tuple(fields.items()),
            body,
        )

    def select(self, request):
        writer, reader = socket.socketpair()
        with writer, reader:
            reader.settimeout(1)
            self.selection.respond(writer, request, self.deadline, check=lambda: None)
            # The dispatcher may borrow the socket but the listener closes it.
            self.assertGreaterEqual(writer.fileno(), 0)
            writer.shutdown(socket.SHUT_WR)
            with reader.makefile("rb") as stream:
                head, body = stream.read(4096).split(b"\r\n\r\n", 1)
        self.assertIn(b"HTTP/1.1 200", head)
        self.assertNotIn(_NATIVE.encode("ascii"), head + body)
        return json.loads(body)

    def serve_native(self, *, location="/synthetic/project"):
        host, server = map(self.stack.enter_context, socket.socketpair())
        server.settimeout(2)
        seen, errors = [], []
        project = "/synthetic/project"
        route = "/api/session/" + _SESSION
        moved = route + "/message" + studio._MOVED
        replies = {
            "/api/info": {
                "version": self.identity.version,
                "pid": 42,
                "urls": [self.identity.endpoint],
            },
            "/api/location": {
                "directory": project,
                "project": {"id": "p", "directory": project, "canonical": project},
            },
            route: {
                "data": {
                    "id": _SESSION,
                    "location": {"directory": location},
                    "projectID": "p",
                    "subpath": None,
                    "time": {"created": 1, "updated": 2},
                }
            },
            moved: {"data": [], "cursor": {"next": None, "previous": None}},
        }

        def serve():
            try:
                with server.makefile("rb") as reader:
                    while line := reader.readline(4096):
                        method, path, version = line.decode("ascii").strip().split(" ")
                        self.assertEqual((method, version), ("GET", "HTTP/1.1"))
                        fields = {}
                        while (line := reader.readline(4096)) not in (b"", b"\r\n"):
                            name, value = line.decode("ascii").strip().split(": ", 1)
                            fields[name] = value
                        self.assertEqual(fields["Host"], self.authority)
                        self.assertEqual(fields["Origin"], self.origin.value)
                        self.assertTrue(fields["Cookie"].endswith("=" + _NATIVE))
                        self.assertNotIn(self.token, fields["Cookie"])
                        self.assertNotIn("Authorization", fields)
                        seen.append(path)
                        body = json.dumps(replies[path]).encode("ascii")
                        closing = fields["Connection"] == "close"
                        server.sendall(
                            (
                                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                                f"Content-Length: {len(body)}\r\n"
                                f"Connection: {fields['Connection']}\r\n\r\n"
                            ).encode("ascii")
                            + body
                        )
                        if closing:
                            server.shutdown(socket.SHUT_WR)
                            return
            except Exception as error:
                errors.append(error)

        thread = Thread(target=serve)
        thread.start()

        def join():
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

        self.addCleanup(join)

        def connect(owner, *, track, check, deadline):
            self.assertEqual(owner, self.identity)
            check()
            track(host)

        self.connect.side_effect = connect
        return seen

    def test_explicit_selection_verifies_exact_native_scope_and_home_invalidates_it(
        self,
    ):
        seen = self.serve_native()
        view = self.select(self.request())
        self.assertEqual(
            view, {"page": self.page.nonce, "generation": 1, "sessionID": _SESSION}
        )
        approved = self.selection.registry.snapshot(
            self.browser,
            self.page.nonce,
            session_id=_SESSION,
            generation=1,
            deadline=self.deadline,
        )
        self.assertEqual(approved.scope.session_id, _SESSION)
        self.assertEqual(
            seen,
            [
                "/api/info",
                "/api/location",
                "/api/session/" + _SESSION,
                "/api/session/" + _SESSION + "/message" + studio._MOVED,
            ]
            * 2,
        )
        self.assertEqual(
            self.select(self.request(session=None, sequence=2))["sessionID"], None
        )
        with self.assertRaises(ValueError):
            self.selection.registry.observe(approved, self.deadline)
        self.connect.assert_called_once()

    def test_bad_fetch_context_duplicate_body_and_other_browser_refuse_before_native(
        self,
    ):
        cases = (
            self.request(header_edits={"Origin": "https://foreign.test"}),
            self.request(header_edits={"Sec-Fetch-Mode": "navigate"}),
            self.request(body=b'{"page":"x","page":"y","sequence":1,"sessionID":null}'),
            self.request(sequence=True),
            self.request(header_edits={"Cookie": "__Host-ocbox-app=" + "A" * 43}),
        )
        for case in cases:
            with (
                self.subTest(case=case),
                self.assertRaises(web_named_headers.NamedRequestError),
            ):
                self.select(case)
        self.connect.assert_not_called()

    def test_foreign_session_cannot_commit_selection_or_retry_old_sequence(self):
        seen = self.serve_native(location="/synthetic/other")
        with self.assertRaises(web_named_headers.NamedRequestError):
            self.select(self.request())
        self.assertEqual(
            seen, ["/api/info", "/api/location", "/api/session/" + _SESSION]
        )
        with self.assertRaises(web_named_headers.NamedRequestError):
            self.select(self.request(session=None, sequence=1))
        with self.assertRaises(ValueError):
            self.selection.registry.snapshot(
                self.browser,
                self.page.nonce,
                session_id=_SESSION,
                generation=1,
                deadline=self.deadline,
            )
        self.connect.assert_called_once()
