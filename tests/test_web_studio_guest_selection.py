"""Synthetic one-use selection checks; no gateway, native app or stored cookie."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic
import unittest
from unittest.mock import patch

from oc_box import web_studio_guest as guest
from oc_box import web_studio_guest_selection as selection


_COOKIE = "synthetic-direct-native=page-selection-test"


class GuestSelectionTests(unittest.TestCase):
    def setUp(self):
        context = patch.dict("os.environ", {"OC_BOX_CONTAINER": "0"})
        context.start()
        self.addCleanup(context.stop)
        self.binding = guest.GuestBinding(
            "a" * 32, "b" * 32, "c" * 64, "http://127.0.0.1:48231"
        )
        self.gate = guest.GuestAdmission(
            self.binding,
            observe_gateway=lambda original, **_: original,
            verify_native=lambda original, **_: original,
        )
        self.addCleanup(self.gate.close)
        self.pages = selection.GuestSelections(self.gate, self.binding)

    def page(self):
        body = self.gate.offer(self.binding).value.encode("ascii")
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
                ("Cookie", _COOKIE),
            ),
            body,
        )
        use, page = self.gate.confirm(
            self.binding, request, cancel=lambda: None, deadline=monotonic() + 8
        )
        self.assertIsNotNone(page)
        self.assertTrue(self.gate.finish(use, confirmed=True))
        return page

    def use(self, page, *, cookie=_COOKIE, authorize=True):
        use, grant = self.gate.start(
            self.binding,
            app=page.app,
            page=page.page,
            cancel=lambda: None,
            deadline=monotonic() + 8,
        )
        self.addCleanup(self.gate.finish, use, confirmed=True)
        self.assertIs(grant, page.grant)
        if authorize:
            self.assertEqual(
                self.gate.authorize_native(use, grant, cookie=cookie),
                cookie is not None,
            )
        return use

    def verify(self, original, use, session_id, deadline):
        self.assertIs(original, self.binding)
        self.assertIsInstance(use, guest.GuestUse)
        self.assertEqual(deadline, use.deadline)
        return session_id

    def test_exact_native_use_selects_and_never_adopts_another_grant(self):
        page = self.page()
        use = self.use(page, authorize=False)
        with self.assertRaises(selection.GuestSelectionError):
            self.pages.state(use)
        self.assertTrue(self.gate.authorize_native(use, page.grant, cookie=_COOKIE))
        self.assertEqual(
            self.pages.state(use).public_state(), {"generation": 0, "sessionID": None}
        )
        approved = self.pages.change(
            use, sequence=1, session_id="ses_exact", verify=self.verify
        )
        self.assertIs(
            self.pages.snapshot(use, session_id="ses_exact", generation=1), approved
        )
        self.assertIs(self.pages.observe(use, approved), approved)
        with self.assertRaises(selection.GuestSelectionError):
            self.pages.observe(self.use(self.page()), approved)
        with self.assertRaises(selection.GuestSelectionError):
            self.pages.change(use, sequence=1, session_id=None)
        self.assertIsNone(
            self.pages.change(use, sequence=2, session_id=None).session_id
        )
        with self.assertRaises(selection.GuestSelectionError):
            self.pages.observe(use, approved)

    def test_newer_intent_prevents_a_late_verifier_from_restoring_its_view(self):
        page = self.page()
        first, second = self.use(page), self.use(page)
        entered, release = Event(), Event()

        def verify(original, use, ident, deadline):
            entered.set()
            self.assertTrue(release.wait(2))
            return self.verify(original, use, ident, deadline)

        with ThreadPoolExecutor(1) as pool:
            older = pool.submit(
                self.pages.change,
                first,
                sequence=1,
                session_id="ses_old",
                verify=verify,
            )
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaises(selection.GuestSelectionError):
                    self.pages.state(second)
                latest = self.pages.change(
                    second, sequence=2, session_id="ses_new", verify=self.verify
                )
            finally:
                release.set()
            with self.assertRaises(selection.GuestSelectionError):
                older.result(timeout=2)
        self.assertIs(self.pages.observe(second, latest), latest)

    def test_cookie_loss_retires_selection_and_expired_scope_cannot_reappear(self):
        page = self.page()
        use = self.use(page)
        view = self.pages.change(
            use, sequence=1, session_id="ses_private", verify=self.verify
        )
        self.use(page, cookie=None)
        with self.assertRaises(selection.GuestSelectionError):
            self.pages.observe(use, view)
        self.assertIsNone(
            self.gate.start(
                self.binding,
                app=page.app,
                page=page.page,
                cancel=lambda: None,
                deadline=monotonic() + 8,
            )
        )
