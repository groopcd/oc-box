"""Exercise the prospective header gate, never a live named listener."""

import unittest

from oc_box import web_named_headers as gate
from oc_box.web_local_origins import LocalOrigins

ORIGINS = LocalOrigins()
APP = ORIGINS.workspace
ACCOUNT = ORIGINS.account
HOST = APP.value.removeprefix("https://")
TOKEN = "A" * 43


class NamedHeadersTests(unittest.TestCase):
    def admit(self, fields, **overrides):
        options = {
            "origin": APP,
            "headers": [("Host", HOST), *fields],
            "method": "POST",
            "actual_tls": True,
            **overrides,
        }
        return gate.admit_headers(**options)

    def test_exact_origin_extracts_only_the_requested_browser_lease(self):
        token = self.admit(
            [
                ("Origin", APP.value),
                ("Cookie", "opencode_session=ignored; __Host-ocbox-app=" + TOKEN),
            ],
            cookie_name="__Host-ocbox-app",
        )
        self.assertEqual(token, TOKEN)
        self.assertIsNone(self.admit([("Origin", APP.value)]))

    def test_exact_tls_host_and_origin_are_required(self):
        for fields, options in (
            ([("Origin", APP.value)], {"actual_tls": False}),
            ([("Origin", APP.value)], {"headers": [("Host", ACCOUNT.hostname)]}),
            ([("Origin", APP.value), ("hOSt", HOST)], {}),
            ([("Origin", APP.value), ("origin", APP.value)], {}),
            ([("Origin", "http://" + HOST)], {}),
            ([("Origin", ACCOUNT.value)], {}),
            ([("Origin", "null")], {}),
            ([("Origin", APP.value + "/")], {}),
            ([], {}),
            ([("Origin", APP.value), ("Referer", ACCOUNT.value + "/")], {}),
            ([("Origin", APP.value), ("X-Forwarded-Proto", "https")], {}),
            ([("Origin", APP.value), ("Authorization", "ignored")], {}),
            ([("Origin", APP.value), ("X-Extra", "bad\r\nfield")], {}),
            ([("Origin", APP.value), ("X-Extra", "a" * 16384)], {}),
        ):
            with (
                self.subTest(fields=[item[0] for item in fields], options=options),
                self.assertRaisesRegex(gate.NamedRequestError, "not admitted"),
            ):
                self.admit(fields, **options)

    def test_regular_navigation_has_bounded_origin_exception(self):
        for site in ("same-origin", "none"):
            self.assertIsNone(self.admit([("Sec-Fetch-Site", site)], method="GET"))
        for fields in (
            [],
            [("Sec-Fetch-Site", "same-site")],
            [("Sec-Fetch-Site", "cross-site")],
        ):
            with self.subTest(fields=fields), self.assertRaises(gate.NamedRequestError):
                self.admit(fields, method="GET")

    def test_websocket_requires_get_upgrade_envelope_and_exact_origin(self):
        headers = [
            ("Upgrade", "websocket"),
            ("Connection", "keep-alive, Upgrade"),
            ("Origin", APP.value),
        ]
        self.assertIsNone(self.admit(headers, websocket=True, method="GET"))
        for fields, options in (
            (headers, {"websocket": True, "method": "POST"}),
            (headers[:-1], {"websocket": True, "method": "GET"}),
            (headers[1:], {"websocket": True, "method": "GET"}),
            (headers, {"method": "GET"}),
        ):
            with (
                self.subTest(options=options),
                self.assertRaises(gate.NamedRequestError),
            ):
                self.admit(fields, **options)

    def test_handoff_post_is_cross_site_document_navigation_and_never_reads_cookies(
        self,
    ):
        headers = [
            ("Origin", ACCOUNT.value),
            ("Sec-Fetch-Site", "cross-site"),
            ("Sec-Fetch-Mode", "navigate"),
            ("Sec-Fetch-Dest", "document"),
        ]
        self.assertIsNone(self.admit(headers, form_origin=ACCOUNT))
        for fields, options in (
            (headers, {}),
            (headers, {"form_origin": ACCOUNT, "method": "GET"}),
            (headers, {"form_origin": ACCOUNT, "cookie_name": "__Host-ocbox-app"}),
            (
                headers + [("Cookie", "__Host-ocbox-app=" + TOKEN)],
                {"form_origin": ACCOUNT},
            ),
            (headers[:-1], {"form_origin": ACCOUNT}),
            (headers[1:], {"form_origin": ACCOUNT}),
        ):
            with (
                self.subTest(options=options),
                self.assertRaises(gate.NamedRequestError),
            ):
                self.admit(fields, **options)

    def test_missing_malformed_and_duplicate_session_cookies_refuse(self):
        for value in (
            "",
            "opencode_session=" + TOKEN,
            "__Host-ocbox-app=" + TOKEN[:-1] + "B",
            "__Host-ocbox-app=" + TOKEN + "; __Host-ocbox-app=" + TOKEN,
        ):
            with (
                self.subTest(kind=len(value)),
                self.assertRaises(gate.NamedRequestError),
            ):
                self.admit(
                    [("Origin", APP.value), ("Cookie", value)],
                    cookie_name="__Host-ocbox-app",
                )
