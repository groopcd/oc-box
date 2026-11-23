"""Private composition with fixture PAKE and verified app admission.

This never opens a listener. External TLS, cookie, live owner, and upstream
authorization checks are responsibilities of the future ingress.
"""

import unittest
from contextlib import ExitStack
from dataclasses import replace
from unittest.mock import Mock

import test_web_pake_flow as fixture

from oc_box.web_handoff import WebHandoff


class NamedCompositionTests(unittest.TestCase):
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
            first_use = stack.enter_context(
                flow.admission.connection(
                    case.binding, first_session, cancel=cancel_first
                )
            )
            second_use = stack.enter_context(
                flow.admission.connection(
                    case.binding, second_session, cancel=cancel_second
                )
            )
            self.assertTrue(flow.admission.revoke(case.binding, first_session))
            self.assertTrue(first_use.cancelled)
            self.assertFalse(second_use.cancelled)
            cancel_first.assert_called_once_with()
            cancel_second.assert_not_called()
            with flow.admission.connection(
                case.binding, first_session, cancel=Mock()
            ) as stale:
                self.assertIsNone(stale)
            flow.close()
            self.assertTrue(second_use.cancelled)
            cancel_second.assert_called_once_with()
