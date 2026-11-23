"""Exercise both local origin guards with no listener or shared host discovery."""

import json
import os
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

import test_web_pake_flow as fixture

from oc_box import (
    web_account_ingress,
    web_admission,
    web_app_ingress,
    web_bootstrap,
    web_local_origins,
    web_named_headers,
    web_named_local,
    web_native_pair,
)


class LocalIngressCompositionTests(unittest.TestCase):
    def test_separate_cookies_and_app_confirmation_enforce_live_native_scope(self):
        case = fixture.PakeFlowTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        origins = web_local_origins.LocalOrigins()
        binding = replace(case.binding, origin=origins.workspace.value)
        case.binding = binding
        flow = case.flow(auth_origin=origins.account.value)
        endpoint = "http://127.0.0.1:50100"
        owner = Mock(side_effect=lambda value: value == binding)
        native = web_native_pair.NativeCookie(
            binding,
            endpoint,
            origins.workspace.value.removeprefix("https://"),
            "a" * 10 + "." + "b" * 43,
            owner_current=owner,
        )
        pair = Mock(return_value=native)
        sign_in = web_named_local.LocalNamedSignIn(
            origins=origins,
            binding=binding,
            flow=flow,
            owner_current=owner,
            pair_native=pair,
        )
        self.addCleanup(sign_in.close)
        bootstrap = web_bootstrap.BrowserBootstrap(origins=origins, binding=binding)
        self.addCleanup(bootstrap.close)

        account_origin = origins.account.value
        app_origin = origins.workspace.value
        app_host = app_origin.removeprefix("https://")
        account_host = account_origin.removeprefix("https://")
        account = web_account_ingress.AccountIngress(
            origins=origins,
            sign_in=sign_in,
            translate_nonce=lambda nonce: bootstrap.app_nonce(
                binding, auth_origin=account_origin, account_nonce=nonce
            ),
        )
        with patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"}):
            app = web_app_ingress.NamedAppIngress(
                sign_in,
                endpoint=endpoint,
                bootstrap_current=lambda nonce: bootstrap.paired_app_current(
                    binding, nonce
                ),
            )
        ticket = bootstrap.issue(binding)
        account_cookie = bootstrap.accept(
            binding, ticket=ticket.value, auth_origin=account_origin
        )
        self.assertIsNotNone(account_cookie)

        def account_post(path, data, nonce):
            body = json.dumps(data).encode("ascii")
            return account(
                method="POST",
                target=path,
                actual_tls=True,
                body=body,
                headers=[
                    ("Host", account_host),
                    ("Origin", account_origin),
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                    ("Cookie", "__Host-ocbox-account=" + nonce),
                ],
            )

        begin = {"username": "in-scope", "ke1": fixture.KE1}
        self.assertEqual(account_post("/start", begin, ticket.app_nonce).status, 403)
        start = account_post("/start", begin, account_cookie)
        self.assertEqual(start.status, 200)
        result = json.loads(start.body)
        finish = account_post(
            "/finish",
            {"challenge_id": result["challenge_id"], "ke3": fixture.PROOF},
            account_cookie,
        )
        self.assertEqual(finish.status, 200)
        pair.assert_not_called()
        handoff = json.loads(finish.body)["ticket"]
        # The cross-site POST is a receiver, never the confirmation itself or
        # authority to alter a cookie. Its body goes to a script-free page.
        self.assertIsNone(
            web_named_headers.admit_headers(
                origin=origins.workspace,
                method="POST",
                actual_tls=True,
                form_origin=origins.account,
                headers=[
                    ("Host", app_host),
                    ("Origin", account_origin),
                    ("Sec-Fetch-Site", "cross-site"),
                    ("Sec-Fetch-Mode", "navigate"),
                    ("Sec-Fetch-Dest", "document"),
                ],
            )
        )

        def confirm(nonce):
            return app.confirm(
                method="POST",
                target=web_app_ingress.CONFIRM_PATH,
                actual_tls=True,
                ticket=handoff,
                headers=[
                    ("Host", app_host),
                    ("Origin", app_origin),
                    ("Cookie", "__Host-ocbox-bootstrap=" + nonce),
                ],
            )

        with self.assertRaises(web_app_ingress.AppRequestError):
            confirm(account_cookie)
        pair.assert_not_called()
        lease = confirm(ticket.app_nonce)
        pair.assert_called_once_with(binding, origins.workspace)
        self.assertTrue(
            bootstrap.retire(
                binding, auth_origin=account_origin, account_nonce=account_cookie
            )
        )
        with self.assertRaises(web_app_ingress.AppRequestError):
            confirm(ticket.app_nonce)

        headers = [
            ("Host", app_host),
            ("Origin", app_origin),
            ("Cookie", "__Host-ocbox-app=" + lease),
        ]
        run = Mock(return_value="synthetic metadata receipt")
        cancel = Mock()
        self.assertEqual(
            app.dispatch(
                method="GET",
                target="/api/info",
                headers=headers,
                actual_tls=True,
                cancel=cancel,
                run=run,
            ),
            "synthetic metadata receipt",
        )
        args = run.call_args.args
        self.assertEqual(args[:3], (endpoint, "GET", "/api/info"))
        self.assertEqual(
            dict(args[3])["Cookie"],
            native.upstream_cookie(
                binding=binding, endpoint=endpoint, origin=origins.workspace
            ),
        )
        self.assertNotIn(lease, repr(args[3]))
        self.assertTrue(
            app.logout(
                method="POST",
                target=web_app_ingress.LOGOUT_PATH,
                headers=headers,
                actual_tls=True,
            )
        )
        with self.assertRaises(web_app_ingress.StaleAppSessionError):
            app.dispatch(
                method="GET",
                target="/api/info",
                headers=headers,
                actual_tls=True,
                cancel=cancel,
                run=run,
            )
        self.assertEqual(run.call_count, 1)
        self.assertIsInstance(sign_in.admission, web_admission.BrowserAdmission)
