"""Private composition with fixture PAKE and verified app admission.

This never opens a listener. External TLS, cookie, live owner, and upstream
authorization checks are responsibilities of the future ingress.
"""

import unittest
from contextlib import ExitStack
from dataclasses import replace
from unittest.mock import Mock, patch

import test_web_pake_flow as fixture

from oc_box import web_local_origins, web_named_local, web_native_pair
from oc_box.web_handoff import WebHandoff


class NamedCompositionTests(unittest.TestCase):
    @staticmethod
    def redeem(driver, ticket, nonce=fixture.BROWSER):
        return driver.redeem(
            ticket.value, browser_nonce=nonce, claim_browser=lambda _: lambda: True
        )

    def local_driver(self):
        case = fixture.PakeFlowTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        origins = web_local_origins.LocalOrigins()
        case.binding = replace(case.binding, origin=origins.workspace.value)
        flow = case.flow(auth_origin=origins.account.value)
        current = Mock(return_value=True)
        native = web_native_pair.NativeCookie(
            case.binding,
            "http://127.0.0.1:43100",
            origins.workspace.value.removeprefix("https://"),
            "a" * 10 + "." + "b" * 43,
            owner_current=current,
        )
        pair = Mock(return_value=native)
        driver = web_named_local.LocalNamedSignIn(
            origins=origins,
            binding=case.binding,
            flow=flow,
            owner_current=current,
            pair_native=pair,
        )
        self.addCleanup(driver.close)
        return case, origins, driver, current, pair, native

    @staticmethod
    def local_ticket(driver):
        challenge = driver.start(
            browser_nonce=fixture.BROWSER, username="in-scope", ke1=fixture.KE1
        )
        return driver.finish(
            browser_nonce=fixture.BROWSER,
            challenge_id=challenge.challenge_id,
            ke3=fixture.PROOF,
        )

    def test_private_composition_pairs_only_after_redemption_and_retains_native_cookie(
        self,
    ):
        case, origins, driver, _, pair, native = self.local_driver()
        ticket = self.local_ticket(driver)
        pair.assert_not_called()
        self.assertIsNone(self.redeem(driver, ticket, fixture.OTHER_BROWSER))
        pair.assert_not_called()
        lease = self.redeem(driver, ticket)
        self.assertIsInstance(lease, str)
        session = driver.admission.lookup(case.binding, lease)
        self.assertIs(session.native_cookie, native)
        self.assertEqual(session.principal, "in-scope")
        self.assertIsNone(self.redeem(driver, ticket))
        pair.assert_called_once_with(case.binding, origins.workspace)

    def test_owner_change_during_native_cookie_mint_refuses_and_consumes_ticket(self):
        case, _, driver, current, pair, native = self.local_driver()
        ticket = self.local_ticket(driver)

        def owner_lost(*_args):
            current.return_value = False
            return native

        pair.side_effect = owner_lost
        self.assertIsNone(self.redeem(driver, ticket))
        current.return_value = True
        self.assertIsNone(self.redeem(driver, ticket))
        self.assertIsNone(driver.admission.issue(case.binding, principal="synthetic"))
        pair.assert_called_once()

    def test_cleanup_fence_wins_against_a_delayed_admission_after_pairing(self):
        case, _, driver, _, pair, _ = self.local_driver()
        ticket = self.local_ticket(driver)
        delegate = driver.admission.issue

        def delayed_issue(*args, **kwargs):
            with patch.object(
                case.backend, "start", side_effect=fixture.pake.VerifierCleanupError
            ):
                self.assertIsNone(
                    driver.start(
                        browser_nonce=fixture.BROWSER,
                        username="in-scope",
                        ke1=fixture.KE1,
                    )
                )
            return delegate(*args, **kwargs)

        with patch.object(driver.admission, "issue", side_effect=delayed_issue):
            self.assertIsNone(self.redeem(driver, ticket))
        self.assertIsNone(driver.admission.issue(case.binding, principal="synthetic"))
        pair.assert_called_once()

    def test_app_session_exists_only_after_same_browser_handoff_and_revokes_one(self):
        case = fixture.PakeFlowTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        flow = case.flow()
        handoff = WebHandoff(case.binding, auth_origin=fixture.AUTH_ORIGIN)
        self.addCleanup(flow.close)
        self.addCleanup(handoff.close)

        def proof(browser):
            challenge = case.start(flow, browser_nonce=browser)
            return case.finish(flow, challenge, browser_nonce=browser)

        def grant(principal, browser):
            return handoff.issue(
                case.binding,
                auth_origin=fixture.AUTH_ORIGIN,
                browser_nonce=browser,
                principal=principal,
            )

        def redeem(ticket, browser, *, binding=None):
            binding = binding or case.binding
            return handoff.consume(
                binding,
                ticket.value,
                auth_origin=fixture.AUTH_ORIGIN,
                browser_nonce=browser,
            )

        # A valid proof results in a principal, not an app session/cookie.
        first = grant(proof(fixture.BROWSER), fixture.BROWSER)
        self.assertIsNone(redeem(first, fixture.OTHER_BROWSER))
        self.assertIsNone(
            redeem(
                first,
                fixture.BROWSER,
                binding=replace(case.binding, owner_generation="new"),
            )
        )
        self.assertIsNone(flow.admission.lookup(case.binding, first.value))
        principal = redeem(first, fixture.BROWSER)
        self.assertEqual(principal, "in-scope")
        self.assertIsNone(redeem(first, fixture.BROWSER))
        first_session = flow.admission.lookup(
            case.binding, flow.admission.issue(case.binding, principal=principal)
        )
        second = grant(proof(fixture.OTHER_BROWSER), fixture.OTHER_BROWSER)
        principal = redeem(second, fixture.OTHER_BROWSER)
        second_session = flow.admission.lookup(
            case.binding, flow.admission.issue(case.binding, principal=principal)
        )

        with ExitStack() as stack:
            cancel_first, cancel_second = Mock(), Mock()
            second_use = stack.enter_context(
                flow.admission.connection(
                    case.binding, second_session, cancel=cancel_second
                )
            )
            with flow.admission.connection(
                case.binding, first_session, cancel=cancel_first
            ) as first_use:
                logout = flow.admission.begin_revocation(case.binding, first_session)
                self.assertTrue(logout.revoked)
                self.assertTrue(first_use.cancelled)
                self.assertFalse(second_use.cancelled)
                self.assertFalse(logout.wait_drained(0))
            self.assertTrue(logout.wait_drained(0))
            cancel_first.assert_called_once_with()
            cancel_second.assert_not_called()
            with flow.admission.connection(
                case.binding, first_session, cancel=Mock()
            ) as stale:
                self.assertIsNone(stale)
            flow.close()
            self.assertTrue(second_use.cancelled)
            cancel_second.assert_called_once_with()
