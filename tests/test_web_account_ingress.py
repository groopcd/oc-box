"""Synthetic account requests, with no listener, PAKE implementation or native I/O."""

import base64
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from unittest.mock import Mock, patch

import test_web_pake_flow as fixture

from oc_box import (
    web_account_ingress as ingress,
    web_accounts,
    web_admission,
    web_handoff,
    web_local_origins,
    web_named_headers,
    web_named_local,
    web_pake_flow,
)

START = b'{"username":"in-scope","ke1":"+/8="}'
ACCOUNT_COOKIE = "__Host-ocbox-account"
ACCOUNT_NONCE = "D" * 42 + "M"
OTHER_ACCOUNT_NONCE = "E" * 42 + "Q"
REFUSED = b'{"error":"Named sign-in refused."}'


class AccountIngressTests(unittest.TestCase):
    def setUp(self):
        # The sibling bootstrap change adds this production cookie to the gate.
        # Keep these tests independently runnable on the original source base.
        allow = patch.object(
            web_named_headers,
            "_COOKIE_NAMES",
            web_named_headers._COOKIE_NAMES | {ACCOUNT_COOKIE},
        )
        allow.start()
        self.addCleanup(allow.stop)
        self.origins = web_local_origins.LocalOrigins(account_port=10443)
        self.binding = web_admission.WebBinding(
            fixture.WORKSPACE, "synthetic-owner", self.origins.workspace.value
        )
        self.backend = fixture.SyntheticBackend()
        flow = web_pake_flow.PakeFlow(
            self.binding,
            store=web_accounts.AccountStore(
                fixture.SETUP,
                (web_accounts.Account("in-scope", (fixture.WORKSPACE,), fixture.REAL),),
            ),
            backend=self.backend,
            sizes=fixture.SIZES,
            auth_origin=self.origins.account.value,
        )
        self.owner = Mock(return_value=True)
        self.pair_native = Mock()
        self.sign_in = web_named_local.LocalNamedSignIn(
            origins=self.origins,
            binding=self.binding,
            flow=flow,
            owner_current=self.owner,
            pair_native=self.pair_native,
        )
        self.addCleanup(self.sign_in.close)
        self.translate_nonce = Mock(
            side_effect={
                ACCOUNT_NONCE: fixture.BROWSER,
                OTHER_ACCOUNT_NONCE: fixture.OTHER_BROWSER,
            }.get
        )
        self.handler = ingress.AccountIngress(
            origins=self.origins,
            sign_in=self.sign_in,
            translate_nonce=self.translate_nonce,
        )

    def send(
        self,
        target=ingress.START_PATH,
        body=START,
        *,
        replace_headers=(),
        add=(),
        **kwargs,
    ):
        fields = {
            "Host": self.origins.account.value.removeprefix("https://"),
            "Origin": self.origins.account.value,
            "Cookie": f"{ACCOUNT_COOKIE}={ACCOUNT_NONCE}",
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        }
        for name, value in replace_headers:
            if value is None:
                del fields[name]
            else:
                fields[name] = value
        return self.handler(
            **{
                "method": "POST",
                "target": target,
                "headers": [*fields.items(), *add],
                "body": body,
                "actual_tls": True,
                **kwargs,
            }
        )

    def finish(self, challenge, proof=fixture.PROOF, **kwargs):
        body = json.dumps({"challenge_id": challenge, "ke3": proof}).encode("ascii")
        return self.send(ingress.FINISH_PATH, body, **kwargs)

    def assert_response(self, response, status):
        self.assertEqual(response.status, status)
        headers = dict(response.headers)
        self.assertEqual(
            set(headers),
            {
                "Cache-Control",
                "Content-Length",
                "Content-Security-Policy",
                "Content-Type",
                "Cross-Origin-Resource-Policy",
                "Referrer-Policy",
                "X-Content-Type-Options",
            },
        )
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Content-Length"], str(len(response.body)))
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def assert_refused(self, response):
        self.assert_response(response, 403)
        self.assertEqual(response.body, REFUSED)

    def assert_no_dispatch(self):
        self.translate_nonce.assert_not_called()
        self.owner.assert_not_called()
        self.pair_native.assert_not_called()
        self.assertEqual(self.backend.starts, [])
        self.assertEqual(self.backend.finishes, [])

    def test_exact_routes_return_only_challenge_fields_and_post_ticket(self):
        with patch.object(self.sign_in, "start", wraps=self.sign_in.start) as start:
            response = self.send(
                replace_headers=(
                    (
                        "Cookie",
                        f"opencode_session=ignored-native; {ACCOUNT_COOKIE}={ACCOUNT_NONCE}",
                    ),
                ),
                add=(("Sec-Fetch-Site", "same-origin"),),
            )
        start.assert_called_once_with(
            browser_nonce=fixture.BROWSER, username="in-scope", ke1=fixture.KE1
        )
        self.assert_response(response, 200)
        challenge = json.loads(response.body)
        self.assertEqual(set(challenge), {"challenge_id", "ke2", "context"})
        self.assertEqual(challenge["ke2"], fixture.KE2)
        self.assertEqual(
            challenge["context"], web_pake_flow._b64(self.backend.starts[0]["context"])
        )
        self.assertNotIn(challenge["challenge_id"], repr(response))
        self.translate_nonce.assert_called_once_with(ACCOUNT_NONCE)
        self.owner.assert_called_once_with(self.binding)

        with patch.object(self.sign_in, "finish", wraps=self.sign_in.finish) as finish:
            response = self.finish(challenge["challenge_id"])
        finish.assert_called_once_with(
            browser_nonce=fixture.BROWSER,
            challenge_id=challenge["challenge_id"],
            ke3=fixture.PROOF,
        )
        self.assert_response(response, 200)
        result = json.loads(response.body)
        self.assertEqual(set(result), {"ticket"})
        self.assertRegex(result["ticket"], r"\A[A-Za-z0-9_-]{53}[AQgw]\Z")
        self.assertNotIn(result["ticket"], repr(response))
        self.assertIsNone(self.sign_in.admission.lookup(self.binding, result["ticket"]))
        self.assert_refused(self.finish(challenge["challenge_id"]))
        self.pair_native.assert_not_called()

    def test_raw_paths_and_methods_have_no_other_dispatch_or_fallback(self):
        for target in (
            "/",
            "//start",
            "/%73tart",
            "/start/",
            "/start?",
            "/start#fragment",
            "/finish?ticket=synthetic",
            "/auth/",
            "/api/start",
            self.origins.account.value + "/start",
        ):
            with self.subTest(target=target):
                self.assert_refused(self.send(target))
        for method in ("GET", "HEAD", "OPTIONS", "PUT", "post"):
            with self.subTest(method=method):
                self.assert_refused(self.send(method=method))
        self.assert_no_dispatch()

    def test_wrong_context_never_reaches_bootstrap_or_owner(self):
        for kwargs in (
            {"actual_tls": False},
            {"actual_tls": 1},
            {"replace_headers": (("Host", None),)},
            {"replace_headers": (("Host", self.origins.workspace.hostname),)},
            {"replace_headers": (("Origin", None),)},
            {"replace_headers": (("Origin", "null"),)},
            {"replace_headers": (("Origin", self.origins.workspace.value),)},
            {"replace_headers": (("Origin", self.origins.account.value + "/"),)},
            {"add": (("oRiGiN", self.origins.account.value),)},
            {"add": (("Referer", self.origins.workspace.value + "/"),)},
            {"add": (("Sec-Fetch-Site", "cross-site"),)},
            {"add": (("Sec-Fetch-Site", "same-site"),)},
            {"add": (("Sec-Fetch-Site", "none"),)},
            {"add": (("X-Forwarded-Proto", "https"),)},
            {"add": (("Authorization", "synthetic"),)},
            {"add": (("Upgrade", "websocket"), ("Connection", "upgrade"))},
        ):
            with self.subTest(kwargs=kwargs):
                self.assert_refused(self.send(**kwargs))
        self.assert_no_dispatch()

    def test_constructor_refuses_different_account_or_workspace_origin(self):
        for origins in (
            replace(self.origins, workspace_port=11444),
            replace(self.origins, account_port=11443),
        ):
            with self.assertRaisesRegex(ValueError, "Invalid named account ingress"):
                ingress.AccountIngress(
                    origins=origins,
                    sign_in=self.sign_in,
                    translate_nonce=self.translate_nonce,
                )
        self.assert_no_dispatch()

    def test_framing_requires_single_exact_length_and_unencoded_json(self):
        for name, value in (
            ("Content-Length", None),
            ("Content-Length", "0"),
            ("Content-Length", str(len(START) - 1)),
            ("Content-Length", str(len(START) + 1)),
            ("Content-Length", f"+{len(START)}"),
            ("Content-Length", f"0{len(START)}"),
            ("Content-Length", f"{len(START)},{len(START)}"),
            ("Content-Length", "9" * 5000),
            ("Content-Type", None),
            ("Content-Type", "application/x-www-form-urlencoded"),
            ("Content-Type", "application/json; charset=utf-8"),
        ):
            with self.subTest(header=name, size=len(value or "")):
                self.assert_refused(self.send(replace_headers=((name, value),)))
        for item in (
            ("content-LENGTH", str(len(START))),
            ("content-TYPE", "application/json"),
            ("Transfer-Encoding", "chunked"),
            ("Content-Encoding", "gzip"),
            ("Trailer", "Content-Length"),
            ("TE", "trailers"),
            ("Expect", "100-continue"),
            ("Connection", "content-length"),
        ):
            with self.subTest(header=item[0]):
                self.assert_refused(self.send(add=(item,)))
        self.assert_refused(self.send(body=b""))
        self.assert_refused(self.send(body=bytearray(START)))
        self.assert_refused(self.send(body=START.ljust(ingress.MAX_BODY_BYTES + 1)))
        self.assert_no_dispatch()
        self.assert_response(self.send(body=START.ljust(ingress.MAX_BODY_BYTES)), 200)

    def test_duplicate_forbidden_and_malformed_json_has_no_effect(self):
        for name in (
            "password",
            "code",
            "native_cookie",
            "workspace_id",
            "browser_nonce",
            "url",
        ):
            with self.subTest(field=name):
                body = json.dumps(
                    {"username": "in-scope", "ke1": fixture.KE1, name: "ignored"}
                )
                self.assert_refused(self.send(body=body.encode("ascii")))
        for body in (
            b'{"username":"in-scope","username":"in-scope"}',
            b'{"username":"in-scope","ke1":"+/8=","k\\u00651":"+/8="}',
            b'{"username":"in-scope","ke1":"+/9="}',  # Noncanonical padding bits.
            b'{"username":"in-scope","ke1":"-_8="}',  # Not standard base64.
            b'{"username":"in-scope","ke1":null}',
            b'{"username":"in-scope","ke1":NaN}',
            b'{"username":"in-scope","ke1":true}',
            b'{"username":"in-scope","ke1":[1]}',
            b'{"username":"in-scope","ke1":{"a":"+","b":"8="}}',
            b'{"username":"in-scope","ke1":"\\ud800"}',
            b'{"username":"Invalid","ke1":"+/8="}',
            b'{"username":"\\u0000","ke1":"+/8="}',
            b'{"username":"in-scope","ke1":"+/8=","extra":{}}',
            b'{"username":"in-scope"}',
            b'{"username":"in-scope","ke3":"AwQ="}',
            b'["in-scope","+/8="]',
            b"\xef\xbb\xbf" + START,
            b'{"username":"\xff","ke1":"+/8="}',
            START + b"{}",
            START[:-1],
            b"[" * 2000 + b"]" * 2000,
        ):
            with self.subTest(size=len(body)):
                self.assert_refused(self.send(body=body))
        oversized_message = base64.b64encode(
            bytes(web_pake_flow.MAX_MESSAGE_BYTES + 1)
        ).decode("ascii")
        body = json.dumps({"username": "in-scope", "ke1": oversized_message})
        self.assert_refused(self.send(body=body.encode("ascii")))
        self.assert_no_dispatch()

    def test_account_cookie_must_resolve_to_distinct_valid_app_nonce(self):
        for cookie in (
            None,
            "__Host-ocbox-bootstrap=" + fixture.BROWSER,
            "__Host-ocbox-app=" + fixture.BROWSER,
            f"{ACCOUNT_COOKIE}={ACCOUNT_NONCE[:-1]}B",
            f"{ACCOUNT_COOKIE}={ACCOUNT_NONCE}; {ACCOUNT_COOKIE}={ACCOUNT_NONCE}",
        ):
            with self.subTest(length=len(cookie or "")):
                self.assert_refused(self.send(replace_headers=(("Cookie", cookie),)))
        self.assert_refused(
            self.send(add=(("cookie", f"{ACCOUNT_COOKIE}={ACCOUNT_NONCE}"),))
        )
        self.assert_no_dispatch()

        unissued = "C" * 42 + "I"
        self.assert_refused(
            self.send(replace_headers=(("Cookie", f"{ACCOUNT_COOKIE}={unissued}"),))
        )
        self.translate_nonce.assert_called_once_with(unissued)
        self.translate_nonce.side_effect = None
        for result in (True, "malformed", ACCOUNT_NONCE):
            self.translate_nonce.return_value = result
            self.assert_refused(self.send())
        self.owner.assert_not_called()
        self.assertEqual(self.backend.starts, [])
        self.pair_native.assert_not_called()

    def test_wrong_origin_and_other_browser_cannot_burn_correct_finish(self):
        challenge = json.loads(self.send().body)["challenge_id"]
        self.translate_nonce.reset_mock()
        self.owner.reset_mock()
        self.assert_refused(
            self.finish(
                challenge, replace_headers=(("Origin", self.origins.workspace.value),)
            )
        )
        # Noncanonical base64 is rejected before it reaches the flow.
        self.assert_refused(self.finish(challenge, proof="AwR="))
        self.assert_refused(self.finish(challenge[:-1] + "B"))
        self.assert_refused(
            self.send(
                ingress.FINISH_PATH,
                json.dumps(
                    {"challenge_id": challenge, "ke3": fixture.PROOF, "ticket": "x"}
                ).encode("ascii"),
            )
        )
        self.translate_nonce.assert_not_called()
        self.owner.assert_not_called()
        self.assert_refused(
            self.finish(
                challenge,
                replace_headers=(
                    ("Cookie", f"{ACCOUNT_COOKIE}={OTHER_ACCOUNT_NONCE}"),
                ),
            )
        )
        self.assertEqual(self.backend.finishes, [])
        self.assert_response(self.finish(challenge), 200)
        self.assertEqual(len(self.backend.finishes), 1)
        self.pair_native.assert_not_called()

    def test_flow_failure_and_wrong_suite_length_remain_uniform_and_one_attempt(self):
        challenge = json.loads(self.send().body)["challenge_id"]
        self.assert_refused(self.finish(challenge, proof="AQ=="))
        self.assert_refused(self.finish(challenge))
        self.assertEqual(self.backend.finishes, [])
        self.assertEqual(len(self.backend.discards), 1)

        challenge = json.loads(self.send().body)["challenge_id"]
        self.assert_refused(self.finish(challenge, proof=fixture.WRONG))
        self.assert_refused(self.finish(challenge))
        self.assertEqual(len(self.backend.finishes), 1)
        self.pair_native.assert_not_called()

    def test_issued_bootstrap_is_not_proof_of_current_owner(self):
        challenge = json.loads(self.send().body)["challenge_id"]
        self.owner.return_value = False
        self.assert_refused(self.finish(challenge))
        self.assert_refused(self.send())
        self.assertEqual(len(self.backend.starts), 1)
        self.assertEqual(self.backend.finishes, [])
        self.assertEqual(len(self.backend.discards), 1)
        self.pair_native.assert_not_called()

    def test_private_errors_and_unexpected_driver_results_do_not_escape(self):
        detail = "synthetic-private-error"
        invalid_challenge = web_pake_flow.PakeChallenge(fixture.BROWSER, detail, "bad")
        invalid_ticket = web_handoff.HandoffToken(detail)
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            self.assertNoLogs(level="DEBUG"),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            self.translate_nonce.side_effect = RuntimeError(detail)
            self.assert_refused(self.send())
            self.translate_nonce.side_effect = None
            self.translate_nonce.return_value = fixture.BROWSER
            with patch.object(self.sign_in, "start", side_effect=RuntimeError(detail)):
                self.assert_refused(self.send())
            with patch.object(self.sign_in, "start", return_value=invalid_challenge):
                self.assert_refused(self.send())
            with patch.object(self.sign_in, "finish", return_value=invalid_ticket):
                self.assert_refused(self.finish(fixture.BROWSER))
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.pair_native.assert_not_called()


if __name__ == "__main__":
    unittest.main()
