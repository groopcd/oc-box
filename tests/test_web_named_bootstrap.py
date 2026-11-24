import re
import unittest
from unittest.mock import Mock

from oc_box import (
    web_admission,
    web_bootstrap,
    web_local_origins,
    web_named_bootstrap,
    web_named_headers,
)


class NamedBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.origins = web_local_origins.LocalOrigins()
        self.binding = web_admission.WebBinding(
            "a" * 32, "b" * 32, self.origins.workspace.value
        )
        self.bootstrap = web_bootstrap.BrowserBootstrap(
            origins=self.origins, binding=self.binding
        )
        self.current = Mock(return_value=True)
        self.ingress = web_named_bootstrap.NamedBootstrapIngress(
            origins=self.origins,
            binding=self.binding,
            bootstrap=self.bootstrap,
            owner_current=self.current,
        )

    def workspace(self, **edits):
        request = {
            "method": "GET",
            "target": web_named_bootstrap.WORKSPACE_ENTRY,
            "headers": [
                ("Host", self.origins.workspace.value.removeprefix("https://")),
                ("Sec-Fetch-Site", "none"),
                ("Sec-Fetch-Mode", "navigate"),
                ("Sec-Fetch-Dest", "document"),
            ],
            "body": b"",
            "actual_tls": True,
        }
        return self.ingress.workspace(**(request | edits))

    def account(self, body, **edits):
        request = {
            "method": "POST",
            "target": web_named_bootstrap.ACCOUNT_ENTRY,
            "headers": [
                ("Host", self.origins.account.value.removeprefix("https://")),
                ("Origin", self.origins.workspace.value),
                ("Sec-Fetch-Site", "cross-site"),
                ("Sec-Fetch-Mode", "navigate"),
                ("Sec-Fetch-Dest", "document"),
                ("Content-Length", str(len(body))),
                ("Content-Type", "application/x-www-form-urlencoded"),
            ],
            "body": body,
            "actual_tls": True,
        }
        return self.ingress.account(**(request | edits))

    @staticmethod
    def ticket(entry):
        token = re.search(rb'name="ticket" value="([^"]+)"', entry.html)[1]
        return b"ticket=" + token

    @staticmethod
    def nonce(cookie):
        return cookie.split(";", 1)[0].split("=", 1)[1]

    def test_exact_browser_transition_has_only_distinct_bootstrap_cookies(self):
        entry = self.workspace()
        self.assertIn(
            f'action="{self.origins.account.value}/auth/oc-box-account-entry"'.encode(),
            entry.html,
        )
        self.assertNotIn(b"password", entry.html)
        self.assertIn("frame-ancestors 'none'", entry.policy)
        self.assertEqual(dict(entry.headers())["Referrer-Policy"], "origin")
        flags = "; Secure; HttpOnly; SameSite=Strict; Path=/"
        self.assertTrue(entry.cookie.startswith("__Host-ocbox-bootstrap="))
        self.assertTrue(entry.cookie.endswith(flags))
        result = self.account(self.ticket(entry))
        self.assertTrue(result.cookie.startswith("__Host-ocbox-account="))
        self.assertTrue(result.cookie.endswith(flags))
        self.assertNotEqual(self.nonce(entry.cookie), self.nonce(result.cookie))
        self.assertEqual(
            self.bootstrap.app_nonce(
                self.binding,
                auth_origin=self.origins.account.value,
                account_nonce=self.nonce(result.cookie),
            ),
            self.nonce(entry.cookie),
        )
        self.assertNotIn(self.nonce(result.cookie), repr(result))
        with self.assertRaises(web_named_headers.NamedRequestError):
            self.account(self.ticket(entry))

    def test_rejects_non_tls_subresources_queries_and_request_bodies_before_mint(self):
        base = [
            ("Host", self.origins.workspace.value.removeprefix("https://")),
            ("Sec-Fetch-Site", "none"),
            ("Sec-Fetch-Mode", "navigate"),
            ("Sec-Fetch-Dest", "iframe"),
        ]
        for edits in (
            {"actual_tls": False},
            {"headers": base},
            {"target": web_named_bootstrap.WORKSPACE_ENTRY + "?ticket=elsewhere"},
            {"body": b"x"},
            {"method": "POST"},
        ):
            with (
                self.subTest(fields=tuple(edits)),
                self.assertRaises(web_named_headers.NamedRequestError),
            ):
                self.workspace(**edits)
        self.current.assert_not_called()

    def test_wrong_form_origin_old_cookie_and_extra_fields_do_not_burn_ticket(self):
        entry = self.workspace()
        body = self.ticket(entry)
        base = [
            ("Host", self.origins.account.value.removeprefix("https://")),
            ("Sec-Fetch-Site", "cross-site"),
            ("Sec-Fetch-Mode", "navigate"),
            ("Sec-Fetch-Dest", "document"),
            ("Content-Length", "50"),
            ("Content-Type", "application/x-www-form-urlencoded"),
        ]
        headers = [*base, ("Origin", self.origins.workspace.value)]
        for request in (
            [*base, ("Origin", "https://foreign.invalid")],
            [*base, ("Origin", "null")],
            [*headers, ("Cookie", "__Host-ocbox-account=fixture")],
            [*headers, ("Transfer-Encoding", "chunked")],
        ):
            with self.assertRaises(web_named_headers.NamedRequestError):
                self.account(body, headers=request)
        with self.assertRaises(web_named_headers.NamedRequestError):
            self.account(body + b"&extra=x")
        self.assertTrue(self.account(body).cookie.startswith("__Host-ocbox-account="))

    def test_owner_loss_on_workspace_entry_retires_bootstraps(self):
        self.current.side_effect = [True, False]
        with self.assertRaises(web_named_headers.NamedRequestError):
            self.workspace()
        self.assertIsNone(self.bootstrap.issue(self.binding))

    def test_owner_loss_on_account_entry_retires_bootstraps(self):
        entry = self.workspace()
        self.current.side_effect = [True, RuntimeError("private owner diagnostic")]
        with self.assertRaisesRegex(
            web_named_headers.NamedRequestError, "not admitted"
        ):
            self.account(self.ticket(entry))
        self.assertIsNone(self.bootstrap.issue(self.binding))
