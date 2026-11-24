"""Synthetic one-use selection checks; no gateway, native app or stored cookie."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oc_box import web_studio_guest as guest
from oc_box import web_studio_guest_binding as guest_binding
from oc_box import web_studio_guest_native as native
from oc_box import web_studio_guest_read as guest_read
from oc_box import web_studio_guest_selection as selection
from oc_box import web_studio_guest_send as guest_send
from oc_box import web_studio_send


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

    def test_guest_send_uses_the_same_fresh_cookie_and_retains_uncertain_post(self):
        for transport_ok in (True, False):
            with self.subTest(transport_ok=transport_ok):
                page = self.page()
                old_use = self.use(page)
                self.pages.change(
                    old_use, sequence=1, session_id="ses_exact", verify=self.verify
                )
                use = self.use(page, authorize=False)
                endpoint = "http://127.0.0.1:48232"
                identity = SimpleNamespace(
                    workspace_id=self.binding.workspace_id,
                    generation=self.binding.owner_generation,
                    container_id=self.binding.container_id,
                    endpoint=endpoint,
                    version="2.4.1",
                )
                owner = SimpleNamespace(
                    owner=SimpleNamespace(
                        box=SimpleNamespace(container_project=lambda data: "/fixture")
                    ),
                    data={},
                )
                scope = guest_binding._UnservedBinding(
                    None,
                    None,
                    owner,
                    identity,
                    native.GuestNativeScope(self.binding, endpoint, 1, "2.4.1"),
                    Event(),
                )
                registry = Mock()
                registered_use = object()
                peer = Mock()
                peer.post_attempted = True
                peer.query.return_value = {
                    "data": {
                        "id": "msg_persisted",
                        "sessionID": "ses_exact",
                        "time": {"created": 1},
                        "type": "user",
                        "payload": {"text": "synthetic input"},
                        "delivery": "queue",
                    }
                }
                relay = Mock()
                relay.retire.return_value = transport_ok
                with (
                    patch.object(
                        guest_send.web_guest_duplex_exec,
                        "GuestDuplexExec",
                        return_value=relay,
                    ),
                    patch.object(guest_send, "_Peer", return_value=peer) as endpoints,
                    patch.object(
                        web_studio_send,
                        "_preflight",
                        return_value="/api/session/ses_exact/prompt",
                    ),
                ):
                    result = guest_send.send(
                        scope,
                        registry,
                        registered_use,
                        self.gate,
                        use,
                        page.grant,
                        self.pages,
                        session_id="ses_exact",
                        generation=1,
                        text="synthetic input",
                        cookie=_COOKIE,
                    )
                self.assertEqual(
                    result,
                    (
                        web_studio_send.SendResult.QUEUED
                        if transport_ok
                        else web_studio_send.SendResult.UNCONFIRMED,
                        transport_ok,
                    ),
                )
                self.assertEqual(endpoints.call_args.args[2], _COOKIE)
                self.assertTrue(self.gate.native_current(use))
                relay.retire.assert_called_once_with()
                peer.query.assert_called_once_with(
                    "/api/session/ses_exact/prompt",
                    body=b'{"text":"synthetic input","delivery":"queue","resume":true}',
                )

    def test_private_post_wire_uses_native_cookie_not_privileged_auth(self):
        reply = b'{"ok":true}'
        wire = (
            b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(reply)}\r\n\r\n".encode("ascii")
            + reply
        )
        tunnel = Mock()
        tunnel.ready = True
        tunnel.ended = False
        tunnel._queued_data = None

        def receive():
            if not tunnel.consumed.call_count:
                return wire
            tunnel.ended = True
            return None

        tunnel.receive.side_effect = receive
        peer = guest_send._Peer(
            tunnel,
            self.binding,
            _COOKIE,
            monotonic() + 1,
            check=lambda: None,
            current=lambda: None,
        )
        self.assertEqual(
            peer.query("/api/session/ses_exact/prompt", body=b"{}"), {"ok": True}
        )
        sent = tunnel.send.call_args.args[0]
        self.assertIn(b"Cookie: " + _COOKIE.encode("ascii") + b"\r\n", sent)
        self.assertNotIn(b"Authorization:", sent)
        self.assertTrue(peer.post_attempted)
        with self.assertRaises(web_studio_send.SendError):
            peer.query("/api/session/ses_exact/prompt", body=b"{}")
        tunnel.send.assert_called_once()

    def test_guest_catalog_matches_native_pid_workspace_and_exact_projection(self):
        identity = SimpleNamespace(
            version="2.4.1", endpoint="http://127.0.0.1:48232", native_pid=441
        )
        info = {
            "version": identity.version,
            "pid": identity.native_pid,
            "urls": [identity.endpoint],
            "hostname": "fixture",
            "paths": {"tmp": "/tmp/fixture"},
            "capabilities": {"persistentPty": True},
        }
        location = {
            "directory": "/fixture",
            "project": {
                "id": "p_fixture",
                "directory": "/fixture",
                "canonical": "/fixture",
            },
        }
        row = {
            "id": "ses_exact",
            "location": {"directory": "/fixture"},
            "projectID": "p_fixture",
            "time": {"created": 1, "updated": 2},
            "title": "Synthetic session",
        }
        peer = Mock()
        peer.query.side_effect = [
            info,
            location,
            {"data": [row], "cursor": {"next": None, "previous": None}},
            info,
            location,
        ]
        view = guest_read._catalog(peer, identity, "/fixture", ("must-not-show",))
        self.assertEqual(view["sessions"][0]["id"], "ses_exact")
        self.assertEqual(view["sessions"][0]["title"], "Synthetic session")
        self.assertEqual(peer.query.call_args.kwargs, {"closing": True})
        contaminated = {**row, "title": "Synthetic token must-not-show"}
        peer.query.side_effect = [
            info,
            location,
            {"data": [contaminated], "cursor": {"next": None, "previous": None}},
        ]
        with self.assertRaises(ValueError):
            guest_read._catalog(peer, identity, "/fixture", ("must-not-show",))
        peer.query.side_effect = [{**info, "pid": 999}, location]
        with self.assertRaises(ValueError):
            guest_read._catalog(peer, identity, "/fixture", ())
        invalid = {**row, "location": {"directory": "/other"}}
        peer.query.side_effect = [
            info,
            location,
            {"data": [invalid], "cursor": {"next": None, "previous": None}},
        ]
        with self.assertRaises(ValueError):
            guest_read._catalog(peer, identity, "/fixture", ())
