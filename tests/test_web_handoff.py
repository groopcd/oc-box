"""Offline one-use handoffs with synthetic principals and no listener."""

import base64
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial
from threading import Barrier
from unittest.mock import patch

from oc_box import web_handoff
from oc_box.web_admission import BrowserAdmission, WebBinding


def nonce(marker):
    return base64.urlsafe_b64encode(bytes([marker]) * 32).rstrip(b"=").decode("ascii")


class WebHandoffTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        clock = patch.object(web_handoff, "monotonic", new=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.binding = WebBinding(
            workspace_id="workspace-a",
            owner_generation="owner-a",
            origin="https://app.example.test",
        )
        self.auth_origin = "https://auth.example.test:8443"
        self.nonce = nonce(1)
        self.other_nonce = nonce(2)
        self.scope = {
            "binding": self.binding,
            "auth_origin": self.auth_origin,
            "browser_nonce": self.nonce,
        }

    def handoff(self, **options):
        registry = web_handoff.WebHandoff(
            **({"binding": self.binding, "auth_origin": self.auth_origin} | options)
        )
        self.addCleanup(registry.close)
        return registry

    def issue(self, registry, **scope):
        return registry.issue(**(self.scope | {"principal": "alice"} | scope))

    def mint(self, registry, **scope):
        token = self.issue(registry, **scope)
        self.assertIsInstance(token, web_handoff.HandoffToken)
        return token

    def consume(self, registry, token, **scope):
        return registry.consume(token=token, **(self.scope | scope))

    def revoke(self, registry, token, **scope):
        return registry.revoke(token=token, **(self.scope | scope))

    def race(self, *calls):
        start = Barrier(len(calls))

        def run(call):
            start.wait(timeout=5)
            return call()

        with ThreadPoolExecutor(max_workers=len(calls)) as pool:
            futures = [pool.submit(run, call) for call in calls]
            return [future.result(timeout=10) for future in futures]

    def test_consume_once_without_implicitly_issuing_browser_admission(self):
        registry = self.handoff()
        admission = BrowserAdmission(self.binding)
        self.addCleanup(admission.close)
        token = self.mint(registry)
        self.assertEqual(len(token.value), 54)
        self.assertFalse(token.value in repr(token))
        self.assertFalse(token.value in str(token))
        self.assertIsNone(admission.lookup(self.binding, token.value))
        session = admission.issue(self.binding, principal="bob")
        self.assertIsNotNone(session)
        self.assertIsNone(self.consume(registry, session))
        principal = self.consume(registry, token.value, binding=replace(self.binding))
        self.assertEqual(principal, "alice")
        self.assertIsNone(self.consume(registry, token.value))
        browser_token = admission.issue(self.binding, principal=principal)
        self.assertEqual(
            admission.lookup(self.binding, browser_token).principal, "alice"
        )

    def test_wrong_scope_cannot_issue_consume_or_revoke(self):
        registry = self.handoff()
        token = self.mint(registry).value
        for scope in (
            {"binding": replace(self.binding, workspace_id="workspace-b")},
            {"binding": replace(self.binding, owner_generation="owner-b")},
            {"binding": replace(self.binding, origin="https://app.example.test:8443")},
            {"binding": None},
            {"auth_origin": "https://auth.example.test"},
            {"auth_origin": "https://AUTH.example.test:8443"},
            {"auth_origin": "https://app.example.test"},
            {"auth_origin": None},
            {"browser_nonce": "A" * 42 + "B"},  # Noncanonical pad bits.
            {"browser_nonce": self.nonce.encode("ascii")},
            {"browser_nonce": "x" * 4096},
        ):
            self.assertIsNone(self.issue(registry, **scope))
            self.assertIsNone(self.consume(registry, token, **scope))
            self.assertFalse(self.revoke(registry, token, **scope))
        self.assertIsNone(self.consume(registry, token, browser_nonce=self.other_nonce))
        self.assertFalse(self.revoke(registry, token, browser_nonce=self.other_nonce))
        self.assertEqual(self.consume(registry, token), "alice")

    def test_downstream_refusal_does_not_allow_retry(self):
        registry = self.handoff()
        admission = BrowserAdmission(self.binding, max_sessions=1)
        self.addCleanup(admission.close)
        existing = admission.issue(self.binding, principal="bob")
        session = admission.lookup(self.binding, existing)
        self.assertIsNotNone(session)
        token = self.mint(registry).value
        principal = self.consume(registry, token)
        self.assertEqual(principal, "alice")
        self.assertIsNone(admission.issue(self.binding, principal=principal))
        self.assertTrue(admission.revoke(self.binding, session))
        self.assertIsNone(self.consume(registry, token))

    def test_new_owner_registry_cannot_redeem_prior_owner_token(self):
        older = self.handoff()
        old_token = self.mint(older).value
        older.close()
        replacement = replace(self.binding, owner_generation="owner-b")
        newer = self.handoff(binding=replacement)
        new_token = self.mint(newer, binding=replacement).value
        self.assertIsNone(self.consume(newer, old_token, binding=replacement))
        self.assertEqual(self.consume(newer, new_token, binding=replacement), "alice")

    def test_malformed_token_and_bad_principal_are_generic_refusals(self):
        registry = self.handoff()
        token = self.mint(registry).value
        for value in (None, {}, "", 9, "a\nb", "\ud800", "x" * 129):
            self.assertIsNone(self.issue(registry, principal=value))
        for value in (
            None,
            {},
            token.encode("ascii"),
            token + "\n",
            token + "==",
            token[:-1] + "B",  # Noncanonical alias.
            "B" + token[1:],  # Well formed but unknown.
            "x" * 4096,
        ):
            self.assertIsNone(self.consume(registry, value))
            self.assertFalse(self.revoke(registry, value))
        self.assertEqual(self.consume(registry, token), "alice")

    def test_constructor_refuses_malformed_or_port_only_origin_and_unsafe_limits(self):
        for options in (
            {"binding": None},
            {"auth_origin": None},
            {"auth_origin": "http://auth.example.test"},
            {"auth_origin": "https://auth.example.test:443"},
            {"auth_origin": "https://AUTH.example.test"},
            {"auth_origin": "https://auth.example.test/never-render-request-data"},
            {"auth_origin": "https://app.example.test"},
            {"auth_origin": "https://app.example.test:8443"},
            {"ttl": 0},
            {"ttl": -1},
            {"ttl": True},
            {"ttl": float("nan")},
            {"ttl": float("inf")},
            {"ttl": 60.001},
            {"max_pending": True},
            {"max_pending": 0},
            {"max_pending": 1.0},
            {"max_pending": 257},
        ):
            with (
                self.subTest(option=options),
                self.assertRaisesRegex(
                    ValueError, r"\AInvalid web handoff configuration\.\Z"
                ),
            ):
                self.handoff(**options)

    def test_pending_capacity_does_not_evict_and_revoke_releases_one_slot(self):
        registry = self.handoff(max_pending=2)
        first = self.mint(registry).value
        second = self.mint(registry, browser_nonce=self.other_nonce).value
        self.assertIsNone(self.issue(registry, principal="bob"))
        self.assertTrue(self.revoke(registry, first))
        self.assertFalse(self.revoke(registry, first))
        third = self.mint(registry, principal="bob").value
        self.assertIsNone(self.consume(registry, first))
        self.assertEqual(
            self.consume(registry, second, browser_nonce=self.other_nonce), "alice"
        )
        self.assertEqual(self.consume(registry, third), "bob")

    def test_absolute_monotonic_expiry_at_boundary_and_idle_sweep(self):
        registry = self.handoff(ttl=10, max_pending=3)
        first = self.mint(registry).value
        swept = self.mint(registry).value
        self.now += 9
        later = self.mint(registry).value
        self.assertIsNone(self.consume(registry, first, browser_nonce=self.other_nonce))
        self.now += 1
        self.assertIsNone(self.consume(registry, first))
        registry.expire()
        self.assertEqual(len(registry._pending), 1)  # Idle state is actually dropped.
        self.assertFalse(self.revoke(registry, swept))
        self.assertEqual(self.consume(registry, later), "alice")

    def test_issue_recovers_expired_capacity_without_timer(self):
        registry = self.handoff(ttl=0.5, max_pending=1)
        first = self.mint(registry).value
        self.now += 0.25
        self.assertIsNone(self.issue(registry))
        self.now += 0.25
        second = self.mint(registry).value
        self.assertFalse(self.revoke(registry, first))
        self.assertEqual(self.consume(registry, second), "alice")

    def test_entropy_failure_refuses_and_repeated_entropy_does_not_reuse_token(self):
        registry = self.handoff(ttl=1, max_pending=1)
        with patch.object(web_handoff.secrets, "token_bytes", side_effect=OSError):
            self.assertIsNone(self.issue(registry))
        with patch.object(
            web_handoff.secrets, "token_bytes", return_value=b"x" * 32
        ) as rng:
            consumed = self.mint(registry).value
            self.assertEqual(self.consume(registry, consumed), "alice")
            revoked = self.mint(registry).value
            self.assertTrue(self.revoke(registry, revoked))
            expired = self.mint(registry).value
            self.now += 1
            last = self.mint(registry).value
        self.assertEqual([call.args for call in rng.call_args_list], [(32,)] * 4)
        self.assertEqual(len({consumed, revoked, expired, last}), 4)
        for old in (consumed, revoked, expired):
            self.assertIsNone(self.consume(registry, old))
        self.assertEqual(self.consume(registry, last), "alice")

    def test_competing_attempts_admit_only_one_rightful_caller(self):
        registry = self.handoff()
        token = self.mint(registry).value
        rightful = partial(self.consume, registry, token)
        wrong = (
            partial(self.consume, registry, token, browser_nonce=self.other_nonce),
            partial(self.consume, registry, token, auth_origin=self.binding.origin),
            partial(
                self.consume,
                registry,
                token,
                binding=replace(self.binding, owner_generation="owner-b"),
            ),
            partial(self.revoke, registry, token, browser_nonce=self.other_nonce),
        )
        self.assertEqual(self.race(*wrong), [None, None, None, False])
        results = self.race(*([rightful] * 8), *wrong)
        self.assertCountEqual(results[:8], ["alice", *([None] * 7)])
        self.assertEqual(results[8:], [None, None, None, False])
        self.assertIsNone(rightful())

    def test_concurrent_issue_cannot_exceed_capacity(self):
        registry = self.handoff(max_pending=3)
        results = self.race(*([partial(self.issue, registry)] * 12))
        tokens = [token.value for token in results if token is not None]
        self.assertEqual(len(tokens), 3)
        self.assertEqual(len(set(tokens)), 3)
        for token in tokens:
            self.assertEqual(self.consume(registry, token), "alice")

    def test_consume_and_revoke_are_mutually_exclusive(self):
        registry = self.handoff()
        token = self.mint(registry).value
        result = self.race(
            partial(self.consume, registry, token),
            partial(self.revoke, registry, token),
        )
        self.assertIn(result, (["alice", False], [None, True]))
        self.assertIsNone(self.consume(registry, token))
        self.assertFalse(self.revoke(registry, token))

    def test_close_racing_issue_or_consume_leaves_no_admissible_grants(self):
        registry = self.handoff()
        queried = self.mint(registry).value
        pending = self.mint(registry).value
        principal, issued, _ = self.race(
            partial(self.consume, registry, queried),
            partial(self.issue, registry),
            registry.close,
        )
        self.assertIn(principal, (None, "alice"))
        registry.close()
        registry.expire()
        self.assertIsNone(self.consume(registry, queried))
        self.assertIsNone(self.consume(registry, pending))
        self.assertFalse(self.revoke(registry, pending))
        if issued is not None:
            self.assertIsNone(self.consume(registry, issued.value))
        self.assertIsNone(self.issue(registry))


if __name__ == "__main__":
    unittest.main()
