"""Synthetic named app + Studio adapters. No listening sockets or inference."""

from contextlib import contextmanager
from dataclasses import replace
import json
import os
import socket
import sys
from threading import Thread
from time import monotonic
import unittest
from unittest.mock import Mock, patch

import test_web_pake_flow as pake
import test_web_studio_send as native

from oc_box import (
    oc_box as box,
    web_admission,
    web_app_ingress,
    web_bootstrap,
    web_local_origins,
    web_named_headers,
    web_named_owner,
    web_named_studio_chat as chat,
    web_named_transport,
    web_native_pair,
    web_studio_conversation,
    web_studio_selection,
    web_studio_send,
)


_COOKIE = "K" * 10 + "." + "s" * 43  # Synthetic upstream value, never used by chat.
_ID = native.ID
_TEXT = native.TEXT
_PASSWORD = "synthetic_unused_native_control_" + "q" * 32


class NamedStudioChatTests(unittest.TestCase):
    def setUp(self):
        on_host = patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"})
        on_host.start()
        self.addCleanup(on_host.stop)
        origins = web_local_origins.LocalOrigins()
        self.origin = origins.workspace.value
        self.authority = self.origin.removeprefix("https://")
        self.identity = web_named_owner.OwnerIdentity(
            "a" * 32, "b" * 32, "c" * 64, "2.4.1", native.ORIGIN
        )
        self.observe_owner = Mock(return_value=self.identity)
        control, self.controller = socket.socketpair()
        self.addCleanup(control.close)
        self.addCleanup(self.controller.close)
        self.connector = Mock(side_effect=AssertionError("not a native cookie relay"))
        self.lease = web_named_owner.VerifiedOwnerLease(
            self.identity,
            control,
            native.PROJECT,
            "/synthetic/home",
            _PASSWORD,
            self.observe_owner,
            self.connector,
        )
        self.owner = web_named_owner.NamedOwnerBinding(
            box, origins=origins, lease=self.lease
        )
        self.addCleanup(self.owner.close)
        self.binding = self.owner.binding
        flow = pake.pake.PakeFlow(
            self.binding,
            store=pake.accounts.AccountStore(
                pake.SETUP,
                (
                    pake.accounts.Account(
                        "in-scope", (self.identity.workspace_id,), pake.REAL
                    ),
                ),
            ),
            backend=pake.SyntheticBackend(),
            sizes=pake.SIZES,
            auth_origin=origins.account.value,
        )
        bootstrap = web_bootstrap.BrowserBootstrap(
            origins=origins, binding=self.binding
        )
        self.addCleanup(bootstrap.close)
        app = self.owner.compose(
            flow=flow,
            claim_bootstrap=lambda nonce: bootstrap.claim_app(self.binding, nonce),
        )
        issued = bootstrap.issue(self.binding)
        self.assertTrue(
            bootstrap.accept(
                self.binding, ticket=issued.value, auth_origin=origins.account.value
            )
        )
        challenge = app.sign_in.start(
            browser_nonce=issued.app_nonce, username="in-scope", ke1=pake.KE1
        )
        ticket = app.sign_in.finish(
            browser_nonce=issued.app_nonce,
            challenge_id=challenge.challenge_id,
            ke3=pake.PROOF,
        ).value.encode("ascii")
        with patch.object(web_native_pair, "_read_bridge", return_value=_COOKIE):
            self.token = app.ingress.confirm(
                method="POST",
                target=web_app_ingress.CONFIRM_PATH,
                headers=(
                    ("Host", self.authority),
                    ("Origin", self.origin),
                    ("Cookie", "__Host-ocbox-bootstrap=" + issued.app_nonce),
                    ("Content-Length", str(len(ticket))),
                ),
                actual_tls=True,
                body=ticket,
            )
        self.admission = app.sign_in.admission
        self.browser = self.admission.lookup(self.binding, self.token)
        self.selections = self.registry(self.identity, self.admission)
        self.addCleanup(self.selections.close)
        self.page = self.selections.bootstrap(self.browser, deadline=monotonic() + 2)
        self.selected = self.select(1)
        self.chat = chat.NamedStudioChat(self.owner, self.selections)
        self.check = Mock()
        self.write = Mock(side_effect=self.assert_checked_write)
        self.interrupt = Mock()
        self.retire = Mock(side_effect=self.confirm_retire)

    def registry(self, identity, admission):
        return web_studio_selection.SelectionRegistry(
            identity,
            admission,
            self.binding,
            observe_owner=lambda _: self.observe_owner(),
        )

    def select(self, sequence, ident=_ID):
        return self.selections.change(
            self.browser,
            self.page.nonce,
            sequence=sequence,
            session_id=ident,
            deadline=monotonic() + 2,
            verify=lambda _owner, _browser, exact, _deadline: exact,
        )

    def assert_registered(self):
        with self.admission._lock:
            self.assertEqual(len(self.admission._uses), 1)

    def assert_checked_write(self, reply, deadline, *, check):
        check()
        self.assert_registered()
        self.assertIs(type(reply), chat.ChatReply)
        self.assertGreater(deadline, monotonic())
        self.assertLessEqual(deadline, self.browser.expires_at)
        check()

    def confirm_retire(self):
        self.assert_registered()
        return True

    def request(self, method="GET", *, origin=None, fields=None, body=None, view=None):
        if view is None:
            view = self.selected
        headers = {
            "Host": self.authority,
            "Cookie": f"{web_app_ingress.APP_COOKIE_NAME}={self.token}",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            chat.PAGE_HEADER: view.nonce,
            chat.GENERATION_HEADER: str(view.generation),
        }
        if method == "POST":
            target = chat.SEND_PATH
            if body is None:
                body = json.dumps({"sessionID": _ID, "text": _TEXT}).encode()
            headers.update(
                {"Content-Type": "application/json", "Content-Length": str(len(body))}
            )
            headers["Origin"] = self.origin
        else:
            target = chat.READ_PATH + "?session=" + _ID
            body = b"" if body is None else body
        if origin is not None:
            headers["Origin"] = origin
        headers.update(fields or {})
        return web_named_transport.NamedRequest(
            "workspace",
            method,
            target,
            tuple(
                (name, value) for name, value in headers.items() if value is not None
            ),
            body,
        )

    def respond(self, request, **options):
        return self.chat.respond(
            request,
            **{
                "actual_tls": True,
                "deadline": monotonic() + 2,
                "check": self.check,
                "write": self.write,
                "interrupt": self.interrupt,
                "retire": self.retire,
                **options,
            },
        )

    def refused(self, request, **options):
        with self.assertRaisesRegex(
            web_named_headers.NamedRequestError,
            r"^Named web request was not admitted\.$",
        ):
            self.respond(request, **options)

    @contextmanager
    def send_native(self, upstream, *, confirmed=True):
        """Only substitute the Podman-side relay and its cleanup evidence."""
        threads, errors, operations = [], [], []

        def start(operation, launcher):
            self.assertIs(launcher, box)
            self.assertIs(operation._control, self.lease.control)
            self.assertIs(operation._scope.browser, self.browser)
            self.assertEqual(
                operation._scope.owner_generation, self.identity.generation
            )
            self.assertEqual(operation._scope.session_id, _ID)
            self.assert_registered()
            operations.append(operation)
            host, peer = socket.socketpair()
            peer.settimeout(3)

            def serve():
                with peer:
                    try:
                        upstream.serve(peer)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    except Exception as error:
                        errors.append(error)

            child = native.FixtureChild(lambda: None)
            child.confirmed = confirmed
            operation.track(host)
            operation.register_workspace(child)
            runner = Thread(target=serve)
            threads.append(runner)
            runner.start()

        with patch.object(web_studio_send.StudioSend, "_start", start):
            try:
                yield operations
            finally:
                for thread in threads:
                    thread.join(timeout=3)
                    self.assertFalse(thread.is_alive())
                if errors:
                    raise errors[0]

    def read_worker(self, value, *, exit_code=0):
        wire = web_studio_conversation._READ_READY + json.dumps(value).encode("utf-8")
        cmd = box.ContainerCommand(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                "import os,sys; assert os.read(0,1)==b'R'; "
                f"sys.stdout.buffer.write({wire!r}); sys.exit({exit_code})",
            ],
            box.maintenance_host_env(),
        )
        return patch.object(box.workspace_service, "client_command", return_value=cmd)

    def test_exact_same_origin_get_without_origin_uses_managed_worker(self):
        value = {
            "sessionID": _ID,
            "title": "Synthetic",
            "hasEarlier": False,
            "messages": [
                {
                    "id": f"msg_row{i}",
                    "role": "assistant",
                    "truncated": False,
                    "text": "x" * 4096,
                }
                for i in range(3)
            ],
        }
        with (
            self.read_worker(value) as worker,
            patch.object(
                web_native_pair.NativeCookie, "upstream_cookie"
            ) as native_cookie,
            patch.object(web_studio_send.StudioSend, "execute") as attempt,
        ):
            self.assertIsNone(self.respond(self.request()))
        native_cookie.assert_not_called()
        self.connector.assert_not_called()
        attempt.assert_not_called()
        self.retire.assert_called_once()
        self.write.assert_called_once()
        reply = self.write.call_args.args[0]
        self.assertEqual(reply.status, 200)
        self.assertGreater(len(reply.body), web_named_transport._RESPONSE_LIMIT)
        self.assertEqual(json.loads(reply.body), value)
        self.assertEqual(dict(reply.headers)["Cache-Control"], "no-store")
        self.assertNotIn("Set-Cookie", dict(reply.headers))
        _, record, connection, argv = worker.call_args.args
        self.assertEqual(record["container"], self.identity.container_id)
        self.assertEqual(connection["url"], self.identity.endpoint)
        self.assertEqual(argv[-3:], [self.identity.endpoint, native.PROJECT, _ID])
        self.assertNotIn(self.token, " ".join(argv))

    def test_read_cleanup_fence_is_not_converted_to_a_reply(self):
        value = {
            "sessionID": _ID,
            "title": "Synthetic",
            "hasEarlier": False,
            "messages": [],
        }
        with self.read_worker(value, exit_code=1) as worker:
            self.refused(self.request(origin=self.origin))
        worker.assert_called_once()
        self.write.assert_not_called()
        self.retire.assert_called_once()
        self.assertFalse(self.admission.wait_drained(0))
        self.assertIsNone(self.admission.lookup(self.binding, self.token))

    def test_send_uses_existing_native_contract_and_fixed_acknowledgment(self):
        upstream = native.FixtureNative()
        with (
            self.send_native(upstream) as operations,
            patch.object(
                web_native_pair.NativeCookie, "upstream_cookie"
            ) as native_cookie,
        ):
            self.assertIsNone(self.respond(self.request("POST")))
        self.assertEqual(len(operations), 1)
        native_cookie.assert_not_called()
        self.connector.assert_not_called()
        posts = [item for item in upstream.requests if item[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1], native.ROUTE + "/prompt")
        self.assertEqual(
            json.loads(posts[0][2]),
            {"text": _TEXT, "delivery": "queue", "resume": True},
        )
        self.write.assert_called_once()
        reply = self.write.call_args.args[0]
        self.assertEqual((reply.status, reply.body), (200, b'{"status":"queued"}'))
        self.retire.assert_called_once()

    def test_refused_without_post_and_unconfirmed_after_post_are_distinct(self):
        for expected in ("refused", "unconfirmed"):
            with self.subTest(expected=expected):
                upstream = native.FixtureNative()
                if expected == "refused":
                    upstream.inbox = [{"type": "synthetic"}]
                else:
                    upstream.ack_status = "409 Conflict"
                with self.send_native(upstream):
                    self.respond(self.request("POST"))
                reply = self.write.call_args.args[0]
                self.assertEqual(reply.status, 403 if expected == "refused" else 503)
                self.assertEqual(json.loads(reply.body), {"status": expected})
                self.assertEqual(
                    sum(method == "POST" for method, *_ in upstream.requests),
                    int(expected == "unconfirmed"),
                )
                self.assertIs(
                    self.admission.lookup(self.binding, self.token), self.browser
                )
                self.write.reset_mock()

    def test_unsafe_request_shapes_never_reach_either_adapter(self):
        get, post = self.request(), self.request("POST")
        cases = {
            "account": replace(get, role="account"),
            "head": replace(get, method="HEAD"),
            "cross route method": replace(post, method="GET"),
            "native query": replace(get, target="/api/session/" + _ID),
            "encoded ID": replace(get, target=get.target.replace("=ses", "=%73es")),
            "duplicate query": replace(get, target=get.target + "&session=" + _ID),
            "fragments": replace(get, target=get.target + "#"),
            "send query": replace(post, target=post.target + "?text=extra"),
            "host": self.request(fields={"Host": self.authority + ".invalid"}),
            "cross-origin host": self.request(origin="https://else.localhost"),
            "cross-origin metadata": self.request(
                fields={"Sec-Fetch-Site": "same-site"}
            ),
            "no metadata": self.request(
                origin=self.origin, fields={"Sec-Fetch-Site": None}
            ),
            "navigate": self.request(fields={"Sec-Fetch-Mode": "navigate"}),
            "iframe": self.request(fields={"Sec-Fetch-Dest": "iframe"}),
            "referer": self.request(fields={"Referer": "https://else.localhost/"}),
            "post no Origin": self.request("POST", fields={"Origin": None}),
            "fetch user": self.request(fields={"Sec-Fetch-User": "?1"}),
            "forwarded": self.request(fields={"Forwarded": "host=else.localhost"}),
            "auth": self.request(fields={"Authorization": "Basic synthetic"}),
            "upgrade": self.request(fields={"Upgrade": "websocket"}),
            "content encoding": self.request(
                "POST", fields={"Content-Encoding": "identity"}
            ),
            "transfer encoding": self.request(
                "POST", fields={"Transfer-Encoding": "chunked"}
            ),
            "trailer": self.request("POST", fields={"Trailer": "x-check"}),
            "replay API": self.request("POST", fields={"Idempotency-Key": "opaque"}),
            "duplicate host": replace(
                get, headers=get.headers + (("host", self.authority),)
            ),
            "duplicate page": replace(
                get,
                headers=get.headers + ((chat.PAGE_HEADER.lower(), self.page.nonce),),
            ),
            "no page": self.request(fields={chat.PAGE_HEADER: None}),
            "malformed page": self.request(
                fields={chat.PAGE_HEADER: self.page.nonce + "="}
            ),
            "missing generation": self.request(fields={chat.GENERATION_HEADER: None}),
            "noncanonical generation": self.request(
                fields={chat.GENERATION_HEADER: "01"}
            ),
            "overlimit generation": self.request(
                fields={chat.GENERATION_HEADER: str(2**53)}
            ),
            "noncurrent generation": self.request(fields={chat.GENERATION_HEADER: "2"}),
            "get body": self.request(body=b"{}"),
            "get framing": self.request(fields={"Content-Length": "01"}),
            "post mismatch length": self.request(
                "POST", fields={"Content-Length": "1"}
            ),
            "post no length": self.request("POST", fields={"Content-Length": None}),
            "bad content type": self.request(
                "POST", fields={"Content-Type": "text/plain"}
            ),
            "duplicate JSON key": self.request(
                "POST", body=b'{"text":"x","text":"y","sessionID":"ses_studio_send"}'
            ),
            "extra JSON key": self.request(
                "POST", body=b'{"text":"x","sessionID":"ses_studio_send","resume":true}'
            ),
            "bad UTF-8": self.request(
                "POST", body=b'{"text":"\xff","sessionID":"ses_studio_send"}'
            ),
            "blank text": self.request(
                "POST", body=b'{"text":" ","sessionID":"ses_studio_send"}'
            ),
            "body cap": self.request("POST", body=b"x" * 8193),
            "wrong exact ID": self.request(
                "POST", body=b'{"text":"x","sessionID":"ses_other"}'
            ),
        }
        with (
            patch.object(chat.web_studio_read, "respond") as read,
            patch.object(web_studio_send.StudioSend, "execute") as send,
        ):
            for name, request in cases.items():
                with self.subTest(name=name):
                    self.refused(request)
            self.refused(get, actual_tls=False)
            self.refused(post, deadline=monotonic() - 1)
            read.assert_not_called()
            send.assert_not_called()
        self.write.assert_not_called()
        self.retire.assert_not_called()

    def test_only_live_app_grant_and_its_own_page_authorize(self):
        other_token = self.admission.issue(
            self.binding, principal=self.browser.principal
        )
        with patch.object(web_studio_send.StudioSend, "execute") as execute:
            for cookie in (
                "opencode_session=" + _COOKIE,
                "__Host-ocbox-bootstrap=" + self.token,
                f"__Host-ocbox-app={other_token}",
                f"__Host-ocbox-app={self.token}; __Host-ocbox-app={self.token}",
                None,
            ):
                self.refused(self.request("POST", fields={"Cookie": cookie}))
            self.assertTrue(self.admission.revoke(self.binding, self.browser))
            self.refused(self.request("POST"))
            execute.assert_not_called()
        self.write.assert_not_called()

    def test_composition_will_not_adopt_equal_identity_or_foreign_admission(self):
        for identity, admission in (
            (replace(self.identity), self.admission),
            (self.identity, web_admission.BrowserAdmission(self.binding)),
        ):
            with self.assertRaises(web_named_headers.NamedRequestError):
                chat.NamedStudioChat(self.owner, self.registry(identity, admission))

    def test_same_id_away_back_during_preflight_prevents_native_post(self):
        upstream = native.FixtureNative()

        def after_target(target):
            if sum(path == native.ROUTE for _, path, _ in upstream.requests) == 2:
                self.select(2, None)
                self.select(3)
            return target

        upstream.after_target = after_target
        with self.send_native(upstream):
            self.refused(self.request("POST"))
        self.assertTrue(upstream.requests)
        self.assertFalse(any(method == "POST" for method, *_ in upstream.requests))
        self.write.assert_not_called()
        self.retire.assert_called_once()

    def test_lost_owner_or_selection_after_post_never_writes_queued(self):
        for changed in ("selection", "owner"):
            with self.subTest(changed=changed):
                upstream = native.FixtureNative()

                def after_post():
                    if changed == "selection":
                        self.select(2, None)
                        self.select(3)
                    else:
                        self.observe_owner.return_value = replace(
                            self.identity, generation="e" * 32
                        )

                self.selected = self.select(7) if changed == "owner" else self.selected
                upstream.before_post = after_post
                with self.send_native(upstream):
                    self.refused(self.request("POST"))
                self.assertEqual(sum(m == "POST" for m, *_ in upstream.requests), 1)
                self.write.assert_not_called()

    def test_revoke_cancels_both_send_and_delivery_while_use_is_registered(self):
        upstream = native.FixtureNative()
        revocations = []

        def revoke():
            revocations.append(
                self.admission.begin_revocation(self.binding, self.browser)
            )

        upstream.before_post = revoke
        with self.send_native(upstream) as operations:
            self.refused(self.request("POST"))
        self.assertEqual(len(operations), 1)
        self.assertTrue(operations[0]._cancelled.is_set())
        self.write.assert_not_called()
        self.interrupt.assert_called_once()
        self.retire.assert_called_once()
        self.assertTrue(revocations[0].wait_drained(0))

    def test_revocation_after_registration_prevents_send_startup(self):
        revocations = []

        def check():
            with self.admission._lock:
                active = bool(self.admission._uses)
            if active and not revocations:
                revocations.append(
                    self.admission.begin_revocation(self.binding, self.browser)
                )

        with patch.object(web_studio_send.StudioSend, "_start") as start:
            self.refused(self.request("POST"), check=check)
        start.assert_not_called()
        self.write.assert_not_called()
        self.interrupt.assert_called_once()
        self.retire.assert_called_once()
        self.assertTrue(revocations[0].wait_drained(0))

    def test_uncertain_native_cleanup_fences_actual_grant_without_reply(self):
        upstream = native.FixtureNative()
        with self.send_native(upstream, confirmed=False) as operations:
            self.refused(self.request("POST"))
        self.assertEqual(sum(m == "POST" for m, *_ in upstream.requests), 1)
        self.write.assert_not_called()
        self.retire.assert_called_once()
        self.interrupt.assert_not_called()  # Downstream was retired before fencing.
        self.assertFalse(operations[0].cleanup_confirmed)
        self.assertFalse(self.admission.wait_drained(0))
        self.assertIsNone(self.admission.lookup(self.binding, self.token))
        self.refused(self.request())

    def test_failed_write_is_close_only_and_retirement_uncertainty_is_not_drain(self):
        upstream = native.FixtureNative()

        def incomplete(reply, deadline, *, check):
            self.assert_checked_write(reply, deadline, check=check)
            raise OSError("PRIVATE SYNTHETIC DOWNSTREAM DIAGNOSTIC")

        self.write.side_effect = incomplete
        self.retire.side_effect = lambda: False
        with self.send_native(upstream):
            self.refused(self.request("POST"))
        self.write.assert_called_once()
        self.retire.assert_called_once()
        self.assertFalse(self.admission.wait_drained(0))


if __name__ == "__main__":
    unittest.main()
