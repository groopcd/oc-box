"""Loopback named-site preflight without a listener, DNS or trust-store changes."""

import contextlib
import io
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oc_box import oc_box, web_tls, web_tls_cli
from oc_box import web_local_origins as local

OPENSSL = shutil.which("openssl")


class LocalProfileTests(unittest.TestCase):
    def test_origins_use_distinct_sites_and_fixed_loopback_addresses(self):
        sites = local.LocalOrigins()
        self.assertEqual(sites.account.value, "https://accounts.ocbox.test:9443")
        self.assertEqual(sites.workspace.value, "https://workspace.ocbox-app.test:9444")
        self.assertEqual(sites.account_bind, ("127.0.0.1", 9443))
        self.assertEqual(sites.workspace_bind, ("127.0.0.1", 9444))
        for ports in (
            (9443, 9443),
            (443, 9444),
            (0, 9444),
            (9443, 65536),
            (True, 9444),
        ):
            with self.subTest(ports=ports), self.assertRaises(local.LocalOriginError):
                local.LocalOrigins(*ports)

    def test_sni_fails_closed_for_other_names_and_missing_host(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        local._isolate_sni(context, local.ACCOUNT_HOST)
        for name in (None, local.WORKSPACE_HOST, "127.0.0.1", "accounts.ocbox.test."):
            with self.subTest(name=name):
                self.assertEqual(
                    context.sni_callback(None, name, context),
                    ssl.ALERT_DESCRIPTION_UNRECOGNIZED_NAME,
                )
        self.assertIsNone(context.sni_callback(None, local.ACCOUNT_HOST, context))


@unittest.skipUnless(OPENSSL, "openssl is required for synthetic TLS files")
class LocalTLSFileTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.cert, self.key = (
            Path(self.folder) / "cert.pem",
            Path(self.folder) / "key.pem",
        )
        self.app_cert = Path(self.folder) / "app-cert.pem"
        self.app_key = Path(self.folder) / "app-key.pem"
        for name, cert, key in (
            (local.ACCOUNT_HOST, self.cert, self.key),
            (local.WORKSPACE_HOST, self.app_cert, self.app_key),
        ):
            subprocess.run(
                [
                    OPENSSL,
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-subj",
                    f"/CN={name}",
                    "-addext",
                    f"subjectAltName=DNS:{name}",
                    "-days",
                    "1",
                    "-out",
                    str(cert),
                    "-keyout",
                    str(key),
                ],
                check=True,
                capture_output=True,
                timeout=10,
            )
            key.chmod(0o600)

    def cli(self, *options):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(oc_box, "detect_opencode_release") as native,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            result = web_tls_cli.cli(
                oc_box,
                [
                    "check-local-pair",
                    "--json",
                    "--account-cert",
                    str(self.cert),
                    "--account-key",
                    str(self.key),
                    "--workspace-cert",
                    str(self.app_cert),
                    "--workspace-key",
                    str(self.app_key),
                    *options,
                ]
            )
        native.assert_not_called()
        self.assertNotIn(self.folder, output.getvalue())
        return result, json.loads(output.getvalue())

    def test_two_certificate_pairs_pass_without_claiming_browser_trust_or_dns(self):
        code, result = self.cli("--account-port", "10443", "--workspace-port", "10444")
        self.assertEqual(code, 0)
        self.assertEqual(result["account_origin"], "https://accounts.ocbox.test:10443")
        self.assertEqual(
            result["workspace_origin"], "https://workspace.ocbox-app.test:10444"
        )
        self.assertEqual(result["certificate"], "valid")
        self.assertEqual(result["browser_trust"], "unverified")
        self.assertEqual(result["name_resolution"], "unverified")
        self.assertIs(result["listener_started"], False)

    def test_refuses_certificate_from_other_site_and_port_collision(self):
        self.app_cert, self.app_key = self.cert, self.key
        code, result = self.cli()
        self.assertEqual((code, result["diagnostic"]), (1, "certificate_name"))
        code, result = self.cli("--workspace-port", "9443")
        self.assertEqual((code, result["diagnostic"]), (1, "local_profile"))

    def test_each_context_performs_tls_with_its_own_name(self):
        origins = local.LocalOrigins()
        pair = local.check_tls(
            origins,
            account_cert=self.cert,
            account_key=self.key,
            workspace_cert=self.app_cert,
            workspace_key=self.app_key,
        )
        verifier = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        verifier.load_verify_locations(cafile=self.cert)
        web_tls._verify_handshake(pair.account, verifier, local.ACCOUNT_HOST)
        with self.assertRaises(ssl.SSLError):
            web_tls._verify_handshake(pair.account, verifier, local.WORKSPACE_HOST)
