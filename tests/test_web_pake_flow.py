"""Offline orchestration tests; the synthetic adapter is not a PAKE."""

import base64
import threading
import unittest
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import patch

from oc_box import web_accounts as accounts
from oc_box import web_admission as admission
from oc_box import web_pake_flow as pake

WORKSPACE = "a" * 32
OTHER_WORKSPACE = "b" * 32
BROWSER = "A" * 43
OTHER_BROWSER = "B" * 42 + "E"
AUTH_ORIGIN = "https://account.example.test"

SETUP = b"synthetic server setup"
REAL = b"synthetic registration"
OTHER = b"out-of-scope registration"
KE1 = "+/8="
KE2 = "AQI="
PROOF = "AwQ="
WRONG = "AAE="
SIZES = pake.MessageSizes(ke1=2, ke2=2, ke3=2)


class SyntheticBackend:
    def __init__(self):
        self.starts = []
        self.finishes = []
        self.discards = []

    def start(self, **request):
        self.starts.append(request)
        return base64.b64decode(KE2), self.exchange(base64.b64decode(PROOF))

    def exchange(self, expected):
        return SyntheticExchange(self, expected)

    def finish(self, *, state, ke3):
        self.finishes.append((state, ke3))
        return state == ke3


class SyntheticExchange:
    def __init__(self, backend, expected):
        self.backend, self.expected = backend, expected
        self.discarded = False

    def finish(self, ke3):
        return self.backend.finish(state=self.expected, ke3=ke3)

    def discard(self):
        if not self.discarded:
            self.discarded = True
            self.backend.discards.append(self)


class PakeFlowTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        for module in (pake, admission):
            clock = patch.object(module, "monotonic", side_effect=lambda: self.now)
            clock.start()
            self.addCleanup(clock.stop)
        self.binding = admission.WebBinding(
            WORKSPACE, "synthetic-owner", "https://workspace.example.test"
        )
        self.store = accounts.AccountStore(
            SETUP,
            (
                accounts.Account("in-scope", (WORKSPACE,), REAL),
                accounts.Account("out-of-scope", (OTHER_WORKSPACE,), OTHER),
            ),
        )
        self.backend = SyntheticBackend()

    def flow(self, **options):
        return pake.PakeFlow(
            self.binding,
            **{
                "store": self.store,
                "backend": self.backend,
                "sizes": SIZES,
                "auth_origin": AUTH_ORIGIN,
                **options,
            },
        )

    def start(self, flow, **params):
        return flow.start(
            **{
                "binding": self.binding,
                "browser_nonce": BROWSER,
                "username": "in-scope",
                "ke1": KE1,
                **params,
            }
        )

    def finish(self, flow, challenge, **params):
        return flow.finish(
            **{
                "binding": self.binding,
                "browser_nonce": BROWSER,
                "challenge_id": challenge.challenge_id,
                "ke3": PROOF,
                **params,
            }
        )

    def test_verified_finish_returns_principal_but_does_not_issue_browser_session(self):
        flow = self.flow()
        with patch.object(flow.admission, "issue", wraps=flow.admission.issue) as issue:
            challenge = self.start(flow)
            self.assertEqual(challenge.ke2, KE2)
            self.assertRegex(challenge.challenge_id, r"\A[A-Za-z0-9_-]{43}\Z")
            self.assertNotIn(challenge.challenge_id, repr(challenge))
            self.assertNotIn(challenge.ke2, repr(challenge))
            context = pake._context(
                AUTH_ORIGIN, self.binding.origin, challenge.challenge_id
            )
            self.assertEqual(base64.b64decode(challenge.context), context)
            self.assertEqual(
                self.backend.starts,
                [
                    {
                        "setup": SETUP,
                        "credential_identifier": b"in-scope",
                        "context": context,
                        "registration": REAL,
                        "ke1": b"\xfb\xff",
                        "deadline": 130.0,
                    }
                ],
            )
            self.assertEqual(self.backend.finishes, [])
            issue.assert_not_called()
            self.assertEqual(self.finish(flow, challenge), "in-scope")
            self.assertIsNone(self.finish(flow, challenge))
            self.assertEqual(self.backend.finishes, [(b"\x03\x04", b"\x03\x04")])
            issue.assert_not_called()
            flow.close()

    def test_scope_and_unknown_run_same_dummy_branch_and_never_issue(self):
        flow = self.flow()
        with patch.object(flow.admission, "issue") as issue:
            for username in ("unknown", "out-of-scope"):
                with self.subTest(username=username):
                    challenge = self.start(flow, username=username)
                    self.assertEqual(challenge.ke2, KE2)
                    call = self.backend.starts[-1]
                    self.assertIsNone(call["registration"])
                    self.assertEqual(call["credential_identifier"], username.encode())
                    self.assertEqual(call["setup"], SETUP)
                    # This synthetic backend even says True for dummy state.
                    self.assertIsNone(self.finish(flow, challenge))
                    self.assertIsNone(self.finish(flow, challenge))
            real = self.start(flow)
            self.assertEqual(real.ke2, KE2)
            self.assertIsNone(self.finish(flow, real, ke3=WRONG))
            self.assertIsNone(self.finish(flow, real))
            self.assertEqual(
                self.backend.finishes,
                [
                    (b"\x03\x04", b"\x03\x04"),
                    (b"\x03\x04", b"\x03\x04"),
                    (b"\x03\x04", b"\x00\x01"),
                ],
            )
            issue.assert_not_called()

    def test_finish_needs_exact_owner_workspace_origin_browser_and_transaction(self):
        flow = self.flow()
        challenge = self.start(flow)
        for binding in (
            replace(self.binding, workspace_id=OTHER_WORKSPACE),
            replace(self.binding, owner_generation="other-owner"),
            replace(self.binding, origin="https://workspace.example.test:9443"),
            None,
        ):
            with self.subTest(binding=binding):
                self.assertIsNone(self.start(flow, binding=binding))
                self.assertIsNone(self.finish(flow, challenge, binding=binding))
        self.assertIsNone(self.finish(flow, challenge, browser_nonce=OTHER_BROWSER))
        self.assertIsNone(self.finish(flow, challenge, challenge_id=OTHER_BROWSER))
        self.assertIsNone(self.finish(self.flow(), challenge))
        self.assertEqual(len(self.backend.starts), 1)
        self.assertEqual(self.backend.finishes, [])
        self.assertIsNotNone(
            self.finish(flow, challenge, binding=replace(self.binding))
        )

    def test_same_browser_transactions_do_not_share_proof_state(self):
        flow = self.flow()
        with patch.object(
            self.backend,
            "start",
            side_effect=[
                (b"AB", self.backend.exchange(b"\x03\x04")),
                (b"AB", self.backend.exchange(b"CD")),
            ],
        ):
            first = self.start(flow)
            second = self.start(flow)
        self.assertNotEqual(first.challenge_id, second.challenge_id)
        self.assertNotEqual(first.context, second.context)
        self.assertIsNone(self.finish(flow, second))
        self.assertIsNone(self.finish(flow, second, ke3="Q0Q="))
        self.assertIsNotNone(self.finish(flow, first))
        self.assertEqual(
            self.backend.finishes,
            [
                (b"CD", b"\x03\x04"),
                (b"\x03\x04", b"\x03\x04"),
            ],
        )

    def test_wire_requires_canonical_base64_and_exact_suite_lengths(self):
        flow = self.flow()
        for bad in (
            None,
            b"+/8=",
            "",
            "–/8=",
            "+/8",
            "+/8=\n",
            "+/9=",
            "-_8=",
            "+w==",
            "+///",
            "A" * 10000,
        ):
            with self.subTest(ke1=bad):
                self.assertIsNone(self.start(flow, ke1=bad))
        for bad in (None, b"in-scope", "In-scope", "in-scope\n", "a" * 33, "é"):
            with self.subTest(username=bad):
                self.assertIsNone(self.start(flow, username=bad))
        for bad in (
            None,
            BROWSER.encode(),
            BROWSER + "\n",
            "A" * 42 + "B",
            "A" * 42 + "/",
        ):
            with self.subTest(nonce=bad):
                self.assertIsNone(self.start(flow, browser_nonce=bad))
        self.assertEqual(self.backend.starts, [])
        canonical = self.start(flow)
        # A permissive decoder treats this as the same transaction ID.
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        ident = canonical.challenge_id
        id_alias = ident[:-1] + alphabet[alphabet.index(ident[-1]) + 1]
        self.assertIsNone(self.finish(flow, canonical, challenge_id=id_alias))
        for bad in (
            None,
            b"AwQ=",
            "AwQ",
            "AwR=",
            "Aw==",
            "AwQF",
            "AwQ=\n",
            "A" * 10000,
        ):
            with self.subTest(ke3=bad):
                challenge = self.start(flow)
                self.assertIsNotNone(challenge)
                self.assertIsNone(self.finish(flow, challenge, ke3=bad))
                self.assertIsNone(self.finish(flow, challenge))
        self.assertEqual(self.backend.finishes, [])
        self.assertIsNotNone(self.finish(flow, canonical))

    def test_capacity_does_not_evict_and_ids_cannot_replace_pending_state(self):
        flow = self.flow(max_challenges=2)
        with patch.object(
            pake.secrets, "token_urlsafe", side_effect=[BROWSER, BROWSER, OTHER_BROWSER]
        ):
            first = self.start(flow)
            self.assertIsNone(self.start(flow))
            second = self.start(flow)
            self.assertIsNone(self.start(flow))
        self.assertEqual(len(self.backend.starts), 2)
        self.assertIsNotNone(self.finish(flow, first))
        self.assertIsNotNone(self.finish(flow, second))
        self.assertIsNotNone(self.start(flow))

    def test_absolute_expiry_drops_idle_pending_state_without_verifier(self):
        flow = self.flow(challenge_ttl=10, max_challenges=1)
        first = self.start(flow)
        self.now += 9
        self.assertIsNone(self.start(flow))
        self.now += 1
        flow.expire()
        self.assertIsNone(self.finish(flow, first))
        second = self.start(flow)
        self.assertIsNotNone(second)
        self.now += 10
        self.assertIsNone(self.finish(flow, second))  # No sweep needed.
        self.assertEqual(self.backend.finishes, [])
        self.assertIsNotNone(self.start(flow))

    def test_rolling_attempt_budget_counts_real_dummy_and_failed_start(self):
        flow = self.flow(max_challenges=1)
        with patch.object(self.backend, "start", side_effect=RuntimeError("sensitive")):
            self.assertIsNone(self.start(flow))
        for i in range(11):
            challenge = self.start(
                flow, username=("in-scope", "unknown", "out-of-scope")[i % 3]
            )
            self.assertIsNotNone(challenge)
            self.assertIsNone(self.finish(flow, challenge, ke3=WRONG))
        self.assertIsNone(self.start(flow))
        self.assertEqual(len(self.backend.starts), 11)
        self.now += 60
        self.assertIsNotNone(self.start(flow))

    def test_backend_outputs_are_bounded_and_fail_closed(self):
        flow = self.flow(max_challenges=1)
        bad_response = self.backend.exchange(b"CD")
        for result in (
            None,
            (b"AB",),
            [b"AB", b"state"],
            (b"ABC", b"state"),
            (bytearray(b"AB"), b"state"),
            (b"AB", b""),
            (b"AB", "state"),
            (b"ABC", bad_response),
        ):
            with (
                self.subTest(kind=type(result)),
                patch.object(self.backend, "start", return_value=result),
            ):
                self.assertIsNone(self.start(flow))
        self.assertTrue(bad_response.discarded)
        for output in ({"return_value": 1}, {"side_effect": RuntimeError("sensitive")}):
            with patch.object(self.backend, "finish", **output) as verify:
                challenge = self.start(flow)
                self.assertIsNotNone(challenge)
                self.assertIsNone(self.finish(flow, challenge))
                self.assertIsNone(self.finish(flow, challenge))
                verify.assert_called_once()
        self.assertIsNotNone(self.finish(flow, self.start(flow)))

    def test_cancel_all_paths_retire_exchange_handles_and_cleanup_failure_revokes(self):
        flow = self.flow(max_challenges=1, challenge_ttl=2)
        previous = flow.admission.issue(self.binding, principal="existing-session")
        self.assertIsNotNone(flow.admission.lookup(self.binding, previous))
        first = self.start(flow)
        self.assertIsNone(self.finish(flow, first, ke3="bad"))
        self.assertEqual(len(self.backend.discards), 1)
        self.start(flow)
        self.now += 2
        flow.expire()
        self.assertEqual(len(self.backend.discards), 2)
        failing = self.backend.exchange(base64.b64decode(PROOF))
        with patch.object(self.backend, "start", return_value=(b"AB", failing)):
            challenge = self.start(flow)
        with patch.object(failing, "discard", side_effect=OSError("private fixture")):
            self.assertIsNone(self.finish(flow, challenge))
        self.assertTrue(flow.cleanup_failed)
        self.assertIsNone(flow.admission.lookup(self.binding, previous))
        self.assertIsNone(self.start(flow))

    def test_close_calls_discard_for_running_finish_and_late_start(self):
        flow = self.flow()
        challenge = self.start(flow)
        with self.blocked_call("finish", lambda: self.finish(flow, challenge)) as (
            _,
            results,
        ):
            flow.close()
            self.assertEqual(len(self.backend.discards), 1)
        self.assertEqual(results, [None])
        self.assertEqual(len(self.backend.discards), 1)
        flow = self.flow()
        with self.blocked_call("start", lambda: self.start(flow)) as (_, results):
            flow.close()
        self.assertEqual(results, [None])
        self.assertEqual(len(self.backend.discards), 2)

    @contextmanager
    def blocked_call(self, method, operation):
        entered, release = threading.Event(), threading.Event()
        results = []
        callback = getattr(self.backend, method)

        def blocking(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise TimeoutError
            return callback(*args, **kwargs)

        worker = threading.Thread(
            target=lambda: results.append(operation()), daemon=True
        )
        with patch.object(self.backend, method, side_effect=blocking) as adapter:
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                yield adapter, results
            finally:
                release.set()
                worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_slow_start_holds_slot_and_cannot_publish_after_deadline(self):
        flow = self.flow(max_challenges=1, challenge_ttl=1)
        with self.blocked_call("start", lambda: self.start(flow)) as (begin, results):
            self.now += 1
            flow.expire()
            self.assertIsNone(self.start(flow))
            begin.assert_called_once()
        self.assertEqual(results, [None])
        self.assertIsNotNone(self.start(flow))

    def test_finish_consumes_before_verify_and_bounds_concurrent_work(self):
        flow = self.flow(max_challenges=3, max_in_flight=1)
        first, second = self.start(flow), self.start(flow)
        with self.blocked_call("finish", lambda: self.finish(flow, first)) as (
            verify,
            results,
        ):
            self.assertIsNone(self.finish(flow, first))
            self.assertIsNone(self.finish(flow, second))
            self.assertIsNone(self.start(flow))
            verify.assert_called_once()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0], "in-scope")
        self.assertIsNone(self.finish(flow, second))
        self.assertIsNotNone(self.start(flow))

    def test_slow_finish_cannot_issue_after_expiry_or_close(self):
        for shut_down in (False, True):
            with self.subTest(shut_down=shut_down):
                flow = self.flow(challenge_ttl=1, max_challenges=1)
                challenge = self.start(flow)
                with patch.object(flow.admission, "issue") as issue:
                    with self.blocked_call(
                        "finish",
                        lambda flow=flow, challenge=challenge: self.finish(
                            flow, challenge
                        ),
                    ) as (_, results):
                        if shut_down:
                            flow.close()
                        else:
                            self.now += 1
                            flow.expire()
                        self.assertIsNone(self.start(flow))
                    self.assertEqual(results, [None])
                    issue.assert_not_called()
                if shut_down:
                    self.assertIsNone(self.start(flow))
                else:
                    self.assertIsNotNone(self.start(flow))

    def test_expiry_callback_can_close_before_a_pending_finish(self):
        flow = self.flow()
        token = flow.admission.issue(self.binding, principal="existing-session")
        session = flow.admission.lookup(self.binding, token)
        self.now = session.expires_at - 1
        results = []
        with flow.admission.connection(self.binding, session, cancel=flow.close) as use:
            challenge = self.start(flow)
            # An expired independently-admitted session triggers a callback
            # that closes both the admission and pending verifier, not a new grant.
            self.now += 1
            worker = threading.Thread(target=flow.expire, daemon=True)
            worker.start()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertTrue(use.cancelled)
        results.append(self.finish(flow, challenge))
        self.assertEqual(results, [None])
        self.assertIsNone(self.start(flow))

    def test_malformed_snapshot_and_unbounded_configuration_refused(self):
        account = self.store.accounts[0]
        for store in (
            None,
            replace(self.store, setup=b""),
            replace(self.store, accounts=(account,) * (accounts.MAX_ACCOUNTS + 1)),
            replace(self.store, accounts=(account, account)),
            replace(self.store, accounts=(replace(account, username="Invalid"),)),
            replace(self.store, accounts=(replace(account, workspace_ids=("*",)),)),
            replace(self.store, accounts=(replace(account, registration=b""),)),
        ):
            with self.subTest(store=store), self.assertRaises(ValueError):
                self.flow(store=store)
        for key, value in (
            ("max_challenges", 129),
            ("max_challenges", True),
            ("max_in_flight", 9),
            ("max_in_flight", 0),
            ("challenge_ttl", float("inf")),
            ("challenge_ttl", float("nan")),
            ("challenge_ttl", 61),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.flow(**{key: value})
        with self.assertRaises(TypeError):
            self.flow(backend=None)
        for origin in (
            "http://account.example.test",
            self.binding.origin,
            self.binding.origin + ":9443",
        ):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                self.flow(auth_origin=origin)
        for size in (True, 0, -1, "2", pake.MAX_MESSAGE_BYTES + 1):
            with self.subTest(size=size), self.assertRaises(ValueError):
                replace(SIZES, ke1=size)
        self.assertEqual(self.backend.starts, [])


if __name__ == "__main__":
    unittest.main()
