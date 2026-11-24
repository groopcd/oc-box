"""First-party guest HTTP framing; no installed routes or browser credentials."""

import unittest

from oc_box import web_studio_guest as guest
from oc_box import web_studio_guest_ingress as ingress
from oc_box import web_studio_guest_read as guest_read


_APP, _PAGE = "a" * 42 + "A", "b" * 42 + "A"
_COOKIE = "synthetic=not-authority"
_BINDING = guest.GuestBinding("a" * 32, "b" * 32, "c" * 64, "http://127.0.0.1:48231")


def _head(method, path, *, body=None, headers=(), drops=(), version="HTTP/1.1"):
    fields = {
        "Host": _BINDING.authority,
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "same-origin",
        "Sec-Fetch-Dest": "empty",
        "Cookie": _COOKIE,
    }
    if path != guest.CONFIRM_PATH:
        fields.update({ingress.APP: _APP, ingress.PAGE: _PAGE})
    if body is not None:
        fields.update(
            {
                "Origin": _BINDING.origin,
                "Content-Length": str(len(body)),
                "Content-Type": "text/plain"
                if path == guest.CONFIRM_PATH
                else "application/json",
            }
        )
    if (
        method == "POST"
        and path == ingress.SEND
        or path.startswith(ingress.CONVERSATION + "?")
    ):
        fields[ingress.GENERATION] = "2"
    for key in drops:
        fields.pop(key)
    return "\r\n".join(
        [
            f"{method} {path} {version}",
            *(f"{name}: {value}" for name, value in [*fields.items(), *headers]),
        ]
    ).encode("ascii")


class GuestIngressTests(unittest.TestCase):
    def test_exact_confirmation_and_all_four_scoped_operations(self):
        challenge = _APP.encode("ascii")
        parsed = ingress.prepare(
            _BINDING, _head("POST", guest.CONFIRM_PATH, body=challenge)
        )
        self.assertEqual(parsed.length, 43)
        confirmation = parsed.complete(challenge)
        self.assertIsInstance(confirmation, guest.ConfirmationRequest)
        self.assertIsNotNone(guest._confirmation(_BINDING, confirmation))
        send_body = b'{"sessionID":"ses_exact","text":"synthetic private input"}'
        matrix = (
            ("GET", ingress.CATALOG, None, guest_read.Catalog()),
            (
                "GET",
                ingress.CONVERSATION + "?session=ses_exact",
                None,
                guest_read.Transcript("ses_exact", 2),
            ),
            (
                "POST",
                ingress.SELECTION,
                b'{"sessionID":null,"sequence":1}',
                guest_read.Select(1, None),
            ),
            (
                "POST",
                ingress.SELECTION,
                b'{"sessionID":"ses_exact","sequence":2}',
                guest_read.Select(2, "ses_exact"),
            ),
            ("POST", ingress.SEND, send_body, None),
        )
        for method, path, body, expected in matrix:
            with self.subTest(path=path):
                prepared = ingress.prepare(_BINDING, _head(method, path, body=body))
                packet = prepared.complete(body or b"")
                self.assertEqual(
                    (packet.app, packet.page, packet.cookie), (_APP, _PAGE, _COOKIE)
                )
                self.assertNotIn(_APP, repr(prepared))
                self.assertNotIn(_COOKIE, repr(packet))
                if expected is not None:
                    self.assertEqual(packet.operation, expected)
                else:
                    self.assertIsInstance(packet.operation, ingress.Send)
                    self.assertEqual(packet.operation.text, "synthetic private input")
                    self.assertEqual(
                        (packet.operation.session_id, packet.operation.generation),
                        ("ses_exact", 2),
                    )
                    self.assertNotIn("synthetic private input", repr(packet.operation))

    def test_missing_native_cookie_reaches_the_gate_as_none(self):
        prepared = ingress.prepare(
            _BINDING, _head("GET", ingress.CATALOG, drops=("Cookie",))
        )
        self.assertIsNone(prepared.complete(b"").cookie)
        bad = _head(
            "GET", ingress.CATALOG, drops=("Cookie",), headers=(("Cookie", ""),)
        )
        with self.assertRaises(ingress.GuestRequestError):
            ingress.prepare(_BINDING, bad)

    def test_rejects_alt_framing_or_context_before_reading_any_body(self):
        empty_send = b"{}"
        matrix = (
            _head("GET", ingress.CATALOG, version="HTTP/1.0"),
            _head("HEAD", ingress.CATALOG),
            _head("GET", "http://" + _BINDING.authority + ingress.CATALOG),
            _head("GET", ingress.CATALOG + "?cache=1"),
            _head("GET", ingress.CONVERSATION + "?session=ses_exact&other=1"),
            _head("GET", ingress.CONVERSATION + "?session=ses_%65xact"),
            _head("GET", ingress.CATALOG, headers=(("hOsT", _BINDING.authority),)),
            _head(
                "GET", ingress.CATALOG, headers=(("Origin", "http://127.0.0.1:48232"),)
            ),
            _head("GET", ingress.CATALOG, headers=(("Cookie", _COOKIE),)),
            _head("GET", ingress.CATALOG, drops=(ingress.PAGE,)),
            _head("GET", ingress.CATALOG, headers=((ingress.GENERATION, "1"),)),
            _head("GET", ingress.CATALOG, headers=(("Content-Length", "1"),)),
            _head("POST", ingress.SEND, body=empty_send, drops=("Origin",)),
            _head("POST", ingress.SEND, body=empty_send, drops=(ingress.GENERATION,)),
            _head(
                "POST",
                ingress.SEND,
                body=empty_send,
                drops=("Content-Length",),
                headers=(("Content-Length", "02"),),
            ),
            _head(
                "POST",
                ingress.SEND,
                body=empty_send,
                drops=("Content-Length",),
                headers=(("Content-Length", "8193"),),
            ),
            _head(
                "POST",
                ingress.SEND,
                body=empty_send,
                headers=(("Connection", "close, upgrade"),),
            ),
            _head(
                "POST",
                ingress.SEND,
                body=empty_send,
                headers=(("Transfer-Encoding", "chunked"),),
            ),
            _head(
                "POST",
                ingress.SEND,
                body=empty_send,
                headers=(("Content-Encoding", "gzip"),),
            ),
            _head(
                "POST",
                ingress.SEND,
                body=empty_send,
                headers=(("X-Forwarded-Host", _BINDING.authority),),
            ),
            _head(
                "POST",
                ingress.SEND,
                body=empty_send,
                headers=(("Authorization", "Basic private"),),
            ),
            _head(
                "POST",
                ingress.SEND,
                body=empty_send,
                headers=(("Idempotency-Key", "fixture"),),
            ),
        )
        for index, head in enumerate(matrix):
            with self.subTest(case=index), self.assertRaises(ingress.GuestRequestError):
                ingress.prepare(_BINDING, head)

    def test_rejects_partial_surplus_ambiguous_or_browser_supplied_native_data(self):
        matrix = (
            (ingress.SELECTION, b'{"sessionID":null,"sequence":true}'),
            (ingress.SELECTION, b'{"sessionID":null,"sequence":9007199254740992}'),
            (ingress.SELECTION, b'{"sessionID":"elsewhere","sequence":1}'),
            (ingress.SEND, b'{"sessionID":"ses_exact","text":"private","resume":true}'),
            (
                ingress.SEND,
                b'{"sessionID":"ses_exact","text":"private","text":"second"}',
            ),
            (ingress.SEND, b'{"sessionID":"ses_exact","text":"\\ud800"}'),
        )
        for path, body in matrix:
            with self.subTest(path=path), self.assertRaises(ingress.GuestRequestError):
                ingress.prepare(_BINDING, _head("POST", path, body=body)).complete(body)
        body = b'{"sessionID":"ses_exact","text":"private"}'
        prepared = ingress.prepare(_BINDING, _head("POST", ingress.SEND, body=body))
        for other in (body[:-1], body + b"\r\nGET /native HTTP/1.1\r\n"):
            with self.assertRaises(ingress.GuestRequestError):
                prepared.complete(other)
