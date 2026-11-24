from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
import socket
from threading import Event
from time import monotonic
import unittest
from unittest.mock import Mock, patch

from oc_box import oc_box as box, web_admission, web_named_owner, web_studio_read
from oc_box import web_studio_selection as studio


ONE = "ses_studio_one"
TWO = "ses_studio_two"


class StudioSelectionTests(unittest.TestCase):
    def setUp(self):
        on_host = patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"})
        on_host.start()
        self.addCleanup(on_host.stop)
        self.owner = web_named_owner.OwnerIdentity(
            "a" * 32, "b" * 32, "c" * 64, "2.4.1", "http://127.0.0.1:49153"
        )
        self.binding = web_admission.WebBinding(
            self.owner.workspace_id,
            self.owner.generation,
            "https://workspace.localhost",
        )
        self.admission = web_admission.BrowserAdmission(self.binding)
        self.browser = self.issue(self.admission)
        self.observe_owner = Mock(return_value=self.owner)
        self.registry = self.make_registry()

    def issue(self, admission):
        token = admission.issue(self.binding, principal="synthetic")
        return admission.lookup(self.binding, token)

    def make_registry(self, **args):
        return studio.SelectionRegistry(
            self.owner,
            self.admission,
            self.binding,
            observe_owner=self.observe_owner,
            **args,
        )

    def bootstrap(self, **kwargs):
        return self.registry.bootstrap(
            **{"browser": self.browser, "deadline": monotonic() + 2, **kwargs}
        )

    def change(self, page, sequence, session_id=ONE, **kwargs):
        return self.registry.change(
            **{
                "browser": self.browser,
                "nonce": page.nonce,
                "sequence": sequence,
                "session_id": session_id,
                "deadline": monotonic() + 2,
                "verify": lambda _owner, _browser, ident, _deadline: ident,
                **kwargs,
            }
        )

    def snapshot(self, page, **kwargs):
        return self.registry.snapshot(
            **{
                "browser": self.browser,
                "nonce": page.nonce,
                "session_id": page.session_id,
                "generation": page.generation,
                "deadline": monotonic() + 2,
                **kwargs,
            }
        )

    def observe(self, page):
        return self.registry.observe(page, monotonic() + 2)

    def assert_refused(self, callback, *args, **kwargs):
        with self.assertRaisesRegex(
            studio.SelectionError,
            r"^The Studio page selection could not be verified\.$",
        ):
            callback(*args, **kwargs)

    def test_exact_bootstrap_and_server_approved_read_scope(self):
        page = self.bootstrap()
        self.assertRegex(page.nonce, r"^[A-Za-z0-9_-]{43}$")
        self.assertEqual(
            (page.generation, page.session_id, page.scope), (0, None, None)
        )
        self.assertEqual(
            page.public_state(),
            {"page": page.nonce, "generation": 0, "sessionID": None},
        )
        self.assertIsNone(self.observe(page))
        self.assert_refused(self.snapshot, page, session_id=ONE, generation=1)

        verify = Mock(return_value=ONE)
        selected = self.change(page, 19, verify=verify)
        self.assertEqual(selected.generation, 1)  # Not the browser's sequence.
        self.assertEqual(
            selected.public_state(),
            {"page": page.nonce, "generation": 1, "sessionID": ONE},
        )
        owner, browser, ident, _deadline = verify.call_args.args
        self.assertIs(owner, self.owner)
        self.assertIs(browser, self.browser)
        self.assertEqual(ident, ONE)
        self.assertIs(self.snapshot(selected), selected)
        self.assertIs(
            self.snapshot(selected), selected
        )  # Exact current GET can repeat.
        self.assert_refused(self.snapshot, selected, session_id=TWO)
        self.assert_refused(self.snapshot, selected, generation=19)
        scope = self.observe(selected)
        self.assertIs(type(scope), web_studio_read.ReadScope)
        self.assertIs(scope.owner, self.owner)
        self.assertIs(scope.browser, self.browser)
        self.assertEqual((scope.session_id, scope.selection_generation), (ONE, 1))
        self.assert_refused(self.observe, page)
        self.assert_refused(self.observe, replace(selected))

    def test_replay_and_away_back_same_id_never_revalidate_old_scope(self):
        page = self.bootstrap()
        old = self.change(page, 1)
        verify = Mock(return_value=TWO)
        self.assert_refused(self.change, page, 1, TWO, verify=verify)
        verify.assert_not_called()
        self.assertIs(self.observe(old), old.scope)

        away = self.change(page, 3, None, verify=verify)
        self.assertEqual((away.generation, self.observe(away)), (2, None))
        verify.assert_not_called()
        self.assert_refused(self.change, page, 2, TWO, verify=verify)
        self.assert_refused(self.snapshot, old)
        back = self.change(page, 4)
        self.assertEqual((back.session_id, back.generation), (ONE, 3))
        self.assert_refused(self.observe, old)
        self.assert_refused(self.observe, away)
        self.assert_refused(self.snapshot, old)
        same = self.change(page, 5)
        self.assertEqual((same.session_id, same.generation), (ONE, 4))
        self.assert_refused(self.observe, back)

    def test_pages_with_same_generation_and_session_are_independent(self):
        one = self.change(self.bootstrap(), 1)
        two = self.change(self.bootstrap(), 1)
        self.assertNotEqual(one.nonce, two.nonce)
        self.assertEqual(one.scope, two.scope)
        self.assert_refused(self.observe, replace(one, nonce=two.nonce))
        moved = self.change(one, 2, TWO)
        self.assertEqual(self.observe(moved).session_id, TWO)
        self.assert_refused(self.observe, one)
        self.assertIs(self.observe(two), two.scope)
        self.registry.retire(self.browser, moved.nonce, deadline=monotonic() + 2)
        self.assert_refused(self.observe, moved)
        self.assertIs(self.observe(two), two.scope)

    def test_capacity_refuses_without_eviction_or_reuse_of_retired_nonce(self):
        self.registry = self.make_registry(max_pages=2)
        with patch.object(studio.secrets, "token_bytes", return_value=b"x" * 24):
            one = self.change(self.bootstrap(), 1)
            two = self.change(self.bootstrap(), 1, TWO)
            self.assert_refused(self.bootstrap)
            self.assertIs(self.observe(one), one.scope)
            self.assertIs(self.observe(two), two.scope)
            self.registry.retire(self.browser, one.nonce, deadline=monotonic() + 2)
            three = self.change(self.bootstrap(), 1)
        self.assertNotIn(three.nonce, (one.nonce, two.nonce))
        self.assert_refused(self.change, one, 2)
        self.assert_refused(self.snapshot, one)
        self.assertIs(self.observe(two), two.scope)
        self.assertIs(self.observe(three), three.scope)

    def test_nonce_ordinal_exhaustion_does_not_reopen_retired_page(self):
        with patch.object(studio, "_MAX_ORDINAL", 1):
            page = self.bootstrap()
            self.registry.retire(self.browser, page.nonce, deadline=monotonic() + 2)
            self.assert_refused(self.bootstrap)
            self.assert_refused(self.change, page, 1)

    def test_bad_verification_consumes_intent_but_never_selects_session(self):
        page = self.change(self.bootstrap(), 1)
        for sequence, verifier in enumerate(
            (
                Mock(return_value=True),
                Mock(return_value=TWO),
                Mock(side_effect=RuntimeError("PRIVATE NATIVE DIAGNOSTIC")),
            ),
            start=2,
        ):
            self.assert_refused(self.change, page, sequence, verify=verifier)
            self.assert_refused(self.observe, page)
            self.assert_refused(
                self.snapshot, page, generation=sequence, session_id=ONE
            )
            self.assert_refused(self.change, page, sequence)
        final = self.change(page, 5)
        self.assertEqual(final.generation, 5)

    def test_late_async_verifier_cannot_replace_newer_intent_or_view(self):
        page = self.change(self.bootstrap(), 1)
        started = Event()
        allow_return = Event()

        def verify(_owner, _browser, ident, deadline):
            started.set()
            return ident if allow_return.wait(max(0, deadline - monotonic())) else None

        with ThreadPoolExecutor(max_workers=1) as executor:
            slow = executor.submit(self.change, page, 2, TWO, verify=verify)
            try:
                self.assertTrue(started.wait(1))
                self.assert_refused(self.observe, page)
                self.assert_refused(self.snapshot, page, session_id=TWO, generation=2)
                newest = self.change(page, 4)  # Newer request skips an unseen sequence.
                self.assert_refused(self.change, page, 3, None)
            finally:
                allow_return.set()
            self.assert_refused(slow.result, timeout=1)
        self.assertIs(self.observe(newest), newest.scope)
        self.assertEqual((newest.session_id, newest.generation), (ONE, 3))

    def test_grant_mismatch_checks_exact_identity_not_values_or_principal(self):
        page = self.change(self.bootstrap(), 1)
        foreign = web_admission.BrowserAdmission(self.binding)
        for impostor in (
            self.issue(foreign),
            self.issue(self.admission),
            replace(self.browser),
            self.browser._token,
        ):
            with self.subTest(kind=type(impostor).__name__):
                self.assert_refused(self.snapshot, page, browser=impostor)
                self.assert_refused(self.change, page, 2, browser=impostor)
                self.assert_refused(
                    self.registry.retire, impostor, page.nonce, deadline=monotonic() + 2
                )
        self.assert_refused(self.bootstrap, browser=self.issue(foreign))
        other_registry = studio.SelectionRegistry(
            self.owner, foreign, self.binding, observe_owner=self.observe_owner
        )
        self.assert_refused(
            other_registry.bootstrap, self.browser, deadline=monotonic() + 2
        )
        self.assertIs(self.observe(page), page.scope)

    def test_real_revocation_refuses_during_registered_use_and_committing_change(self):
        page = self.change(self.bootstrap(), 1)
        interrupted = Event()
        revoked = []

        def verify(_owner, _browser, ident, _deadline):
            revoked.append(self.admission.begin_revocation(self.binding, self.browser))
            return ident

        with self.admission.connection(
            self.binding, self.browser, cancel=interrupted.set
        ) as use:
            self.assertIsNotNone(use)
            self.assertIs(self.observe(page), page.scope)
            self.assert_refused(self.change, page, 2, TWO, verify=verify)
            self.assertTrue(interrupted.is_set() and use.cancelled)
            self.assertFalse(revoked[0].wait_drained(0))
            self.assert_refused(self.observe, page)
            self.assert_refused(self.bootstrap)
        self.assertTrue(revoked[0].wait_drained(0))

    def test_revoked_page_cannot_be_replayed_by_same_principal_or_reused_token(self):
        self.registry = self.make_registry(max_pages=1)
        page = self.change(self.bootstrap(), 1)
        self.assertTrue(self.admission.revoke(self.binding, self.browser))
        self.assert_refused(self.observe, page)
        with patch.object(
            web_admission.secrets, "token_urlsafe", return_value=self.browser._token
        ):
            new_browser = self.issue(self.admission)
        self.assertIsNot(new_browser, self.browser)
        fresh = self.bootstrap(browser=new_browser)  # Sweep releases capacity.
        self.assertNotEqual(fresh.nonce, page.nonce)
        self.assert_refused(self.snapshot, page, browser=new_browser)
        self.assert_refused(self.observe, page)

    def test_read_backend_observer_holds_initial_view_through_use(self):
        view = self.snapshot(self.change(self.bootstrap(), 1))
        write = Mock()
        replacement = []
        control, peer = socket.socketpair()
        with control, peer:
            lease = web_named_owner.VerifiedOwnerLease(
                self.owner,
                control,
                "/synthetic/project",
                "/synthetic/home",
                "synthetic_unused_password_" + "q" * 24,
                observe=lambda: self.owner,
                connect_native=Mock(),
            )

            def projected(*_args, **_kwargs):
                self.change(view, 2, None)
                replacement.append(self.change(view, 3))
                return {
                    "sessionID": ONE,
                    "title": "Synthetic",
                    "messages": [],
                    "hasEarlier": False,
                }

            with patch.object(
                web_studio_read.studio, "_read_conversation", side_effect=projected
            ):
                with self.assertRaises(web_studio_read.ReadError):
                    web_studio_read.respond(
                        box,
                        web_studio_read.ReadRequest(
                            "GET", web_studio_read.PATH + "?session=" + ONE, b""
                        ),
                        view.scope,
                        lease=lease,
                        admission=self.admission,
                        binding=self.binding,
                        observe=lambda deadline: self.registry.observe(view, deadline),
                        deadline=monotonic() + 2,
                        write=write,
                        interrupt=lambda: None,
                        retire=lambda: True,  # Synthetic writer owns no stream.
                    )
        self.assert_refused(self.observe, view)
        self.assertEqual(len(replacement), 1)
        self.assertEqual(self.observe(replacement[0]).session_id, ONE)
        write.assert_not_called()
        self.assertTrue(
            self.admission.begin_revocation(self.binding, self.browser).wait_drained(0)
        )

    def test_teardown_and_owner_drift_are_permanent_even_at_same_endpoint(self):
        self.assert_refused(
            studio.SelectionRegistry,
            replace(self.owner, generation="e" * 32),
            self.admission,
            self.binding,
            observe_owner=self.observe_owner,
        )
        page = self.change(self.bootstrap(), 1)
        self.observe_owner.return_value = replace(self.owner, generation="e" * 32)
        self.assert_refused(self.observe, page)
        self.observe_owner.return_value = self.owner
        self.assert_refused(self.observe, page)
        self.assert_refused(self.bootstrap)

        self.registry = self.make_registry()
        page = self.bootstrap()

        def verify(_owner, _browser, ident, _deadline):
            self.registry.close()
            return ident

        self.assert_refused(self.change, page, 1, verify=verify)
        self.assert_refused(self.observe, page)
        self.assert_refused(self.bootstrap)

    def test_absolute_page_expiry_returns_capacity_without_replay(self):
        self.registry = self.make_registry(max_pages=1, page_ttl=1)
        clock = monotonic()
        with patch.object(studio, "monotonic", return_value=clock) as now:
            page = self.change(self.bootstrap(), 1)
            now.return_value = clock + 0.8
            self.assertIs(self.observe(page), page.scope)  # No TTL refresh.
            now.return_value = clock + 1
            self.assert_refused(self.observe, page)
            self.registry.expire(deadline=clock + 2)
            fresh = self.bootstrap()
            self.assertNotEqual(fresh.nonce, page.nonce)
            self.assert_refused(self.change, page, 2)

    def test_grant_expiry_fences_before_admission_sweep(self):
        self.admission = web_admission.BrowserAdmission(self.binding, session_ttl=10)
        self.browser = self.issue(self.admission)
        self.registry = self.make_registry()
        page = self.change(self.bootstrap(), 1)
        expires = self.browser.expires_at
        with patch.object(studio, "monotonic", return_value=expires):
            self.assert_refused(self.registry.observe, page, expires + 2)
            self.assert_refused(
                self.registry.bootstrap, self.browser, deadline=expires + 2
            )

    def test_verifier_cannot_commit_after_page_or_request_deadline(self):
        for cause in ("page", "request"):
            with self.subTest(cause=cause):
                self.registry = self.make_registry(
                    page_ttl=1 if cause == "page" else 60
                )
                clock = monotonic()
                with patch.object(studio, "monotonic", return_value=clock) as now:
                    page = self.change(self.bootstrap(), 1)
                    expire_at = clock + (1 if cause == "page" else 2)

                    def verify(_owner, _browser, ident, deadline):
                        self.assertEqual(deadline, expire_at)
                        now.return_value = expire_at
                        return ident

                    self.assert_refused(
                        self.change, page, 2, TWO, deadline=clock + 2, verify=verify
                    )
                    self.assertEqual(now.return_value, expire_at)
                    self.assert_refused(
                        self.registry.snapshot,
                        self.browser,
                        page.nonce,
                        session_id=TWO,
                        generation=2,
                        deadline=expire_at + 1,
                    )

    def test_deadline_and_bad_request_shape_cannot_authorize_selection(self):
        page = self.bootstrap()
        verify = Mock(return_value=ONE)
        for bad in (
            {"nonce": page.nonce + "="},
            {"nonce": "x" * 43},
            {"sequence": True},
            {"sequence": -1},
            {"sequence": 2**53},
            {"session_id": "/api/session/" + ONE},
            {"deadline": float("inf")},
            {"deadline": monotonic() - 1},
        ):
            with self.subTest(bad=bad):
                self.assert_refused(
                    self.registry.change,
                    self.browser,
                    **{
                        "nonce": page.nonce,
                        "sequence": 1,
                        "session_id": ONE,
                        "deadline": monotonic() + 2,
                        "verify": verify,
                        **bad,
                    },
                )
        verify.assert_not_called()
        self.assertIsNone(self.observe(page))


if __name__ == "__main__":
    unittest.main()
