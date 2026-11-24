"""The unserved browser handoff preserves exact context and admission boundaries."""

import os
import unittest
from unittest.mock import Mock, patch

import test_web_pake_flow as fixture

from oc_box import (
    web_admission,
    web_app_ingress,
    web_bootstrap,
    web_handoff,
    web_local_origins,
    web_named_handoff as handoff,
    web_named_headers,
    web_named_local,
    web_native_pair,
)

ENDPOINT = "http://127.0.0.1:43100"
NATIVE = "a" * 10 + "." + "b" * 43
VERSION = "2.3.2"


class NamedBrowserHandoffTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"})
        environment.start()
        self.addCleanup(environment.stop)
        self.now = 100
        clock = patch.object(web_handoff, "monotonic", new=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.origins = web_local_origins.LocalOrigins()
        self.binding = web_admission.WebBinding(
            fixture.WORKSPACE,
            "synthetic-owner",
            self.origins.workspace.value,
        )
        self.authority = self.origins.workspace.value.removeprefix("https://")
        self.owner = Mock(return_value=True)
        self.pair = Mock(
            return_value=web_native_pair.NativeCookie(
                self.binding,
                ENDPOINT,
                self.authority,
                NATIVE,
                version=VERSION,
                owner_current=self.owner,
            )
        )
        self.bootstrap = web_bootstrap.BrowserBootstrap(
            origins=self.origins, binding=self.binding
        )
        self.addCleanup(self.bootstrap.close)
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
        self.app = web_app_ingress.NamedAppIngress(
            self.sign_in,
            endpoint=ENDPOINT,
            version=VERSION,
            claim_bootstrap=lambda nonce: self.bootstrap.claim_app(self.binding, nonce),
        )
        self.addCleanup(self.app.close)
        self.ingress = handoff.NamedHandoffIngress(origins=self.origins, app=self.app)

    def paired(self):
        bootstrap = self.bootstrap.issue(self.binding)
        self.assertIsNotNone(
            self.bootstrap.accept(
                self.binding,
                auth_origin=self.origins.account.value,
                ticket=bootstrap.value,
            )
        )
        return bootstrap.app_nonce

    def ticket(self, browser):
        result = self.sign_in.start(
            browser_nonce=browser, username="in-scope", ke1=fixture.KE1
        )
        return self.sign_in.finish(
            browser_nonce=browser,
            challenge_id=result.challenge_id,
            ke3=fixture.PROOF,
        ).value

    def request(self, ticket, *, cross=False, browser=None, fields=None, **changes):
        headers = {
            "Host": self.authority,
            "Origin": self.origins.account.value
            if cross
            else self.origins.workspace.value,
            "Sec-Fetch-Site": "cross-site" if cross else "same-origin",
            "Sec-Fetch-Mode": "navigate" if cross else "same-origin",
            "Sec-Fetch-Dest": "document" if cross else "empty",
            "Content-Length": "61" if cross else "54",
            "Content-Type": "application/x-www-form-urlencoded"
            if cross
            else "text/plain",
        }
        if browser is not None:
            headers["Cookie"] = "__Host-ocbox-bootstrap=" + browser
        for key, value in (fields or {}).items():
            if value is None:
                headers.pop(key, None)
            else:
                headers[key] = value
        data = (
            dict(
                method="POST",
                target=handoff.HANDOFF_PATH if cross else handoff.CONFIRM_PATH,
                body=(("ticket=" if cross else "") + ticket).encode("ascii"),
                headers=list(headers.items()),
                actual_tls=True,
            )
            | changes
        )
        return (self.ingress.receive if cross else self.ingress.confirm)(**data)

    def test_receive_requires_exact_opposite_site_document_post(self):
        browser = self.paired()
        ticket = self.ticket(browser)
        for change in (
            {"method": "GET"},
            {"target": handoff.HANDOFF_PATH + "?x=1"},
            {"actual_tls": False},
            {"body": f"ticket={ticket}&".encode()},
            {"body": ("namexx=" + ticket).encode()},
            {"body": ("ticket=" + ticket[:-1] + "B").encode()},
            {"body": bytearray(("ticket=" + ticket).encode())},
            {"browser": browser},
            {"fields": {"Origin": self.origins.workspace.value}},
            {"fields": {"Origin": "https://foreign.test:9443"}},
            {"fields": {"Origin": None}},
            {"fields": {"Sec-Fetch-Site": "same-origin"}},
            {"fields": {"Sec-Fetch-Mode": "cors"}},
            {"fields": {"Sec-Fetch-Dest": "iframe"}},
            {"fields": {"Content-Length": "061"}},
            {"fields": {"Content-Type": "text/plain"}},
            {"fields": {"Content-Encoding": "gzip"}},
            {"fields": {"Range": "bytes=0-5"}},
        ):
            with (
                self.subTest(keys=tuple(change)),
                self.assertRaises(web_named_headers.NamedRequestError),
            ):
                self.request(ticket, cross=True, **change)
        pending = self.request(ticket, cross=True)
        fields = dict(pending.headers)
        self.assertEqual(pending.status, 200)
        self.assertEqual(fields["Cache-Control"], "no-store")
        self.assertNotIn("Set-Cookie", fields)
        self.assertNotIn(ticket, repr(pending))
        self.assertIn(b"document.currentScript.remove()", pending.body)
        self.assertNotIn(b"<input", pending.body)
        self.assertIn(
            "connect-src " + self.origins.workspace.value + handoff.CONFIRM_PATH,
            fields["Content-Security-Policy"],
        )
        self.assertEqual(
            self.request(ticket, cross=True).status, 200
        )  # No consumption or grant.
        self.pair.assert_not_called()

    def test_confirm_checks_real_fetch_envelope_then_exact_browser_once(self):
        browser, other = self.paired(), self.paired()
        ticket = self.ticket(browser)
        for change in (
            {"method": "GET"},
            {"target": handoff.CONFIRM_PATH + "#route"},
            {"actual_tls": False},
            {"body": b"\xff" * 54},
            {"body": (ticket[:-1] + "B").encode()},
            {"fields": {"Origin": self.origins.account.value}},
            {"fields": {"Sec-Fetch-Site": None}},
            {"fields": {"Sec-Fetch-Mode": "cors"}},
            {"fields": {"Sec-Fetch-Dest": "document"}},
            {"fields": {"Content-Type": "text/plain;charset=UTF-8"}},
            {"fields": {"Content-Length": "0"}},
            {"fields": {"Transfer-Encoding": "chunked"}},
        ):
            with (
                self.subTest(keys=tuple(change)),
                self.assertRaises(web_named_headers.NamedRequestError),
            ):
                self.request(ticket, browser=browser, **change)
        with self.assertRaises(web_named_headers.NamedRequestError):
            self.request(ticket, browser=other)
        self.pair.assert_not_called()
        result = self.request(ticket, browser=browser)
        self.assertEqual((result.status, result.body), (204, b""))
        headers = dict(result.headers)
        self.assertRegex(
            headers["Set-Cookie"],
            r"\A__Host-ocbox-app=[A-Za-z0-9_-]{43}; Secure; HttpOnly; SameSite=Strict; Path=/\Z",
        )
        self.assertNotIn(NATIVE, str(headers))
        with self.assertRaises(web_named_headers.NamedRequestError):
            self.request(ticket, browser=browser)
        with self.assertRaises(web_named_headers.NamedRequestError):
            self.request(ticket, cross=True)
        self.pair.assert_called_once_with(self.binding, self.origins.workspace)

    def test_owner_loss_and_expiry_are_fail_closed(self):
        browser = self.paired()
        ticket = self.ticket(browser)
        self.request(ticket, cross=True)
        self.now += 30
        with self.assertRaises(web_named_headers.NamedRequestError):
            self.request(ticket, browser=browser)
        with self.assertRaises(web_named_headers.NamedRequestError):
            self.request(ticket, cross=True)
        ticket = self.ticket(browser)
        self.owner.return_value = False
        with self.assertRaises(web_named_headers.NamedRequestError):
            self.request(ticket, cross=True)
        self.pair.assert_not_called()


if __name__ == "__main__":
    unittest.main()
