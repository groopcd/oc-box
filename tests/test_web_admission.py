"""Offline session lifetime tests with synthetic users, no listener or verifier."""

import threading
import unittest
from contextlib import ExitStack
from dataclasses import replace
from unittest.mock import patch

from oc_box import web_admission as admission


class WebAdmissionTests(unittest.TestCase):
    def setUp(self):
        clock = patch.object(admission, "monotonic", side_effect=lambda: self.now)
        self.now = 100.0
        clock.start()
        self.addCleanup(clock.stop)
        self.binding = admission.WebBinding(
            workspace_id="workspace-a",
            owner_generation="owner-a",
            origin="https://workspace.example.test",
        )

    def registry(self, **options):
        return admission.BrowserAdmission(self.binding, **options)

    def sign_in(self, registry, principal="first"):
        token = registry.issue(self.binding, principal=principal)
        self.assertRegex(token, r"\A[A-Za-z0-9_-]{43}\Z")
        session = registry.lookup(self.binding, token)
        self.assertIsNotNone(session)
        self.assertEqual(session.principal, principal)
        self.assertNotIn(token, repr(session))
        return token, session

    def test_requires_well_formed_binding_and_exact_scope(self):
        registry = self.registry(attempt_limit=1)
        token, session = self.sign_in(registry)
        for binding in (
            replace(self.binding, workspace_id="workspace-b"),
            replace(self.binding, owner_generation="owner-b"),
            replace(self.binding, origin="https://workspace.example.test:8443"),
            None,
        ):
            with self.subTest(binding=binding):
                self.assertIsNone(registry.lookup(binding, token))
                self.assertIsNone(registry.issue(binding, principal="first"))
                self.assertFalse(registry.try_attempt(binding))
                self.assertFalse(registry.revoke(binding, session))
                with registry.connection(binding, session, cancel=self.fail) as use:
                    self.assertIsNone(use)
        self.assertIs(registry.lookup(replace(self.binding), token), session)
        self.assertTrue(registry.try_attempt(self.binding))
        for field, value in (
            ("workspace_id", ""),
            ("owner_generation", None),
            ("origin", "http://workspace.example.test"),
            ("origin", "https://workspace.example.test:443"),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                replace(self.binding, **{field: value})

    def test_session_capacity_does_not_evict_live_sessions(self):
        registry = self.registry(max_sessions=2)
        with patch.object(
            admission.secrets, "token_urlsafe", wraps=admission.secrets.token_urlsafe
        ) as random:
            first, first_session = self.sign_in(registry)
            second, second_session = self.sign_in(registry)
            self.assertNotEqual(first, second)
            self.assertEqual(
                [call.args for call in random.call_args_list], [(32,), (32,)]
            )
        self.assertIsNone(registry.issue(self.binding, principal="third"))
        self.assertIs(registry.lookup(self.binding, first), first_session)
        self.assertIs(registry.lookup(self.binding, second), second_session)
        self.assertTrue(registry.revoke(self.binding, first_session))
        third, _ = self.sign_in(registry, "third")
        self.assertNotIn(third, (first, second))
        self.assertIsNone(registry.lookup(self.binding, first))
        self.assertIs(registry.lookup(self.binding, second), second_session)

    def test_bad_token_is_not_a_session(self):
        registry = self.registry()
        token, _ = self.sign_in(registry)
        for bad in (None, {}, token.encode(), token + "\n", "A" * 4096, "!" * 43):
            with self.subTest(kind=type(bad)):
                self.assertIsNone(registry.lookup(self.binding, bad))

    def test_revoke_cancels_only_that_browser_even_for_same_principal(self):
        registry = self.registry()
        first, first_session = self.sign_in(registry)
        other_browser, peer = self.sign_in(registry)
        other_account, other = self.sign_in(registry, "second")
        cancelled = []
        with ExitStack() as stack:
            uses = [
                stack.enter_context(
                    registry.connection(
                        self.binding, session, cancel=lambda i=i: cancelled.append(i)
                    )
                )
                for i, session in enumerate((first_session, first_session, peer, other))
            ]
            self.assertTrue(registry.revoke(self.binding, first_session))
            self.assertFalse(registry.revoke(self.binding, first_session))
            self.assertCountEqual(cancelled, [0, 1])
            self.assertEqual(
                [use.cancelled for use in uses], [True, True, False, False]
            )
            self.assertIsNone(registry.lookup(self.binding, first))
            self.assertIs(registry.lookup(self.binding, other_browser), peer)
            self.assertIs(registry.lookup(self.binding, other_account), other)
        registry.close()
        self.assertCountEqual(cancelled, [0, 1])

    def test_revocation_does_not_report_drained_after_a_failed_callback(self):
        registry = self.registry()
        _, session = self.sign_in(registry)

        def failed_cancel():
            raise OSError("synthetic transport cleanup")

        with registry.connection(self.binding, session, cancel=failed_cancel) as use:
            result = registry.begin_revocation(self.binding, session)
            self.assertTrue(result.revoked)
            self.assertTrue(use.cancelled)
        self.assertFalse(result.wait_drained(0))
        self.assertTrue(registry.cancellation_failed)
        with registry.connection(self.binding, session, cancel=failed_cancel) as use:
            self.assertIsNone(use)

    def test_expiry_is_absolute_monotonic_and_cancels_idle_connections(self):
        registry = self.registry(session_ttl=10, max_sessions=2)
        token, session = self.sign_in(registry)
        cancelled = threading.Event()
        with registry.connection(self.binding, session, cancel=cancelled.set) as use:
            self.now += 9
            live, live_session = self.sign_in(registry)
            self.assertIs(registry.lookup(self.binding, token), session)
            self.assertFalse(cancelled.is_set())
            self.now += 1
            registry.expire()
            self.assertTrue(cancelled.is_set())
            self.assertTrue(use.cancelled)
            self.assertIsNone(registry.lookup(self.binding, token))
            self.assertIs(registry.lookup(self.binding, live), live_session)
            self.sign_in(registry)

    def test_expired_snapshot_cannot_admit_without_background_sweep(self):
        registry = self.registry(session_ttl=10)
        _, session = self.sign_in(registry)
        self.now += 10
        with registry.connection(self.binding, session, cancel=self.fail) as use:
            self.assertIsNone(use)

    def test_connection_capacity_includes_cancelled_contexts_until_cleanup(self):
        registry = self.registry(max_connections=1)
        _, first = self.sign_in(registry)
        _, second = self.sign_in(registry)
        with registry.connection(self.binding, first, cancel=lambda: None) as use:
            self.assertIsNotNone(use)
            with registry.connection(self.binding, second, cancel=self.fail) as denied:
                self.assertIsNone(denied)
            registry.revoke(self.binding, first)
            self.assertTrue(use.cancelled)
            with registry.connection(self.binding, second, cancel=self.fail) as denied:
                self.assertIsNone(denied)
        with self.assertRaisesRegex(RuntimeError, "^work ended$"):
            with registry.connection(self.binding, second, cancel=self.fail) as use:
                self.assertIsNotNone(use)
                raise RuntimeError("work ended")
        with registry.connection(self.binding, second, cancel=self.fail) as use:
            self.assertIsNotNone(use)
        self.assertTrue(registry.revoke(self.binding, second))
        self.assertFalse(registry.cancellation_failed)

    def test_logout_winning_race_refuses_delayed_upstream_admission(self):
        registry = self.registry()
        token, session = self.sign_in(registry)
        looked_up = threading.Event()
        resume = threading.Event()
        snapshots = []
        observed = []

        def request_worker():
            snapshot = registry.lookup(self.binding, token)
            snapshots.append(snapshot)
            looked_up.set()
            if not resume.wait(2):
                return
            with registry.connection(
                self.binding, snapshot, cancel=lambda: observed.append("cancelled")
            ) as use:
                observed.append("denied" if use is None else "admitted")

        worker = threading.Thread(target=request_worker, daemon=True)
        worker.start()
        try:
            self.assertTrue(looked_up.wait(2))
            self.assertTrue(registry.revoke(self.binding, session))
            self.sign_in(registry)
        finally:
            resume.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(snapshots, [session])
        self.assertEqual(observed, ["denied"])

    def test_logout_latches_startup_cancellation_before_callback_returns(self):
        registry = self.registry()
        token, session = self.sign_in(registry)
        entered = threading.Event()
        finish = threading.Event()
        observed = []

        def notify_transport():
            entered.set()
            observed.append(finish.wait(2))

        with registry.connection(self.binding, session, cancel=notify_transport) as use:
            worker = threading.Thread(
                target=registry.revoke, args=(self.binding, session), daemon=True
            )
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(use.cancelled)
                self.assertIsNone(registry.lookup(self.binding, token))
                with registry.connection(
                    self.binding, session, cancel=self.fail
                ) as late:
                    self.assertIsNone(late)
            finally:
                finish.set()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(observed, [True])

    def test_snapshot_generation_and_registry_identity_are_rechecked(self):
        registry = self.registry()
        other_registry = self.registry()
        # Force a synthetic collision to prove that matching token text and a
        # matching binding do not substitute for the live session generation.
        with patch.object(admission.secrets, "token_urlsafe", return_value="A" * 43):
            token, old = self.sign_in(registry)
            self.assertIsNone(registry.issue(self.binding, principal="first"))
            self.assertTrue(registry.revoke(self.binding, old))
            self.assertEqual(self.sign_in(registry)[0], token)
            same_token, foreign = self.sign_in(other_registry)
        self.assertEqual(same_token, token)
        for stale in (old, foreign):
            self.assertFalse(registry.revoke(self.binding, stale))
            with registry.connection(self.binding, stale, cancel=self.fail) as use:
                self.assertIsNone(use)
        self.assertIsNotNone(registry.lookup(self.binding, token))

    def test_close_all_remains_closed_and_callback_failure_does_not_skip_peers(self):
        registry = self.registry()
        token, first = self.sign_in(registry)
        _, second = self.sign_in(registry, "second")
        observed = []

        def broken_callback():
            # Trusted cleanup can safely reenter; revocation is already visible.
            observed.append(registry.lookup(self.binding, token))
            registry.close()
            raise RuntimeError("synthetic transport failure")

        def close_with_connections():
            with ExitStack() as stack:
                first_use = stack.enter_context(
                    registry.connection(self.binding, first, cancel=broken_callback)
                )
                second_use = stack.enter_context(
                    registry.connection(
                        self.binding, second, cancel=lambda: observed.append("second")
                    )
                )
                registry.close()
                observed.append((first_use.cancelled, second_use.cancelled))

        worker = threading.Thread(target=close_with_connections, daemon=True)
        worker.start()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        registry.close()
        self.assertTrue(registry.cancellation_failed)
        self.assertCountEqual(observed, [None, "second", (True, True)])
        self.assertFalse(registry.try_attempt(self.binding))
        self.assertIsNone(registry.issue(self.binding, principal="first"))
        self.assertFalse(registry.revoke(self.binding, second))
        with registry.connection(self.binding, second, cancel=self.fail) as use:
            self.assertIsNone(use)

    def test_shared_rolling_attempt_budget_is_atomic_and_only_ages_out(self):
        registry = self.registry(attempt_limit=3, attempt_window=10)
        self.assertTrue(registry.try_attempt(self.binding))
        self.now += 1
        self.assertTrue(registry.try_attempt(self.binding))
        self.sign_in(registry)
        self.now += 1
        self.assertTrue(registry.try_attempt(self.binding))
        self.now += 7
        for _ in range(100):
            self.assertFalse(registry.try_attempt(self.binding))
        self.now += 1
        barrier = threading.Barrier(5, timeout=2)
        results = []

        def attempt():
            barrier.wait()
            results.append(registry.try_attempt(self.binding))

        workers = [threading.Thread(target=attempt, daemon=True) for _ in range(4)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertCountEqual(results, [True, False, False, False])
        self.now += 1
        self.assertTrue(registry.try_attempt(self.binding))
        self.assertFalse(registry.try_attempt(self.binding))

    def test_limits_refuse_unbounded_or_nonpositive_values(self):
        for key, value in (
            ("max_sessions", 10**9),
            ("max_connections", 0),
            ("session_ttl", float("inf")),
            ("session_ttl", float("nan")),
            ("attempt_limit", True),
            ("attempt_window", -1),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                admission.BrowserAdmission(self.binding, **{key: value})


if __name__ == "__main__":
    unittest.main()
