"""Pure request fixtures for the unserved enrollment dispatcher."""

import base64
import json
import os
import socket
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
from unittest.mock import Mock, patch

from oc_box import web_accounts, web_enrollment, web_enrollment_ingress as ingress
from oc_box import web_local_origins, web_named_headers, web_registration_worker
from oc_box import web_named_transport
from test_web_enrollment import (
    ACCOUNT,
    KE1,
    KE2,
    KE3,
    NONCE,
    ORIGINS,
    OTHER_BROWSER,
    RECORD,
    REQUEST,
    RESPONSE,
    REVIEW,
    EnrollmentFixture,
    token,
)


START = ingress.START_PATH
PROVE = ingress.PROOF_START_PATH
FINISH = ingress.PROOF_FINISH_PATH
REFUSED = b'{"error":"Named enrollment refused."}'


def b64(value):
    return base64.b64encode(value).decode("ascii")


def wire(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("ascii")


class EnrollmentIngressTests(EnrollmentFixture):
    def setUp(self):
        super().setUp()
        self.bind()

    def bind(self, **edits):
        self.bootstrap, self.binding, claim = self.claim()
        self.addCleanup(self.bootstrap.close)
        self.reviewed = self.approval(claim=claim, **edits)
        self.dispatch = ingress.EnrollmentIngress(
            origins=ORIGINS, approval=self.reviewed
        )

    def headers(self, body):
        return {
            "Host": ACCOUNT.removeprefix("https://"),
            "Origin": ACCOUNT,
            "Cookie": "__Host-ocbox-account=" + NONCE,
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "same-origin",
            "Sec-Fetch-Dest": "empty",
        }

    def send(self, target=START, body=None, *, header_edits=None, extra=(), **edits):
        if body is None:
            body = wire({"request": b64(REQUEST)})
        if type(body) is dict:
            body = wire(body)
        fields = self.headers(body)
        for key, value in (header_edits or {}).items():
            if value is None:
                del fields[key]
            else:
                fields[key] = value
        args = {
            "method": "POST",
            "target": target,
            "body": body,
            "actual_tls": True,
            "headers": tuple(fields.items()) + extra,
            "check": lambda: None,
            "deadline": monotonic() + 10,
        }
        return self.dispatch(**(args | edits))

    def refused(self, response):
        self.assertEqual((response.status, response.body), (403, REFUSED))
        self.assertNotIn("Set-Cookie", dict(response.headers))

    def accepted(self, response):
        self.assertEqual(response.status, 200)
        headers = dict(response.headers)
        self.assertEqual(headers["Content-Length"], str(len(response.body)))
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Set-Cookie", headers)
        self.assertNotIn("Location", headers)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        response.check_write()
        return json.loads(response.body)

    def prepared(self):
        challenge = self.accepted(self.send())["challenge"]
        response = self.send(PROVE, self.proof_start(challenge))
        self.accepted(response)
        return challenge

    def proof_start(self, challenge):
        return {"challenge": challenge, "registration": b64(RECORD), "ke1": b64(KE1)}

    def proof_finish(self, challenge):
        return {"challenge": challenge, "ke3": b64(KE3)}

    def test_final_wire_delivery_leaves_result_for_the_host_not_the_browser(self):
        challenge = self.prepared()
        writer, reader = socket.socketpair()
        with (
            writer,
            reader,
            patch.object(web_accounts, "commit_registration") as commit,
        ):
            final = self.send(FINISH, self.proof_finish(challenge), stream=writer)
            self.accepted(final)
            self.assertIsNone(self.reviewed.take_for_host())
            self.assertIsNone(self.reviewed.take_for_fixture())
            # A repeated browser finish can't burn the one pending delivery.
            self.refused(self.send(FINISH, self.proof_finish(challenge), stream=writer))
            self.assertEqual(
                ingress.write_enrollment_reply(
                    writer, final, monotonic() + 2, check=lambda: None
                ),
                200,
            )
            writer.shutdown(socket.SHUT_WR)
            with reader.makefile("rb") as stream:
                head, body = stream.read(4096).split(b"\r\n\r\n", 1)
            self.assertTrue(head.startswith(b"HTTP/1.1 200"))
            self.assertNotIn(b"Set-Cookie:", head)
            self.assertEqual(body, b'{"verified":true}')
            result = self.reviewed.take_for_host()
            self.assertEqual(result.registration, RECORD)
            self.assertIs(result.review, REVIEW)
            self.assertTrue(result.current())
            self.assertIsNone(self.reviewed.take_for_host())
            self.retire(self.bootstrap, self.binding)
            self.assertFalse(result.current())
            commit.assert_not_called()

    def test_partial_final_wire_delivery_cancels_tentative_host_result(self):
        challenge = self.prepared()

        def disconnect(writer, response, deadline, *, check):
            check()
            writer.sendall(b"HTTP/1.1 200")
            raise BrokenPipeError()

        writer, reader = socket.socketpair()
        with (
            writer,
            reader,
            patch.object(
                web_named_transport, "_write_response", side_effect=disconnect
            ) as write,
        ):
            final = self.send(FINISH, self.proof_finish(challenge), stream=writer)
            self.assertIsNone(self.reviewed.take_for_host())
            with self.assertRaises(web_named_headers.NamedRequestError):
                ingress.write_enrollment_reply(
                    writer, final, monotonic() + 2, check=lambda: None
                )
            write.assert_called_once()
            self.assertEqual(reader.recv(128), b"HTTP/1.1 200")
            with self.assertRaises(web_named_headers.NamedRequestError):
                ingress.write_enrollment_reply(
                    writer, final, monotonic() + 2, check=lambda: None
                )
            write.assert_called_once()
        self.assertIsNone(self.reviewed.take_for_host())
        self.refused(self.send(FINISH, self.proof_finish(challenge)))

    def test_exact_reply_only_and_successful_writer_cannot_be_replayed(self):
        challenge = self.prepared()
        writer, reader = socket.socketpair()
        with writer, reader:
            final = self.send(FINISH, self.proof_finish(challenge), stream=writer)
            for other in (replace(final), replace(final, _final=None)):
                with self.assertRaises(web_named_headers.NamedRequestError):
                    ingress.write_enrollment_reply(
                        writer, other, monotonic() + 2, check=lambda: None
                    )
            replace(final).abort_write()  # A copy is not authority to abort either.
            with patch.object(
                web_named_transport,
                "_write_response",
                wraps=web_named_transport._write_response,
            ) as write:
                self.assertEqual(
                    ingress.write_enrollment_reply(
                        writer, final, monotonic() + 2, check=lambda: None
                    ),
                    200,
                )
                with self.assertRaises(web_named_headers.NamedRequestError):
                    ingress.write_enrollment_reply(
                        writer, final, monotonic() + 2, check=lambda: None
                    )
                write.assert_called_once()
            self.assertIsNotNone(self.reviewed.take_for_host())
            self.assertIsNone(self.reviewed.take_for_host())

    def test_unbound_or_substituted_socket_cannot_be_rebound_after_proof(self):
        for transport_free in (True, False):
            with self.subTest(transport_free=transport_free):
                self.bind()
                challenge = self.prepared()
                writer, reader = socket.socketpair()
                other, peer = socket.socketpair()
                with writer, reader, other, peer:
                    final = self.send(
                        FINISH,
                        self.proof_finish(challenge),
                        stream=None if transport_free else writer,
                    )
                    self.accepted(final)
                    for stream in (other, writer):
                        with self.assertRaises(web_named_headers.NamedRequestError):
                            ingress.write_enrollment_reply(
                                stream, final, monotonic() + 2, check=lambda: None
                            )
                    for socket_reader in (reader, peer):
                        socket_reader.setblocking(False)
                        with self.assertRaises(BlockingIOError):
                            socket_reader.recv(1)
                    self.assertIsNone(self.reviewed.take_for_host())
                    self.assertTrue(self.reviewed.drained())

    def test_complete_wire_bytes_without_postcheck_do_not_release_result(self):
        challenge = self.prepared()
        allowed = True
        checked_write = web_named_transport._write_response

        def lose_after_write(*args, **kwargs):
            nonlocal allowed
            checked_write(*args, **kwargs)
            allowed = False

        writer, reader = socket.socketpair()
        with (
            writer,
            reader,
            patch.object(web_named_transport, "_write_response", lose_after_write),
        ):
            final = self.send(FINISH, self.proof_finish(challenge), stream=writer)
            with self.assertRaises(web_named_headers.NamedRequestError):
                ingress.write_enrollment_reply(
                    writer, final, monotonic() + 2, check=lambda: allowed
                )
            writer.shutdown(socket.SHUT_WR)
            with reader.makefile("rb") as stream:
                self.assertTrue(
                    stream.read(4096).endswith(b'\r\n\r\n{"verified":true}')
                )
        self.assertIsNone(self.reviewed.take_for_host())
        self.assertTrue(self.reviewed.drained())

    def test_interrupted_writer_and_retired_delivered_result_cannot_be_taken(self):
        for interrupted in (True, False):
            with self.subTest(interrupted=interrupted):
                self.bind()
                challenge = self.prepared()
                writer, reader = socket.socketpair()
                with writer, reader:
                    final = self.send(
                        FINISH, self.proof_finish(challenge), stream=writer
                    )
                    if interrupted:
                        with (
                            patch.object(
                                web_named_transport,
                                "_write_response",
                                side_effect=KeyboardInterrupt,
                            ),
                            self.assertRaises(KeyboardInterrupt),
                        ):
                            ingress.write_enrollment_reply(
                                writer, final, monotonic() + 2, check=lambda: None
                            )
                    else:
                        self.assertEqual(
                            ingress.write_enrollment_reply(
                                writer, final, monotonic() + 2, check=lambda: None
                            ),
                            200,
                        )
                        self.retire(self.bootstrap, self.binding)
                    self.assertIsNone(self.reviewed.take_for_host())
                    self.assertTrue(self.reviewed.drained())

    def test_bound_request_deadline_cannot_be_extended_by_writer(self):
        challenge = self.prepared()
        writer, reader = socket.socketpair()
        with (
            writer,
            reader,
            patch.object(web_enrollment, "monotonic", return_value=10) as clock,
            patch.object(web_named_transport, "monotonic", return_value=10),
            patch.object(web_named_transport, "_write_response") as write,
        ):
            final = self.send(
                FINISH, self.proof_finish(challenge), stream=writer, deadline=11
            )
            self.accepted(final)
            clock.return_value = 11
            with self.assertRaises(web_named_headers.NamedRequestError):
                ingress.write_enrollment_reply(writer, final, 20, check=lambda: None)
            write.assert_not_called()
            self.assertIsNone(self.reviewed.take_for_host())

    def test_running_writer_excludes_replay_and_races_cancellation_atomically(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                self.bind()
                challenge = self.prepared()
                entered, proceed = Event(), Event()
                end = web_enrollment.EnrollmentApproval._end_final_write

                def pause_ack(approval, http, writer, *, deadline=None):
                    if deadline is not None:
                        entered.set()
                        if not proceed.wait(5):
                            raise TimeoutError
                    return end(approval, http, writer, deadline=deadline)

                writer, reader = socket.socketpair()
                with (
                    writer,
                    reader,
                    patch.object(
                        web_enrollment.EnrollmentApproval, "_end_final_write", pause_ack
                    ),
                    ThreadPoolExecutor(max_workers=1) as pool,
                ):
                    final = self.send(
                        FINISH, self.proof_finish(challenge), stream=writer
                    )

                    def write():
                        return ingress.write_enrollment_reply(
                            writer, final, monotonic() + 10, check=lambda: None
                        )

                    result = pool.submit(write)
                    try:
                        self.assertTrue(entered.wait(5))
                        self.assertIsNone(self.reviewed.take_for_host())
                        self.assertFalse(self.reviewed.drained())
                        self.refused(self.send(FINISH, self.proof_finish(challenge)))
                        with self.assertRaises(web_named_headers.NamedRequestError):
                            write()
                        if cancel:
                            self.assertFalse(self.reviewed.close())
                            self.assertIsNone(self.reviewed.take_for_host())
                    finally:
                        proceed.set()
                    if cancel:
                        with self.assertRaises(web_named_headers.NamedRequestError):
                            result.result(5)
                        self.assertIsNone(self.reviewed.take_for_host())
                    else:
                        self.assertEqual(result.result(5), 200)
                        self.assertIsNotNone(self.reviewed.take_for_host())
                    self.assertTrue(self.reviewed.drained())
                    writer.shutdown(socket.SHUT_WR)
                    with reader.makefile("rb") as stream:
                        self.assertEqual(stream.read(4096).count(b"HTTP/1.1 200"), 1)

    @unittest.skipUnless(
        find_spec("cryptography"), "optional cryptography not installed"
    )
    def test_http_delivered_result_preserves_the_original_store_cas(self):
        phrase = "synthetic enrollment store passphrase"
        with (
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}),
            TemporaryDirectory() as directory,
        ):
            path = Path(directory) / "accounts"
            web_accounts.initialize(path, phrase, setup=REVIEW.setup)
            review = web_accounts.review_registration(
                path, phrase, REVIEW.username, workspace_ids=REVIEW.workspace_ids
            )
            previous = path.read_bytes()
            self.bind(review=review)
            challenge = self.prepared()
            writer, reader = socket.socketpair()
            with writer, reader:
                final = self.send(FINISH, self.proof_finish(challenge), stream=writer)
                self.assertIsNone(self.reviewed.take_for_host())
                self.assertEqual(path.read_bytes(), previous)
                self.assertEqual(
                    ingress.write_enrollment_reply(
                        writer, final, monotonic() + 2, check=lambda: None
                    ),
                    200,
                )
            self.assertEqual(path.read_bytes(), previous)
            result = self.reviewed.take_for_host()
            self.assertIs(result.review, review)

            web_accounts.add_account(
                path,
                phrase,
                "other-synthetic",
                workspace_ids=REVIEW.workspace_ids,
                registration=b"synthetic other record",
            )
            replaced = path.read_bytes()
            self.assertTrue(result.current())  # Store review is stale, claim is live.
            with self.assertRaisesRegex(
                web_accounts.WebAccountsError, "no longer current"
            ):
                web_accounts.commit_registration(
                    path,
                    phrase,
                    result.review,
                    registration=result.registration,
                    current=result.current,
                )
            self.assertEqual(path.read_bytes(), replaced)

    def test_exact_three_rounds_disclose_only_protocol_and_proof_boolean(self):
        with patch.object(web_accounts, "commit_registration") as commit:
            first = self.send()
            payload = self.accepted(first)
            self.assertEqual(tuple(payload), ("challenge", "response"))
            self.assertEqual(payload["response"], b64(RESPONSE))
            challenge = payload["challenge"]
            self.assertNotIn(challenge, repr(first))
            self.assertIsNone(self.reviewed.take_for_host())

            second = self.send(PROVE, self.proof_start(challenge))
            proof = self.accepted(second)
            self.assertEqual(tuple(proof), ("ke2", "context"))
            self.assertEqual(proof["ke2"], b64(KE2))
            self.assertEqual(len(base64.b64decode(proof["context"])), 32)
            self.assertIsNone(self.reviewed.take_for_host())
            verdict = self.send(FINISH, self.proof_finish(challenge))
            self.assertEqual(self.accepted(verdict), {"verified": True})
            self.assertEqual(verdict.body, b'{"verified":true}')
            # Transport-free research can prove KE3 but cannot issue a result.
            self.assertIsNone(self.reviewed.take_for_host())
            self.assertIsNone(self.reviewed.take_for_fixture())
            self.refused(self.send(FINISH, self.proof_finish(challenge)))
            commit.assert_not_called()
        self.exchange.finish.assert_called_once_with(KE3)
        self.assertTrue(self.reviewed.drained())

    def test_only_literal_post_routes_and_actual_tls(self):
        for route in (
            "/start",
            "/finish",
            "/enroll/start/",
            "/enroll/start?",
            "/enroll%2fstart",
            "/enroll//start",
            "/enroll/./start",
            "/Enroll/start",
            ACCOUNT + START,
            "/x" * 2048,
        ):
            with self.subTest(route=route):
                self.refused(self.send(route))
        for edits in (
            {"method": "OPTIONS"},
            {"method": "GET"},
            {"method": "post"},
            {"actual_tls": False},
            {"actual_tls": 1},
            {"actual_tls": "https"},
            {"deadline": float("inf")},
            {"check": None},
        ):
            self.refused(self.send(**edits))
        self.respond.assert_not_called()

    def test_host_origin_cookie_and_all_fetch_metadata_are_mandatory(self):
        for key, value in (
            ("Host", None),
            ("Host", "accounts.ocbox.test"),
            ("Host", "accounts.ocbox.test:9444"),
            ("Origin", None),
            ("Origin", "null"),
            ("Origin", ORIGINS.workspace.value),
            ("Origin", ACCOUNT + "/"),
            ("Sec-Fetch-Site", None),
            ("Sec-Fetch-Site", "same-site"),
            ("Sec-Fetch-Site", "cross-site"),
            ("Sec-Fetch-Mode", None),
            ("Sec-Fetch-Mode", "navigate"),
            ("Sec-Fetch-Mode", "cors"),
            ("Sec-Fetch-Dest", None),
            ("Sec-Fetch-Dest", "document"),
            ("Cookie", None),
            ("Cookie", "__Host-ocbox-bootstrap=" + NONCE),
            ("Cookie", "__Host-ocbox-account=" + OTHER_BROWSER),
            (
                "Cookie",
                "__Host-ocbox-account=" + NONCE + "; __Host-ocbox-account=" + NONCE,
            ),
        ):
            with self.subTest(key=key):
                self.refused(self.send(header_edits={key: value}))
        for header in (
            ("Origin", "null"),
            ("HOST", ACCOUNT.removeprefix("https://")),
            ("Cookie", "other=x"),
            ("X-Forwarded-Proto", "https"),
            ("Authorization", "untrusted"),
            ("Sec-Fetch-User", "?1"),
        ):
            self.refused(self.send(extra=(header,)))
        self.respond.assert_not_called()
        self.accepted(self.send())

    def test_exact_body_framing_and_no_upgrade(self):
        for header in (
            ("Content-Length", "0"),
            ("Content-Length", "058"),
            ("Transfer-Encoding", "chunked"),
            ("Content-Encoding", "gzip"),
            ("Expect", "100-continue"),
            ("Trailer", "format"),
            ("Te", "trailers"),
            ("Upgrade", "websocket"),
            ("Sec-WebSocket-Key", "fixture"),
            ("Proxy-Connection", "keep-alive"),
            ("HTTP2-Settings", "sample"),
            ("Connection", "close, keep-alive"),
            ("Content-Type", "application/json; charset=utf-8"),
        ):
            key, value = header
            if key in ("Content-Length", "Content-Type"):
                self.refused(self.send(header_edits={key: value}))
            else:
                self.refused(self.send(extra=(header,)))
        self.refused(self.send(extra=(("content-length", "58"),)))
        self.refused(self.send(headers=tuple((f"X-{n}", "x") for n in range(65))))
        self.respond.assert_not_called()

    def test_reject_noncanonical_json_fields_widths_and_base64_padding_bits(self):
        message = b64(REQUEST)
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        alternate = message[:-2] + alphabet[alphabet.index(message[-2]) ^ 1] + "="
        self.assertEqual(base64.b64decode(alternate), REQUEST)
        valid = wire({"request": message})
        for body in (
            b"",
            b"x" * (ingress.MAX_BODY_BYTES + 1),
            valid + b"\n",
            valid[:-1],
            valid.replace(b'"request"', b'"requ\\u0065st"'),
            b"\xef\xbb\xbf" + valid,
            wire({"request": alternate}),
            wire({"request": message.rstrip("=")}),
            wire({"request": b64(bytes(31))}),
            wire({"request": b64(bytes(33))}),
            wire({"request": [message]}),
            wire({"request": {"raw": message}}),
            wire({"request": message, "password": "untrusted"}),
            wire({"request": message, "workspace": "untrusted"}),
            b'{"request":"'
            + message.encode()
            + b'","request":"'
            + message.encode()
            + b'"}',
            valid.replace(b'":"', b'": "'),
        ):
            self.refused(self.send(body=body))
        self.respond.assert_not_called()
        challenge = self.accepted(self.send())["challenge"]
        good = self.proof_start(challenge)
        for target, payload in (
            (
                PROVE,
                {
                    "ke1": good["ke1"],
                    "registration": good["registration"],
                    "challenge": challenge,
                },
            ),
            (PROVE, good | {"challenge": token(2)}),
            (PROVE, good | {"challenge": challenge[:-1] + "B"}),
            (PROVE, good | {"registration": b64(bytes(191))}),
            (PROVE, good | {"ke1": b64(bytes(97))}),
            (FINISH, self.proof_finish(challenge)),
        ):
            self.refused(self.send(target, payload))
        self.validate.assert_not_called()
        self.accepted(self.send(PROVE, good))
        self.refused(self.send(FINISH, {"challenge": challenge, "ke3": b64(bytes(65))}))
        self.exchange.finish.assert_not_called()
        self.accepted(self.send(FINISH, self.proof_finish(challenge)))

    def test_check_on_foreign_cookie_or_challenge_cannot_burn_the_round(self):
        check = Mock(side_effect=RuntimeError("untrusted request is closed"))
        self.refused(
            self.send(
                check=check,
                header_edits={
                    "Cookie": "__Host-ocbox-account=" + OTHER_BROWSER,
                },
            )
        )
        challenge = self.accepted(self.send())["challenge"]
        self.refused(self.send(PROVE, self.proof_start(token(4)), check=check))
        check.assert_not_called()
        self.accepted(self.send(PROVE, self.proof_start(challenge)))

    def test_expired_or_cancelled_start_never_discloses_the_response(self):
        with patch.object(web_enrollment, "monotonic", return_value=10) as clock:
            self.respond.side_effect = lambda **_: (
                setattr(clock, "return_value", 11) or RESPONSE
            )
            self.refused(self.send(deadline=11))
        self.assertEqual(self.respond.call_args.kwargs["deadline"], 11)
        self.refused(self.send())
        self.assertTrue(self.reviewed.drained())

    def test_request_deadline_after_validation_does_not_start_ocop(self):
        challenge = self.accepted(self.send())["challenge"]
        with patch.object(web_enrollment, "monotonic", return_value=10) as clock:
            self.validate.side_effect = lambda **_: (
                setattr(clock, "return_value", 11) or RECORD
            )
            self.refused(self.send(PROVE, self.proof_start(challenge), deadline=11))
        self.validate.assert_called_once_with(RECORD, deadline=11)
        self.verifier.start.assert_not_called()
        self.assertTrue(self.reviewed.drained())

    def test_each_connection_has_a_deadline_without_shortening_proof_lifetime(self):
        challenge = self.accepted(self.send())["challenge"]
        writer, reader = socket.socketpair()
        with (
            writer,
            reader,
            patch.object(web_enrollment, "monotonic", return_value=10) as clock,
            patch.object(web_named_transport, "monotonic", return_value=12),
        ):
            self.accepted(self.send(PROVE, self.proof_start(challenge), deadline=11))
            clock.return_value = (
                12  # The first connection's cutoff is not the next one's.
            )
            final = self.send(
                FINISH, self.proof_finish(challenge), deadline=13, stream=writer
            )
            self.accepted(final)
            self.assertIsNone(self.reviewed.take_for_host())
            self.assertEqual(
                ingress.write_enrollment_reply(writer, final, 13, check=lambda: None),
                200,
            )
        self.assertGreater(self.verifier.start.call_args.kwargs["deadline"], 11)
        self.assertIsNotNone(self.reviewed.take_for_host())

    def test_lost_transport_guard_never_reports_proof_completed(self):
        challenge = self.prepared()
        permitted = True

        def verify(_proof):
            nonlocal permitted
            permitted = False
            return True

        self.exchange.finish.side_effect = verify
        response = self.send(
            FINISH, self.proof_finish(challenge), check=lambda: permitted
        )
        self.refused(response)
        self.assertIsNone(self.reviewed.take_for_host())
        self.assertTrue(self.reviewed.drained())

    def test_final_reply_check_can_fence_after_coordinator_completion(self):
        challenge = self.prepared()

        def fail_when_finished():
            if self.reviewed.drained():
                raise RuntimeError("private transport diagnosis")

        response = self.send(
            FINISH, self.proof_finish(challenge), check=fail_when_finished
        )
        self.refused(response)
        self.assertIsNone(self.reviewed.take_for_host())
        self.assertTrue(self.reviewed.drained())

    def test_prepared_reply_cannot_write_after_retirement_or_explicit_abort(self):
        response = self.send()
        self.accepted(response)
        self.retire(self.bootstrap, self.binding)
        with self.assertRaisesRegex(
            web_named_headers.NamedRequestError, "^Named web request"
        ):
            response.check_write()
        self.assertTrue(self.reviewed.drained())
        self.assertIsNone(self.reviewed.take_for_host())

        self.reviewed = self.approval()
        self.dispatch = ingress.EnrollmentIngress(
            origins=ORIGINS, approval=self.reviewed
        )
        challenge = self.prepared()
        finish = self.send(FINISH, self.proof_finish(challenge))
        self.assertEqual(finish.status, 200)
        finish.abort_write()
        with self.assertRaises(web_named_headers.NamedRequestError):
            finish.check_write()
        self.assertIsNone(self.reviewed.take_for_host())

    def test_writer_deadline_is_absolute_after_dispatch_and_fences_pending_proof(self):
        challenge = self.accepted(self.send())["challenge"]
        with patch.object(web_enrollment, "monotonic", return_value=10) as clock:
            response = self.send(PROVE, self.proof_start(challenge), deadline=11)
            self.accepted(response)
            clock.return_value = 11
            with self.assertRaises(web_named_headers.NamedRequestError):
                response.check_write()
        self.assertIsNone(self.reviewed.take_for_host())
        self.assertTrue(self.reviewed.drained())

    def test_cleanup_error_keeps_sticky_fence_without_automatic_recheck(self):
        self.respond.side_effect = web_registration_worker.RegistrationCleanupError
        self.registrar_close.side_effect = (False, True)
        reply = self.send()
        self.refused(reply)
        reply.abort_write()  # A generic refusal has no protocol or cleanup authority.
        self.refused(self.send())
        self.assertTrue(self.reviewed.cleanup_failed)
        self.assertFalse(self.reviewed.drained())
        self.respond.assert_called_once()
        self.registrar_close.assert_called_once()
        self.assertTrue(self.reviewed.close())
        self.assertTrue(self.reviewed.cleanup_failed)
        self.assertIsNone(self.reviewed.take_for_host())

    def test_composition_refuses_wrong_origin_profile_or_unapproved_object(self):
        with self.assertRaises(ValueError):
            ingress.EnrollmentIngress(
                origins=web_local_origins.LocalOrigins(workspace_port=9445),
                approval=self.reviewed,
            )
        with self.assertRaises(ValueError):
            ingress.EnrollmentIngress(origins=ORIGINS, approval=object())

    def test_binding_is_one_way_and_blocks_direct_rounds_or_late_rebinding(self):
        with self.assertRaises(ValueError):
            ingress.EnrollmentIngress(origins=ORIGINS, approval=self.reviewed)
        self.assertIsNone(self.start(self.reviewed))
        challenge = self.accepted(self.send())["challenge"]
        reply = web_enrollment.RegistrationResponse(challenge, RESPONSE)
        self.assertIsNone(self.prepare(self.reviewed, reply))
        self.accepted(self.send(PROVE, self.proof_start(challenge)))
        self.assertFalse(self.finish(self.reviewed, reply))
        self.exchange.finish.assert_not_called()
        self.accepted(self.send(FINISH, self.proof_finish(challenge)))

        direct = self.approval()
        reply = self.start(direct)
        with self.assertRaises(ValueError):
            ingress.EnrollmentIngress(origins=ORIGINS, approval=direct)
        self.assertIsNotNone(self.prepare(direct, reply))
        self.assertTrue(self.finish(direct, reply))
        with self.assertRaises(ValueError):
            ingress.EnrollmentIngress(origins=ORIGINS, approval=direct)
        self.assertIsNone(direct.take_for_host())
        self.assertEqual(direct.take_for_fixture(), RECORD)
