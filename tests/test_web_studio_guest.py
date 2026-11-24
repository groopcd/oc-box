"""Offline guest admission protocol and lifetime checks; no gateway is started."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import io
import os
from queue import SimpleQueue
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oc_box import web_admission, web_sessions, web_studio_guest as guest


ORIGIN = "http://127.0.0.1:49152"
WORKSPACE, OWNER, CONTAINER = "1" * 32, "2" * 32, "3" * 64
COOKIE = "synthetic-direct-native=not-authority-by-format"


class GuestAdmissionTests(unittest.TestCase):
    def setUp(self):
        host_scope = patch.dict("os.environ", {"OC_BOX_CONTAINER": "0"})
        host_scope.start()
        self.addCleanup(host_scope.stop)
        self.now = 100.0
        clock = patch.object(guest, "monotonic", lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.binding = guest.GuestBinding(WORKSPACE, OWNER, CONTAINER, ORIGIN)
        self.observe = Mock(side_effect=lambda binding, **_: binding)
        self.verify = Mock(side_effect=lambda binding, **_: binding)

    def gate(self, binding=None, **options):
        return guest.GuestAdmission(
            self.binding if binding is None else binding,
            observe_gateway=self.observe,
            verify_native=self.verify,
            **options,
        )

    def request(self, challenge, **overrides):
        body = challenge.value.encode("ascii")
        request = guest.ConfirmationRequest(
            "POST",
            guest.CONFIRM_PATH,
            (
                ("Host", self.binding.authority),
                ("Origin", self.binding.origin),
                ("Sec-Fetch-Site", "same-origin"),
                ("Sec-Fetch-Mode", "same-origin"),
                ("Sec-Fetch-Dest", "empty"),
                ("Content-Type", "text/plain"),
                ("Content-Length", str(len(body))),
                ("Cookie", COOKIE),
            ),
            body,
        )
        return replace(request, **overrides)

    def confirm(self, gate, request=None, *, cancel=lambda: None, deadline=1000):
        if request is None:
            request = self.request(gate.offer(self.binding))
        return gate.confirm(
            self.binding,
            request,
            cancel=cancel,
            deadline=deadline,
        )

    def start(self, gate, bootstrap, *, cancel=lambda: None, deadline=1000):
        return gate.start(
            self.binding,
            app=bootstrap.app,
            page=bootstrap.page,
            cancel=cancel,
            deadline=deadline,
        )

    def bootstrap(self, gate):
        use, bootstrap = self.confirm(gate)
        self.assertIsNotNone(bootstrap)
        self.assertTrue(gate.finish(use, confirmed=True))
        return bootstrap

    def test_exact_guest_origin_and_no_named_binding_or_lifetime_fallback(self):
        for origin in (
            "http://127.0.0.1",
            "http://127.0.0.1:0",
            "http://127.0.0.1:01",
            "http://127.0.0.1:65536",
            "http://127.0.0.1:+1",
            "http://127.0.0.1:49152/",
            "http://127.0.0.1:49152?x",
            "http://127.0.0.1:49152#x",
            "http://localhost:49152",
            "http://127.1:49152",
            "http://127.0.0.2:49152",
            "http://[::1]:49152",
            "http://user@127.0.0.1:49152",
            "https://127.0.0.1:49152",
            "HTTP://127.0.0.1:49152",
            "http://127.0.0.1:１",
            "http://127.0.0.1:49152\n",
        ):
            with self.subTest(origin=origin):
                with self.assertRaises(ValueError):
                    guest.GuestBinding(WORKSPACE, OWNER, CONTAINER, origin)
        for port in (1, 65535):
            edge = guest.GuestBinding(
                WORKSPACE, OWNER, CONTAINER, f"http://127.0.0.1:{port}"
            )
            self.assertEqual(edge.authority, f"127.0.0.1:{port}")
        for fields in (
            {"workspace_id": "x" * 32},
            {"owner_generation": "AB" * 16},
            {"container_id": CONTAINER[:-1]},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                replace(self.binding, **fields)
        with self.assertRaises(ValueError):
            web_admission.WebBinding(WORKSPACE, OWNER, ORIGIN)
        with self.assertRaises(ValueError):
            web_admission.BrowserAdmission(self.binding)
        named = web_admission.WebBinding(WORKSPACE, OWNER, "https://studio.example")
        with self.assertRaises(ValueError):
            self.gate(named)
        with (
            patch.dict("os.environ", {"OC_BOX_CONTAINER": "1"}),
            self.assertRaises(ValueError),
        ):
            self.gate()
        gate = self.gate()
        duplicate = replace(self.binding)
        self.assertIsNone(gate.offer(duplicate))
        gate.close()
        self.assertTrue(gate.wait_drained(0))
        self.assertIsNone(gate.offer(self.binding))
        with self.assertRaises(ValueError):
            self.gate()

    def test_one_use_uses_exact_pinned_native_callable_and_no_private_cookie_in_grant(
        self,
    ):
        gate = self.gate()
        challenge = gate.offer(self.binding)
        request = self.request(challenge)
        use, bootstrap = self.confirm(gate, request)
        self.verify.assert_called_once_with(
            self.binding,
            use=use,
            headers=(
                b"GET /api/info HTTP/1.1\r\nhost: 127.0.0.1:49152\r\n"
                b"origin: http://127.0.0.1:49152\r\ncookie: " + COOKIE.encode()
            ),
            authority="127.0.0.1:49152",
            deadline=110.0,
        )
        self.assertEqual(self.observe.call_count, 2)
        self.assertIsNotNone(bootstrap)
        self.assertNotEqual(bootstrap.app, bootstrap.page)
        self.assertNotIn(challenge.value, (bootstrap.app, bootstrap.page))
        self.assertNotIn(COOKIE, repr(request))
        self.assertNotIn(bootstrap.app, repr(bootstrap))
        self.assertFalse(hasattr(bootstrap.grant, "native_cookie"))
        self.assertEqual(
            set(gate._grants),
            {(guest._digest(bootstrap.app), guest._digest(bootstrap.page))},
        )
        self.assertIsNone(self.confirm(gate, request))
        self.assertTrue(gate.finish(use, confirmed=True))
        actual_use, grant = self.start(gate, bootstrap)
        self.assertIs(grant, bootstrap.grant)
        self.assertTrue(gate.current(actual_use))
        self.assertTrue(gate.finish(actual_use, confirmed=True))
        self.assertFalse(gate.current(actual_use))

    def test_lookalike_cookie_claim_truthy_result_or_verifier_failure_never_admits(
        self,
    ):
        gate = self.gate()
        for verdict in (
            True,
            1,
            None,
            replace(self.binding),
            "authenticated",
            RuntimeError("private synthetic verifier diagnostics"),
        ):

            def verify(_binding, **_):
                if isinstance(verdict, Exception):
                    raise verdict
                return verdict

            with self.subTest(verdict=type(verdict)):
                self.verify.side_effect = verify
                request = self.request(gate.offer(self.binding))
                use, bootstrap = self.confirm(gate, request)
                self.assertIsNone(bootstrap)
                self.assertTrue(gate.finish(use, confirmed=True))
                self.assertIsNone(self.confirm(gate, request))

    def test_cookie_header_block_matches_legacy_authenticator_with_synthetic_child(
        self,
    ):
        gate = self.gate()
        for line, authenticated in (
            (b"HTTP/1.1 401 Unauthorized", False),
            (b"HTTP/1.1 200 OK", True),
        ):
            wire = []

            def verify(binding, *, use, headers, authority, deadline):
                read_fd, write_fd = os.pipe()
                with os.fdopen(write_fd, "wb") as writer:
                    writer.write(line + b"\r\nContent-Length: 0\r\n\r\n")
                with os.fdopen(read_fd, "rb") as reader, io.BytesIO() as capture:
                    child = SimpleNamespace(stdin=capture, stdout=reader)
                    result = web_sessions._authenticated(child, headers, authority)
                    wire.append(capture.getvalue())
                return binding if result is True else None

            with self.subTest(authenticated=authenticated):
                self.verify.side_effect = verify
                use, bootstrap = self.confirm(gate)
                self.assertEqual(bootstrap is not None, authenticated)
                self.assertEqual(
                    wire,
                    [
                        b"GET /api/info HTTP/1.1\r\nConnection: close\r\n"
                        b"host: 127.0.0.1:49152\r\norigin: http://127.0.0.1:49152\r\n"
                        b"cookie: " + COOKIE.encode() + b"\r\n\r\n"
                    ],
                )
                self.assertTrue(gate.finish(use, confirmed=True))

    def test_confirmation_rejects_rewrites_missing_origin_ambiguous_or_over_bound_headers(
        self,
    ):
        gate = self.gate()
        challenge = gate.offer(self.binding)
        good = self.request(challenge)
        change = lambda name, new: tuple(
            (key, value if key != name else new) for key, value in good.headers
        )
        bad = [
            replace(good, method="GET"),
            replace(good, target=ORIGIN + guest.CONFIRM_PATH),
            replace(good, target=guest.CONFIRM_PATH + "?approved=1"),
            replace(good, body=good.body + b"\n"),
            replace(good, headers=dict(good.headers)),
            replace(good, headers=change("Host", "localhost:49152")),
            replace(good, headers=change("Origin", "http://127.0.0.1:49153")),
            replace(good, headers=change("Origin", ORIGIN + "/")),
            replace(good, headers=change("Origin", "null")),
            replace(good, headers=change("Sec-Fetch-Site", "same-site")),
            replace(good, headers=change("Sec-Fetch-Mode", "cors")),
            replace(good, headers=change("Sec-Fetch-Dest", "document")),
            replace(good, headers=change("Content-Length", "043")),
            replace(good, headers=change("Content-Type", "application/json")),
            replace(good, headers=change("Cookie", " \t")),
            replace(
                good, headers=change("Cookie", "c=1\r\nAuthorization: Basic pretend")
            ),
            replace(good, headers=change("Cookie", "x" * 4097)),
        ]
        for name in (
            "Origin",
            "Host",
            "Cookie",
            "Content-Length",
            "Sec-Fetch-Site",
            "Sec-Fetch-Mode",
            "Sec-Fetch-Dest",
        ):
            bad.append(
                replace(
                    good,
                    headers=tuple(pair for pair in good.headers if pair[0] != name),
                )
            )
        for name, value in (
            ("oRiGiN", ORIGIN),
            ("cookie", COOKIE),
            ("Authorization", "Basic pretend"),
            ("Proxy-Authorization", "Basic pretend"),
            ("Forwarded", "host=ignored"),
            ("X-Forwarded-Host", "ignored"),
            ("Transfer-Encoding", "chunked"),
            ("Connection", "Cookie"),
            ("Upgrade", "websocket"),
            ("Access-Control-Request-Method", "POST"),
        ):
            bad.append(replace(good, headers=good.headers + ((name, value),)))
        bad += [
            replace(
                good, headers=good.headers + tuple((f"X-{i}", "") for i in range(30))
            ),
            replace(
                good,
                headers=good.headers + (("X-One", "x" * 4096), ("X-Two", "y" * 4096)),
            ),
        ]
        for index, request in enumerate(bad):
            with self.subTest(case=index):
                self.assertIsNone(self.confirm(gate, request))
        self.verify.assert_not_called()
        self.observe.assert_not_called()
        use, bootstrap = self.confirm(gate, good)
        self.assertIsNotNone(bootstrap)  # none of the malformed attempts burned it
        self.assertTrue(gate.finish(use, confirmed=True))

    def test_distinct_app_page_exact_grant_and_new_gateway_even_with_repeated_entropy(
        self,
    ):
        replacement = replace(self.binding)
        gate, other = self.gate(), self.gate(replacement)
        with patch.object(guest.secrets, "token_bytes", side_effect=lambda n: b"x" * n):
            first = self.bootstrap(gate)
            second = self.bootstrap(gate)
            self.assertEqual(len({first.app, first.page, second.app, second.page}), 4)
            for app, page in (
                (second.app, first.page),
                (first.app, second.page),
                (first.page, first.app),
                (first.app, first.app),
            ):
                self.assertIsNone(
                    gate.start(
                        self.binding,
                        app=app,
                        page=page,
                        cancel=lambda: None,
                        deadline=1000,
                    )
                )
            self.assertFalse(
                gate.revoke(self.binding, guest.GuestGrant(first.grant.expires_at))
            )
            self.assertTrue(gate.revoke(self.binding, first.grant))
            third = self.bootstrap(gate)
            self.assertNotIn(first.app, (third.app, third.page))
            # Reuse of the same host port and metadata doesn't migrate credentials.
            request = self.request(other.offer(replacement))
            use, bootstrap = other.confirm(
                replacement, request, cancel=lambda: None, deadline=1000
            )
            self.assertIsNotNone(bootstrap)
            self.assertNotIn(bootstrap.app, (first.app, first.page))
            self.assertTrue(other.finish(use, confirmed=True))
            self.assertIsNone(
                other.start(
                    replacement,
                    app=first.app,
                    page=first.page,
                    cancel=lambda: None,
                    deadline=1000,
                )
            )

    def test_pending_attempt_grant_use_and_time_limits(self):
        gate = self.gate(
            max_pending=1,
            challenge_ttl=2,
            attempt_limit=1,
            max_uses=1,
            max_grants=1,
            grant_ttl=5,
        )
        expired = gate.offer(self.binding)
        self.assertIsNone(gate.offer(self.binding))
        self.now += 2
        self.assertIsNone(self.confirm(gate, self.request(expired)))
        first = self.bootstrap(gate)
        expired_while_busy = Mock()
        use, grant = self.start(gate, first, cancel=expired_while_busy)
        self.assertIs(grant, first.grant)
        self.assertIsNone(self.start(gate, first))
        self.assertEqual(use.deadline, 107)
        self.assertIsNone(self.confirm(gate))
        self.now += 5
        gate.expire()
        expired_while_busy.assert_called_once_with()
        self.assertTrue(use.cancelled)
        self.assertFalse(gate.current(use))
        self.assertIsNone(self.start(gate, first))
        self.assertTrue(gate.finish(use, confirmed=True))
        limited = self.request(gate.offer(self.binding))
        self.assertIsNone(self.confirm(gate, limited))
        self.now += 55
        self.assertIsNone(self.confirm(gate, limited))
        fresh = self.bootstrap(gate)
        self.assertIsNot(first.grant, fresh.grant)
        self.assertNotEqual(first.app, fresh.app)

    def test_full_grant_table_and_expired_verification_never_issue_late(self):
        gate = self.gate(max_grants=1, challenge_ttl=2)
        first = self.bootstrap(gate)
        denied = self.request(gate.offer(self.binding))
        self.assertIsNone(self.confirm(gate, denied))
        self.verify.assert_called_once()
        self.assertTrue(gate.revoke(self.binding, first.grant))
        self.assertIsNone(self.confirm(gate, denied))
        cancelled = Mock()

        def late(binding, **_):
            self.now += 2
            return binding

        self.verify.side_effect = late
        use, bootstrap = self.confirm(gate, cancel=cancelled)
        self.assertIsNone(bootstrap)
        self.assertTrue(use.cancelled)
        cancelled.assert_called_once_with()
        self.assertTrue(gate.finish(use, confirmed=True))

    def test_inflight_verifier_charged_once_and_cancelled_before_close_can_drain(self):
        entered, let_return = Event(), Event()
        cancel = Mock()
        gate = self.gate()
        req = self.request(gate.offer(self.binding))

        def verify(binding, *, use, **_):
            entered.set()
            self.assertTrue(let_return.wait(3))
            self.assertTrue(use.cancelled)
            return binding

        self.verify.side_effect = verify
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.confirm, gate, req, cancel=cancel)
            try:
                self.assertTrue(entered.wait(3))
                self.assertIsNone(self.confirm(gate, req))
                gate.close()
                gate.close()
                cancel.assert_called_once_with()
                self.assertFalse(gate.wait_drained(0))
            finally:
                let_return.set()
            use, bootstrap = future.result(timeout=3)
        self.verify.assert_called_once()
        self.assertIsNone(bootstrap)
        self.assertTrue(gate.finish(use, confirmed=True))
        self.assertTrue(gate.wait_drained(0))

    def test_cannot_discharge_a_use_while_its_verifier_can_still_access_the_lease(self):
        let_return = Event()
        gate = self.gate()
        verifying = SimpleQueue()

        def verify(binding, *, use, **_):
            verifying.put(use)
            let_return.wait(3)
            return binding

        self.verify.side_effect = verify
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.confirm, gate)
            try:
                use = verifying.get(timeout=3)
                gate.close()
                self.assertFalse(gate.finish(use, confirmed=True))
                self.assertFalse(gate.wait_drained(0))
            finally:
                let_return.set()
            self.assertEqual(future.result(timeout=3), (use, None))
        self.assertFalse(gate.finish(use, confirmed=True))
        self.assertFalse(gate.wait_drained(0))

    def test_gateway_loss_after_native_check_fences_other_active_work_even_if_check_denied(
        self,
    ):
        gate = self.gate()
        live = self.bootstrap(gate)
        cancelled, cancelled_check = Mock(), Mock()
        active, _ = self.start(gate, live, cancel=cancelled)
        lookalike = replace(self.binding)

        def verify(_binding, **_):
            self.observe.side_effect = lambda *_args, **_kw: lookalike
            return None

        self.verify.side_effect = verify
        check, bootstrap = self.confirm(gate, cancel=cancelled_check)
        self.assertIsNone(bootstrap)
        self.assertTrue(active.cancelled)
        self.assertTrue(check.cancelled)
        self.assertIsNone(self.start(gate, live))
        cancelled.assert_called_once_with()
        cancelled_check.assert_called_once_with()
        self.assertFalse(gate.wait_drained(0))
        self.assertTrue(gate.finish(active, confirmed=True))
        self.assertTrue(gate.finish(check, confirmed=True))
        self.assertTrue(gate.wait_drained(0))

    def test_logout_cancels_only_its_grant_and_retires_authority_before_callback(self):
        gate = self.gate()
        first, second = self.bootstrap(gate), self.bootstrap(gate)
        cancelled = Mock(side_effect=lambda: self.assertIsNone(self.start(gate, first)))
        first_use, _ = self.start(gate, first, cancel=cancelled)
        second_use, _ = self.start(gate, second)
        self.assertFalse(gate.wait_drained(0))  # never a lease release while open
        self.assertTrue(gate.revoke(self.binding, first.grant))
        self.assertFalse(gate.revoke(self.binding, first.grant))
        cancelled.assert_called_once_with()
        self.assertTrue(first_use.cancelled)
        self.assertTrue(gate.current(second_use))
        gate.close()
        self.assertTrue(second_use.cancelled)
        self.assertFalse(gate.wait_drained(0))  # includes the previously revoked use
        self.assertTrue(gate.finish(second_use, confirmed=True))
        self.assertFalse(gate.wait_drained(0))
        self.assertTrue(gate.finish(first_use, confirmed=True))
        self.assertTrue(gate.wait_drained(0))

    def test_drain_waits_for_cancellation_callback_delivery_even_if_use_finished(self):
        gate = self.gate()
        bootstrap = self.bootstrap(gate)
        entered, let_return = Event(), Event()

        def cancel():
            entered.set()
            if not let_return.wait(3):
                raise RuntimeError("synthetic synchronization timeout")

        use, _ = self.start(gate, bootstrap, cancel=cancel)
        with ThreadPoolExecutor(max_workers=1) as pool:
            closing = pool.submit(gate.close)
            try:
                self.assertTrue(entered.wait(3))
                self.assertTrue(use.cancelled)
                self.assertTrue(gate.finish(use, confirmed=True))
                self.assertFalse(gate.wait_drained(0))
            finally:
                let_return.set()
            closing.result(timeout=3)
        self.assertTrue(gate.wait_drained(0))

    def test_uncertain_cleanup_or_cancellation_exception_never_reports_drained(self):
        for failure in ("cleanup", "cancel"):
            with self.subTest(failure=failure):
                self.binding = replace(self.binding)
                gate = self.gate()
                bootstrap = self.bootstrap(gate)
                normal = Mock()
                first, _ = self.start(gate, bootstrap, cancel=normal)
                broken = (
                    Mock(side_effect=RuntimeError("private diagnostic"))
                    if failure == "cancel"
                    else Mock()
                )
                second, _ = self.start(gate, bootstrap, cancel=broken)
                if failure == "cleanup":
                    self.assertFalse(gate.finish(second, confirmed=False))
                    self.assertFalse(gate.finish(second, confirmed=True))
                else:
                    gate.close()
                    self.assertTrue(gate.finish(second, confirmed=True))
                self.assertTrue(gate.finish(first, confirmed=True))
                self.assertTrue(first.cancelled)
                self.assertTrue(second.cancelled)
                normal.assert_called_once_with()
                broken.assert_called_once_with()
                self.assertIsNone(gate.offer(self.binding))
                self.assertFalse(gate.wait_drained(0))


if __name__ == "__main__":
    unittest.main()
