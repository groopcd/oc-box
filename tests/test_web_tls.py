"""Offline TLS configuration checks with disposable certificates and no listener."""

import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oc_box import web_tls


class OriginTests(unittest.TestCase):
    def test_canonical_https_origin(self):
        for raw, expected in (
            ("https://localhost", ("localhost", 443, "https://localhost")),
            (
                "https://127.0.0.1:55061",
                ("127.0.0.1", 55061, "https://127.0.0.1:55061"),
            ),
            ("https://192.168.1.45", ("192.168.1.45", 443, "https://192.168.1.45")),
            (
                "HTTPS://Workspace.Example.Test:443",
                ("workspace.example.test", 443, "https://workspace.example.test"),
            ),
            (
                "https://xn--bcher-kva.example",
                ("xn--bcher-kva.example", 443, "https://xn--bcher-kva.example"),
            ),
        ):
            with self.subTest(raw=raw):
                result = web_tls.parse_https_origin(raw)
                self.assertEqual((result.hostname, result.port, result.value), expected)

    def test_refuses_nonorigins_and_ambiguous_hosts(self):
        for raw in (
            "http://localhost",
            "https://0.0.0.0",
            "https://127.1",
            "https://0",
            "https://0177.0.0.1",
            "https://0x7f000001",
            "https://example.123",
            "https://[::1]",
            "https://*.example.test",
            "https://example.test.",
            "https://-local",
            "https://lo_cal",
            "https://bücher.example",
            "https://localhost:0",
            "https://localhost:65536",
            "https://localhost:0443",
            "https://localhost:",
            "https://",
            "https://foo@localhost",
            "https://localhost/path",
            "https://localhost/",
            "https://localhost?",
            "https://localhost#",
            "https://localhost\\foo",
            "https://localhost\n",
            " https://localhost",
            "https://localhost\r",
            "https://loc%61lhost",
            f"https://{'a' * 5000}",
            None,
        ):
            with self.subTest(raw=raw), self.assertRaises(web_tls.WebTLSError) as error:
                web_tls.parse_https_origin(raw)
            self.assertEqual(error.exception.code, "origin")

    def test_helper_discovery_ignores_ambient_path_and_does_not_execute_tools(self):
        with (
            patch.dict(os.environ, {"PATH": "/untrusted"}),
            patch.object(
                shutil, "which", side_effect=["/usr/bin/mkcert", None]
            ) as which,
            patch.object(
                subprocess, "Popen", side_effect=AssertionError("unexpected execution")
            ),
        ):
            self.assertEqual(web_tls.find_helper("mkcert"), "/usr/bin/mkcert")
            self.assertIsNone(web_tls.find_helper("certutil"))
            self.assertIsNone(web_tls.find_helper("/untrusted/mkcert"))
        self.assertEqual(
            [call.args for call in which.call_args_list], [("mkcert",), ("certutil",)]
        )
        for call in which.call_args_list:
            self.assertEqual(call.kwargs, {"path": "/usr/local/bin:/usr/bin:/bin"})


_OPENSSL = shutil.which("openssl", path="/usr/bin:/bin")


@unittest.skipUnless(_OPENSSL, "openssl required to issue disposable test certificates")
class ServerTLSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = tempfile.TemporaryDirectory(dir="/tmp")
        cls.addClassCleanup(fixture.cleanup)
        cls.fixture = Path(fixture.name)
        cls.key = cls.fixture / "server.key"
        cls.root = cls.fixture / "root.pem"
        cls.intermediate = cls.fixture / "intermediate.pem"
        cls.leaf = cls.fixture / "server.pem"
        cls.fullchain = cls.fixture / "fullchain.pem"
        cls.rootless = cls.fixture / "rootless.pem"
        cls._openssl(
            "req",
            "-new",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-nodes",
            "-days",
            "14",
            "-subj",
            "/CN=Fixture Root",
            "-addext",
            "basicConstraints=critical,CA:TRUE,pathlen:1",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
            "-keyout",
            "root.key",
            "-out",
            cls.root,
        )
        cls._openssl(
            "req",
            "-new",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-nodes",
            "-subj",
            "/CN=Fixture Intermediate",
            "-keyout",
            "intermediate.key",
            "-out",
            "intermediate.csr",
        )
        (cls.fixture / "intermediate.ext").write_text(
            "basicConstraints=critical,CA:TRUE,pathlen:0\n"
            "keyUsage=critical,keyCertSign,cRLSign\n",
            encoding="ascii",
        )
        cls._openssl(
            "x509",
            "-req",
            "-days",
            "14",
            "-set_serial",
            "2",
            "-in",
            "intermediate.csr",
            "-CA",
            cls.root,
            "-CAkey",
            "root.key",
            "-extfile",
            "intermediate.ext",
            "-out",
            cls.intermediate,
        )
        cls._openssl(
            "req",
            "-new",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-nodes",
            "-subj",
            "/CN=cn-only.test",
            "-out",
            "server.csr",
            "-keyout",
            cls.key,
        )
        (cls.fixture / "index").touch()
        (cls.fixture / "serial").write_text("10\n", encoding="ascii")
        (cls.fixture / "issuer.cnf").write_text(
            "[ca]\ndefault_ca=issuer\n[issuer]\n"
            "database=index\nserial=serial\nnew_certs_dir=.\nunique_subject=no\n"
            "certificate=intermediate.pem\nprivate_key=intermediate.key\n"
            "policy=names\ndefault_md=sha256\ndefault_days=7\n"
            "[names]\ncommonName=supplied\n"
            "[server]\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n"
            "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:192.168.1.45,DNS:*.example.test\n"
            "[cn_only]\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n"
            "[dns_ip]\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n"
            "subjectAltName=DNS:127.0.0.1\n"
            "[client_only]\nbasicConstraints=CA:FALSE\nextendedKeyUsage=clientAuth\n"
            "subjectAltName=DNS:localhost\n",
            encoding="ascii",
        )
        for name, section, dates in (
            ("server.pem", "server", ()),
            ("second-server.pem", "server", ()),
            ("cn-only.pem", "cn_only", ()),
            ("dns-ip.pem", "dns_ip", ()),
            ("client-only.pem", "client_only", ()),
            (
                "expired.pem",
                "server",
                ("-startdate", "20000101000000Z", "-enddate", "20000102000000Z"),
            ),
            (
                "future.pem",
                "server",
                ("-startdate", "20990101000000Z", "-enddate", "20990102000000Z"),
            ),
        ):
            cls._openssl(
                "ca",
                "-batch",
                "-notext",
                "-config",
                "issuer.cnf",
                "-in",
                "server.csr",
                "-extensions",
                section,
                "-out",
                name,
                *dates,
            )
        cls._openssl(
            "pkcs8",
            "-topk8",
            "-in",
            cls.key,
            "-out",
            "encrypted.key",
            "-passout",
            "pass:disposable-test-password",
        )
        chain = cls.leaf.read_bytes() + cls.intermediate.read_bytes()
        cls.rootless.write_bytes(chain)
        cls.fullchain.write_bytes(chain + cls.root.read_bytes())

    @classmethod
    def _openssl(cls, *args):
        subprocess.run(
            [_OPENSSL, *args],
            cwd=cls.fixture,
            check=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "OPENSSL_CONF": "/dev/null"},
        )

    def setUp(self):
        scratch = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)

    def _refuses(self, code, *, cert=None, key=None, origin="https://localhost"):
        cert = self.fullchain if cert is None else cert
        key = self.key if key is None else key
        with self.assertRaises(web_tls.WebTLSError) as failure:
            web_tls.validate(cert, key, origin)
        self.assertEqual(failure.exception.code, code)
        self.assertNotIn(str(cert), str(failure.exception))
        self.assertNotIn(str(key), str(failure.exception))
        self.assertNotIn(origin, str(failure.exception))
        self.assertIsNone(failure.exception.__cause__)

    def test_returns_server_context_for_leaf_full_and_rootless_chains(self):
        for cert, origin in (
            (self.leaf, "https://localhost"),
            (self.fullchain, "https://127.0.0.1:8443"),
            (self.rootless, "https://192.168.1.45"),
            (self.fullchain, "https://workspace.example.test"),
        ):
            with self.subTest(cert=cert.name, origin=origin):
                context = web_tls.validate(cert, self.key, origin)
                self.assertEqual(context.protocol, ssl.PROTOCOL_TLS_SERVER)
                self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_requires_dns_or_ip_san_for_exact_advertised_host(self):
        for cert, origin in (
            (self.leaf, "https://127.0.0.2"),
            (self.leaf, "https://a.b.example.test"),
            (self.leaf, "https://example.test"),
            (self.fixture / "cn-only.pem", "https://cn-only.test"),
            (self.fixture / "dns-ip.pem", "https://127.0.0.1"),
        ):
            with self.subTest(cert=cert.name, origin=origin):
                self._refuses("certificate_name", cert=cert, origin=origin)

    def test_fails_key_time_and_chain_checks_without_touching_host_trust(self):
        incomplete_chain = self.scratch / "missing-intermediate.pem"
        incomplete_chain.write_bytes(self.leaf.read_bytes() + self.root.read_bytes())
        with (
            patch.dict(os.environ, {"SSL_CERT_FILE": str(self.intermediate)}),
            patch.object(
                ssl.SSLContext, "load_default_certs", side_effect=AssertionError
            ),
            patch.object(
                socket, "socket", side_effect=AssertionError("unexpected socket")
            ),
        ):
            self._refuses("certificate_chain", cert=incomplete_chain)
            self._refuses("certificate_chain", cert=self.fixture / "client-only.pem")
            self._refuses("certificate_time", cert=self.fixture / "expired.pem")
            self._refuses("certificate_time", cert=self.fixture / "future.pem")
            self._refuses("key_mismatch", key=self.fixture / "root.key")
            self._refuses("key_encrypted", key=self.fixture / "encrypted.key")
            web_tls.validate(self.rootless, self.key, "https://localhost")

    def test_refuses_missing_unsafe_and_unbounded_inputs(self):
        missing = self.scratch / "missing-sensitive-path"
        self._refuses("key_missing", key=missing)
        self._refuses("certificate_missing", cert=missing)
        symlink = self.scratch / "key-link"
        symlink.symlink_to(self.key)
        self._refuses("key_file", key=symlink)
        symlink.unlink()
        symlink.symlink_to(self.fixture, target_is_directory=True)
        self._refuses("key_file", key=symlink / self.key.name)
        self._refuses("certificate_file", cert=symlink / self.leaf.name)
        self._refuses("key_file", key=self.scratch / ".." / self.key.name)
        self._refuses("key_file", key=self.scratch)
        special = self.scratch / "fifo"
        os.mkfifo(special, 0o600)
        self._refuses("key_file", key=special)
        writable = self.scratch / "public-key"
        writable.write_bytes(self.key.read_bytes())
        for mode in (0o644, 0o640, 0o620, 0o700, 0o200):
            with self.subTest(mode=oct(mode)):
                writable.chmod(mode)
                self._refuses("key_file", key=writable)
        writable.chmod(0o400)
        web_tls.validate(self.leaf, writable, "https://localhost")
        with patch.object(os, "getuid", return_value=os.getuid() + 1):
            self._refuses("key_file", key=writable)
        os.link(writable, self.scratch / "second-name")
        self._refuses("key_file", key=writable)
        oversized = self.scratch / "oversized"
        with oversized.open("wb") as stream:
            stream.truncate(100 * 1024)
        oversized.chmod(0o600)
        self._refuses("key_file", key=oversized)
        self._refuses("certificate_file", cert=oversized)
        shared_cert = self.scratch / "shared-cert.pem"
        shared_cert.write_bytes(self.leaf.read_bytes())
        shared_cert.chmod(0o664)
        self._refuses("certificate_file", cert=shared_cert)

    def test_refuses_malformed_and_overstuffed_pem(self):
        cert = self.scratch / "bad-crt"
        valid = self.leaf.read_bytes()
        not_der = b"-----BEGIN CERTIFICATE-----\nZm9v\n-----END CERTIFICATE-----\n"
        for bad in (
            b"not a certificate\n",
            not_der,
            valid + not_der + self.intermediate.read_bytes(),
            valid + b"extra text\n",
            valid + self.key.read_bytes(),
            valid * 2,
            self.fullchain.read_bytes()
            + b"".join(
                (self.fixture / name).read_bytes()
                for name in (
                    "second-server.pem",
                    "cn-only.pem",
                    "dns-ip.pem",
                    "client-only.pem",
                    "expired.pem",
                    "future.pem",
                )
            ),
        ):
            cert.write_bytes(bad)
            self._refuses("certificate_invalid", cert=cert)
        key = self.scratch / "bad-key"
        key.write_bytes(b"not a private key\n")
        key.chmod(0o600)
        self._refuses("key_load", key=key)

    def test_material_is_frozen_and_private_before_loading(self):
        cert = self.scratch / "cert.pem"
        key = self.scratch / "key.pem"
        cert.write_bytes(self.fullchain.read_bytes())
        key.write_bytes(self.key.read_bytes())
        key.chmod(0o600)
        native_load = ssl.SSLContext.load_cert_chain

        def replace_originals(context, pinned_cert, pinned_key, *, password):
            cert.write_bytes(b"changed certificate")
            key.write_bytes(b"changed key")
            self.assertEqual(Path(pinned_cert).stat().st_mode & 0o777, 0o600)
            self.assertEqual(Path(pinned_key).stat().st_mode & 0o777, 0o600)
            with self.assertRaises(PermissionError):
                Path(pinned_key).write_bytes(b"not allowed")
            return native_load(context, pinned_cert, pinned_key, password=password)

        with patch.object(ssl.SSLContext, "load_cert_chain", replace_originals):
            web_tls.validate(cert, key, "https://localhost")

    def test_refuses_changed_key_during_snapshot(self):
        key = self.scratch / "changing.key"
        key.write_bytes(self.key.read_bytes())
        key.chmod(0o600)
        native_sendfile = os.sendfile

        def change_permissions(out_fd, in_fd, offset, count):
            copied = native_sendfile(out_fd, in_fd, offset, count)
            if os.fstat(in_fd).st_ino == key.stat().st_ino:
                key.chmod(0o644)
            return copied

        with patch.object(os, "sendfile", change_permissions):
            self._refuses("key_file", key=key)

    def test_missing_memfd_support_fails_closed(self):
        with patch.object(
            os, "memfd_create", side_effect=PermissionError("private detail")
        ):
            self._refuses("unavailable")


if __name__ == "__main__":
    unittest.main()
