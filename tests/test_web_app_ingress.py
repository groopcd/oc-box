"""Named app admission with synthetic credentials and transports only."""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock, patch

import test_web_pake_flow as fixture

from oc_box import (
    web_admission,
    web_bootstrap,
    web_handoff,
    web_local_origins,
    web_named_local,
    web_native_pair,
)
from oc_box import web_app_ingress as app

ENDPOINT = "http://127.0.0.1:43100"
NATIVE = "a" * 10 + "." + "b" * 43


class NamedAppIngressTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"})
        environment.start()
        self.addCleanup(environment.stop)
        self.now = 100.0
        for module in (web_admission, web_bootstrap, web_handoff, fixture.pake):
            clock = patch.object(module, "monotonic", side_effect=lambda: self.now)
            clock.start()
            self.addCleanup(clock.stop)
        self.origins = web_local_origins.LocalOrigins()
        self.origin = self.origins.workspace.value
        self.authority = self.origin.removeprefix("https://")
        self.binding = web_admission.WebBinding(
            fixture.WORKSPACE, "synthetic-owner", self.origin
        )
        self.owner = Mock(return_value=True)
        cookie = web_native_pair.NativeCookie(
            self.binding,
            ENDPOINT,
            self.authority,
            NATIVE,
            version="2.3.2",
            owner_current=self.owner,
        )
        self.pair = Mock(return_value=cookie)
        flow = fixture.pake.PakeFlow(
            self.binding,
            store=fixture.accounts.AccountStore(
                fixture.SETUP,
                (
                    fixture.accounts.Account(
                        "in-scope", (fixture.WORKSPACE,), fixture.REAL
                    ),
                ),
            ),
            backend=fixture.SyntheticBackend(),
            sizes=fixture.SIZES,
            auth_origin=self.origins.account.value,
        )
        self.sign_in = web_named_local.LocalNamedSignIn(
            origins=self.origins,
            binding=self.binding,
            flow=flow,
            owner_current=self.owner,
            pair_native=self.pair,
        )
        self.bootstrap = Mock(return_value=lambda: True)
        self.ingress = self.app_ingress()
        self.addCleanup(self.ingress.close)
        self.permit = app.MetadataPermit.READY

    def app_ingress(self, endpoint=ENDPOINT):
        return app.NamedAppIngress(
            self.sign_in,
            endpoint=endpoint,
            version="2.3.2",
            claim_bootstrap=self.bootstrap,
        )

    def ticket(self, browser=fixture.BROWSER):
        challenge = self.sign_in.start(
            browser_nonce=browser, username="in-scope", ke1=fixture.KE1
        )
        return self.sign_in.finish(
            browser_nonce=browser,
            challenge_id=challenge.challenge_id,
            ke3=fixture.PROOF,
        ).value

    def headers(self, cookie):
        return [("Host", self.authority), ("Origin", self.origin), ("Cookie", cookie)]

    def confirm(self, ticket, browser=fixture.BROWSER, **overrides):
        return self.ingress.confirm(
            **{
                "method": "POST",
                "target": app.CONFIRM_PATH,
                "headers": self.headers("__Host-ocbox-bootstrap=" + browser)
                + [("Content-Length", str(len(ticket)))],
                "actual_tls": True,
                "body": ticket.encode("ascii"),
                **overrides,
            }
        )

    def sign_in_browser(self, browser=fixture.BROWSER):
        return self.confirm(self.ticket(browser), browser)

    def paired_bootstrap(self, *, ttl=60):
        table = web_bootstrap.BrowserBootstrap(
            origins=self.origins, binding=self.binding, ttl=ttl
        )
        self.addCleanup(table.close)
        boot = table.issue(self.binding)
        cookie = table.accept(
            self.binding, auth_origin=self.origins.account.value, ticket=boot.value
        )
        self.assertIsNotNone(cookie)
        self.bootstrap.side_effect = lambda nonce: table.claim_app(self.binding, nonce)
        return table, boot.app_nonce, cookie

    def dispatch(self, token, **overrides):
        return self.ingress.dispatch(
            **{
                "method": "GET",
                "target": "/api/info",
                "headers": self.headers(app.APP_COOKIE_NAME + "=" + token),
                "actual_tls": True,
                "cancel": self.fail,
                "run": lambda *_: self.permit,
                **overrides,
            }
        )

    def logout(self, token, **overrides):
        return self.ingress.logout(
            **{
                "method": "POST",
                "target": app.LOGOUT_PATH,
                "headers": self.headers(app.APP_COOKIE_NAME + "=" + token),
                "actual_tls": True,
                **overrides,
            }
        )

    def test_host_guard_and_fixed_private_endpoint(self):
        with (
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "1"}),
            self.assertRaisesRegex(ValueError, "host-only"),
        ):
            self.app_ingress()
        for endpoint in ("https://127.0.0.1:43100", "http://localhost:43100"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                self.app_ingress(endpoint)
        self.pair.assert_not_called()

    def test_confirmation_requires_exact_post_and_matching_bootstrap_not_app_cookie(
        self,
    ):
        ticket = self.ticket()
        for options in (
            {"method": "GET"},
            {"target": app.CONFIRM_PATH + "?ticket=" + ticket},
            {"target": "/auth/connect/" + ticket},
            {"actual_tls": False},
            {"browser": fixture.OTHER_BROWSER},
            {"headers": self.headers(app.APP_COOKIE_NAME + "=" + fixture.BROWSER)},
            {
                "headers": [
                    ("Host", self.authority),
                    ("Origin", self.origins.account.value),
                    ("Cookie", "__Host-ocbox-bootstrap=" + fixture.BROWSER),
                ]
            },
        ):
            with (
                self.subTest(keys=tuple(options)),
                self.assertRaises(app.AppRequestError),
            ):
                self.confirm(ticket, **options)
        self.pair.assert_not_called()
        self.bootstrap.return_value = None
        with self.assertRaises(app.AppRequestError):
            self.confirm(ticket)
        self.bootstrap.assert_called_with(fixture.BROWSER)
        self.pair.assert_not_called()
        self.bootstrap.return_value = lambda: True
        token = self.confirm(self.ticket())
        self.assertRegex(token, r"\A[A-Za-z0-9_-]{43}\Z")
        self.assertNotEqual(token, fixture.BROWSER)
        self.assertNotIn(token, NATIVE)
        self.assertEqual(app.APP_COOKIE_NAME, "__Host-ocbox-app")
        with self.assertRaises(app.AppRequestError):
            self.confirm(ticket)
        self.pair.assert_called_once_with(self.binding, self.origins.workspace)

    def test_metadata_dispatch_returns_only_permit_and_rebuilds_upstream_headers(self):
        token = self.sign_in_browser()
        fields = self.headers(
            "; ".join(
                (
                    "opencode_session_" + str(self.origins.workspace_port) + "=browser",
                    app.APP_COOKIE_NAME + "=" + token,
                    "__Host-ocbox-bootstrap=" + fixture.BROWSER,
                    "arbitrary=browser",
                )
            )
        ) + [("X-Extra", "not forwarded")]
        run = Mock(return_value=self.permit)
        self.owner.reset_mock()
        for route in ("/api/info", "/api/session/active"):
            self.assertIs(
                self.dispatch(token, target=route, headers=fields, run=run), self.permit
            )
            endpoint, method, target, headers, use = run.call_args.args
            self.assertEqual((endpoint, method, target), (ENDPOINT, "GET", route))
            self.assertEqual(
                headers,
                (
                    ("Host", self.authority),
                    ("Origin", self.origin),
                    (
                        "Cookie",
                        f"opencode_session_{self.origins.workspace_port}={NATIVE}",
                    ),
                ),
            )
            self.assertFalse(use.cancelled)
        self.assertTrue(self.owner.called)
        self.assertTrue(
            all(call.args == (self.binding,) for call in self.owner.call_args_list)
        )

    def test_passes_browser_get_without_origin_but_never_raw_callback_data(self):
        token = self.sign_in_browser()
        headers = [
            ("Host", self.authority),
            ("Cookie", app.APP_COOKIE_NAME + "=" + token),
            ("Sec-Fetch-Site", "same-origin"),
            ("Sec-Fetch-Mode", "cors"),
            ("Sec-Fetch-Dest", "empty"),
        ]
        self.assertIs(self.dispatch(token, headers=headers), self.permit)
        for data in (
            None,
            NATIVE,
            {"Set-Cookie": NATIVE},
            b"unfiltered upstream bytes",
        ):
            with self.subTest(kind=type(data)), self.assertRaises(app.AppRequestError):
                self.dispatch(token, headers=headers, run=Mock(return_value=data))
        self.assertIs(
            self.dispatch(token, run=lambda *_: app.MetadataPermit.UNAVAILABLE),
            app.MetadataPermit.UNAVAILABLE,
        )

    def test_app_framing_refuses_before_consuming_ticket_or_revoking_session(self):
        ticket = self.ticket()
        boot = self.headers("__Host-ocbox-bootstrap=" + fixture.BROWSER)
        size = ("Content-Length", str(len(ticket)))
        for fields, body in (
            (boot, ticket.encode("ascii")),
            (boot + [size], b"\xff" * len(ticket)),
            (boot + [size], b"incomplete"),
            (boot + [size, ("Content-Length", "0")], ticket.encode("ascii")),
            (boot + [size, ("Expect", "100-continue")], ticket.encode("ascii")),
        ):
            with self.assertRaises(app.AppRequestError):
                self.confirm(ticket, headers=fields, body=body)
        self.pair.assert_not_called()
        token = self.confirm(ticket)
        headers = self.headers(app.APP_COOKIE_NAME + "=" + token)
        for framing in (
            ("Content-Length", "1"),
            ("Content-Length", "00"),
            ("Transfer-Encoding", "chunked"),
            ("Content-Encoding", "gzip"),
            ("Connection", "close, content-length"),
        ):
            with self.subTest(framing=framing):
                with self.assertRaises(app.AppRequestError):
                    self.dispatch(token, headers=headers + [framing], run=self.fail)
                with self.assertRaises(app.AppRequestError):
                    self.logout(token, headers=headers + [framing])
        with self.assertRaises(app.AppRequestError):
            self.logout(token, body=b"unexpected")
        self.assertTrue(self.logout(token))

    def test_two_valid_handoffs_cannot_confirm_one_bootstrap(self):
        _, browser, _ = self.paired_bootstrap()
        first, second = self.ticket(browser), self.ticket(browser)
        native = self.pair.return_value
        entered, release = Event(), Event()

        def pair(*_):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("Fixture pairing timed out")
            return native

        self.pair.side_effect = pair
        with ThreadPoolExecutor(max_workers=1) as pool:
            confirmation = pool.submit(self.confirm, first, browser)
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaises(app.AppRequestError):
                    self.confirm(second, browser)
            finally:
                release.set()
            token = confirmation.result(timeout=2)
        self.pair.assert_called_once()
        self.assertIs(self.dispatch(token), self.permit)

    def test_expiry_during_native_pair_never_mints_app_token(self):
        _, browser, _ = self.paired_bootstrap(ttl=1)
        handoff = self.ticket(browser)
        native = self.pair.return_value

        def pair(*_):
            self.now += 1
            return native

        self.pair.side_effect = pair
        with patch.object(
            self.sign_in.admission, "issue", wraps=self.sign_in.admission.issue
        ) as issue:
            with self.assertRaises(app.AppRequestError):
                self.confirm(handoff, browser)
            issue.assert_not_called()
        self.pair.assert_called_once()

    def test_retirement_during_issue_revokes_unpublished_app_token(self):
        table, browser, cookie = self.paired_bootstrap()
        handoff = self.ticket(browser)
        delegate = self.sign_in.admission.issue
        minted = []

        def issue(*args, **kwargs):
            token = delegate(*args, **kwargs)
            minted.append(token)
            self.assertTrue(
                table.retire(
                    self.binding,
                    auth_origin=self.origins.account.value,
                    account_nonce=cookie,
                )
            )
            return token

        with (
            patch.object(self.sign_in.admission, "issue", side_effect=issue),
            self.assertRaises(app.AppRequestError),
        ):
            self.confirm(handoff, browser)
        self.assertEqual(len(minted), 1)
        self.assertIsNotNone(minted[0])
        self.assertIsNone(self.sign_in.admission.lookup(self.binding, minted[0]))

    def test_exact_routes_and_origin_refuse_auth_controls_and_ambiguous_requests(self):
        token = self.sign_in_browser()
        self.owner.reset_mock()
        run = Mock()
        fields = self.headers(app.APP_COOKIE_NAME + "=" + token)
        alternates = (
            {"method": method}
            for method in ("HEAD", "POST", "get", "OPTIONS", "DELETE")
        )
        targets = (
            "/",
            "/api/pair",
            "/api/auth",
            "/auth",
            "/auth/connect/synthetic",
            "/auth/logout",
            app.CONFIRM_PATH,
            app.LOGOUT_PATH,
            "/api/info?",
            "/api/info/",
            "/api/info#",
            "/api//info",
            "/api/./info",
            "/api/pair/../info",
            "/api/%69nfo",
            "//api/info",
            self.origin + "/api/info",
        )
        envelopes = (
            {"actual_tls": False},
            {"actual_tls": 1},
            {"headers": [fields[0], fields[2], ("Sec-Fetch-Site", "none")]},
            {"headers": [fields[0], fields[2], ("Origin", self.origins.account.value)]},
            {"headers": [("Host", self.origins.workspace.hostname), *fields[1:]]},
            {"headers": [*fields, ("origin", self.origin)]},
            {"headers": [*fields, ("Authorization", "Basic synthetic")]},
            {"headers": [*fields, ("X-Forwarded-Proto", "https")]},
            {"headers": fields[:2]},
            {"headers": [*fields, fields[2]]},
            {"headers": [*fields, ("Upgrade", "websocket"), ("Connection", "Upgrade")]},
        )
        for options in (
            *alternates,
            *({"target": target} for target in targets),
            *envelopes,
        ):
            with (
                self.subTest(options=options),
                self.assertRaises(app.AppRequestError) as refusal,
            ):
                self.dispatch(token, run=run, **options)
            self.assertIs(type(refusal.exception), app.AppRequestError)
        run.assert_not_called()
        self.owner.assert_not_called()

    def test_expired_handoff_and_session_cannot_be_replayed(self):
        ticket = self.ticket()
        self.now += 30
        with self.assertRaises(app.AppRequestError):
            self.confirm(ticket)
        self.pair.assert_not_called()
        token = self.sign_in_browser()
        self.now += 3599
        entered, cancelled = Event(), Event()

        def run(*_):
            entered.set()
            self.assertTrue(cancelled.wait(4))
            raise OSError("synthetic transport stopped")

        with ThreadPoolExecutor(max_workers=1) as pool:
            worker = pool.submit(self.dispatch, token, run=run, cancel=cancelled.set)
            self.assertTrue(entered.wait(2))
            self.now += 1
            self.ingress.expire()
            self.assertTrue(cancelled.is_set())
            with self.assertRaises(app.StaleAppSessionError):
                worker.result(timeout=2)
        with self.assertRaises(app.StaleAppSessionError):
            self.dispatch(token, run=self.fail)
        with self.assertRaises(app.StaleAppSessionError):
            self.logout(token)
        self.assertIs(self.dispatch(self.sign_in_browser()), self.permit)

    def test_logout_gates_before_revocation_and_scrubs_callback_errors(self):
        token = self.sign_in_browser()
        for options in (
            {"method": "GET"},
            {"target": "/auth/logout"},
            {"target": app.LOGOUT_PATH + "/"},
            {"actual_tls": False},
            {"timeout": float("inf")},
        ):
            with self.subTest(options=options), self.assertRaises(app.AppRequestError):
                self.logout(token, **options)
        failing = Mock(side_effect=RuntimeError(NATIVE))
        with self.assertRaises(app.AppRequestError) as refusal:
            self.dispatch(token, run=failing)
        self.assertNotIn(NATIVE, str(refusal.exception))
        self.assertIsNone(refusal.exception.__context__)
        self.assertIs(self.dispatch(token), self.permit)
        self.assertTrue(self.logout(token, timeout=0))

    def test_logout_cancels_and_waits_only_for_the_selected_browser(self):
        # Two independent sign-ins of the same principal must not share logout.
        first = self.sign_in_browser()
        second = self.sign_in_browser(fixture.OTHER_BROWSER)
        self.assertNotEqual(first, second)
        entered = [Event(), Event()]
        cancelled = [Event(), Event()]
        finish = [Event(), Event()]

        def run(i, use):
            entered[i].set()
            self.assertTrue(finish[i].wait(5))
            self.assertEqual(use.cancelled, i == 0)
            return self.permit

        with ThreadPoolExecutor(max_workers=3) as pool:
            try:
                workers = [
                    pool.submit(
                        self.dispatch,
                        token,
                        cancel=cancelled[i].set,
                        run=lambda *args, i=i: run(i, args[-1]),
                    )
                    for i, token in enumerate((first, second))
                ]
                self.assertTrue(all(event.wait(2) for event in entered))
                revocation = pool.submit(self.logout, first, timeout=4)
                self.assertTrue(cancelled[0].wait(2))
                self.assertFalse(cancelled[1].is_set())
                self.assertFalse(revocation.done())
                with self.assertRaises(app.StaleAppSessionError):
                    self.dispatch(first, run=self.fail)
                finish[0].set()
                with self.assertRaises(app.StaleAppSessionError):
                    workers[0].result(timeout=2)
                self.assertTrue(revocation.result(timeout=2))
                self.assertFalse(workers[1].done())
                finish[1].set()
                self.assertIs(workers[1].result(timeout=2), self.permit)
            finally:
                for event in finish:
                    event.set()
        self.assertFalse(cancelled[1].is_set())
        self.assertIs(self.dispatch(second), self.permit)

    def test_cancellation_is_installed_before_private_cookie_resolution(self):
        token = self.sign_in_browser()
        entered, cancelled, finish = Event(), Event(), Event()
        run = Mock()
        cookie_method = web_native_pair.NativeCookie.upstream_cookie

        def resolve(cookie, **kwargs):
            entered.set()
            self.assertTrue(finish.wait(4))
            return cookie_method(cookie, **kwargs)

        with (
            patch.object(web_native_pair.NativeCookie, "upstream_cookie", resolve),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            worker = pool.submit(self.dispatch, token, cancel=cancelled.set, run=run)
            try:
                self.assertTrue(entered.wait(2))
                self.assertFalse(self.logout(token, timeout=0))
                self.assertTrue(cancelled.is_set())
            finally:
                finish.set()
            with self.assertRaises(app.StaleAppSessionError):
                worker.result(timeout=2)
        run.assert_not_called()
        with self.assertRaises(app.StaleAppSessionError):
            self.logout(token)

    def test_owner_loss_fences_app_and_cancels_active_work_without_retry(self):
        token = self.sign_in_browser()
        entered, cancelled, finish = Event(), Event(), Event()

        def run(*_):
            entered.set()
            self.assertTrue(finish.wait(4))
            return self.permit

        with ThreadPoolExecutor(max_workers=1) as pool:
            worker = pool.submit(self.dispatch, token, cancel=cancelled.set, run=run)
            try:
                self.assertTrue(entered.wait(2))
                self.owner.side_effect = OSError("private owner error")
                with self.assertRaisesRegex(
                    app.StaleAppStateError, "no longer current"
                ):
                    self.ingress.expire()
                self.assertTrue(cancelled.is_set())
            finally:
                finish.set()
            with self.assertRaises(app.StaleAppStateError):
                worker.result(timeout=2)
        self.owner.side_effect = None
        with self.assertRaises(app.StaleAppStateError):
            self.dispatch(token, run=self.fail)
        self.pair.assert_called_once()

    def test_native_cookie_scope_cannot_be_rebound_or_replaced_by_browser_header(self):
        token = self.sign_in_browser()
        different_endpoint = self.app_ingress("http://127.0.0.1:43101")
        fields = self.headers(
            app.APP_COOKIE_NAME
            + "="
            + token
            + f"; opencode_session_{self.origins.workspace_port}={NATIVE}"
        )
        run = Mock()
        with self.assertRaises(app.StaleAppStateError):
            different_endpoint.dispatch(
                method="GET",
                target="/api/info",
                headers=fields,
                actual_tls=True,
                cancel=Mock(),
                run=run,
            )
        with self.assertRaises(app.StaleAppStateError):
            self.dispatch(token, run=self.fail)
        run.assert_not_called()
        self.pair.assert_called_once()

    def test_private_cookie_performs_its_own_live_lease_check(self):
        native_owner = Mock(return_value=True)
        self.pair.return_value = web_native_pair.NativeCookie(
            self.binding,
            ENDPOINT,
            self.authority,
            NATIVE,
            version="2.3.2",
            owner_current=native_owner,
        )
        token = self.sign_in_browser()
        self.assertIs(self.dispatch(token), self.permit)
        native_owner.assert_called_once_with(self.binding)
        native_owner.return_value = False
        run = Mock()
        with self.assertRaises(app.StaleAppStateError):
            self.dispatch(token, run=run, cancel=Mock())
        run.assert_not_called()
        self.pair.assert_called_once()


if __name__ == "__main__":
    unittest.main()
