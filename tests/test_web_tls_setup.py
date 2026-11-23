"""Offline setup tests: no real mkcert, certutil, TTY, listener, or trust change."""

import fcntl
import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from oc_box import web_tls, web_tls_setup as setup


class RefusalTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(scratch.cleanup)
        self.home = Path(scratch.name)
        self.directory = self.home / "local"
        environment = patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"})
        environment.start()
        self.addCleanup(environment.stop)

    def refuses(self, expected, directory=None, **kwargs):
        with self.assertRaises(setup.WebTLSSetupError) as error:
            setup.setup(self.directory if directory is None else directory, **kwargs)
        self.assertEqual(error.exception.code, expected)
        self.assertNotIn(str(self.home), str(error.exception))
        self.assertNotIn("raw-helper-detail", str(error.exception))

    def test_container_guard_and_missing_approval(self):
        approved = Mock(return_value=True)
        with patch.object(setup, "find_helper") as find:
            with patch.dict(os.environ, {"OC_BOX_CONTAINER": "1"}):
                self.refuses("host_only", install_trust=True, approve=approved)
            with patch.object(setup, "_require_host"):
                self.refuses("approval_required", install_trust=True)
        find.assert_not_called()
        approved.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_unavailable_or_unsafe_tool_leaves_no_directory(self):
        with (
            patch.object(setup, "find_helper", return_value=None),
            patch.object(setup.subprocess, "run") as run,
        ):
            self.refuses("mkcert_missing")
        run.assert_not_called()
        impostor = self.home / "mkcert"
        impostor.touch(mode=0o700)
        with patch.object(setup, "find_helper", return_value=str(impostor)):
            self.refuses("mkcert_unsafe")
        self.assertFalse(self.directory.exists())

    def test_unsafe_directory_ancestors_and_existing_fixed_names(self):
        with patch.object(setup, "find_helper") as find:
            for path in ("relative", self.home / ".." / "local", "/"):
                with self.subTest(path=path):
                    self.refuses("unsafe_state", path)
            shared = self.home / "shared"
            shared.mkdir(mode=0o700)
            shared.chmod(0o777)
            self.refuses("unsafe_state", shared / "local")
            self.directory.mkdir(mode=0o755)
            self.directory.chmod(0o755)
            self.refuses("unsafe_state")
            self.directory.rmdir()
            self.directory.symlink_to(shared, target_is_directory=True)
            self.refuses("unsafe_state")
            self.directory.unlink()
            self.directory.mkdir(mode=0o700)
            key = self.directory / setup.KEY_NAME
            key.symlink_to(self.home / "absent")
            self.refuses("existing_state")
            key.unlink()
            os.mkfifo(key, 0o600)
            self.refuses("existing_state")
        find.assert_not_called()
        self.assertTrue(stat.S_ISFIFO(key.stat().st_mode))
        self.assertEqual(list(shared.iterdir()), [])

    def test_busy_setup_does_not_run_a_helper(self):
        self.directory.mkdir(mode=0o700)
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(setup, "find_helper") as find:
                self.refuses("busy")
            find.assert_not_called()
        finally:
            os.close(fd)

    def test_helper_failure_is_fixed_and_can_retry_an_empty_state(self):
        for error in (
            subprocess.TimeoutExpired(["raw-helper-detail"], timeout=30),
            subprocess.CalledProcessError(1, ["raw-helper-detail"]),
            PermissionError("raw-helper-detail"),
        ):
            with self.subTest(error=type(error)):
                with (
                    patch.object(setup, "_helper", return_value="/usr/bin/mkcert"),
                    patch.object(setup.subprocess, "run", side_effect=error),
                ):
                    self.refuses("generation_failed")
                self.assertEqual(list(self.directory.iterdir()), [])
                self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)


_OPENSSL = shutil.which("openssl", path="/usr/bin:/bin")


@unittest.skipUnless(_OPENSSL, "openssl required to issue disposable test certificates")
class BundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = tempfile.TemporaryDirectory(dir="/tmp")
        cls.addClassCleanup(scratch.cleanup)
        cls.fixture = Path(scratch.name)

        def openssl(*args):
            subprocess.run(
                [_OPENSSL, *args],
                cwd=cls.fixture,
                check=True,
                timeout=15,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={
                    "PATH": "/usr/bin:/bin",
                    "LC_ALL": "C",
                    "OPENSSL_CONF": "/dev/null",
                },
            )

        openssl(
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-nodes",
            "-days",
            "7",
            "-subj",
            "/CN=Disposable setup CA",
            "-addext",
            "basicConstraints=critical,CA:TRUE,pathlen:0",
            "-addext",
            "keyUsage=critical,keyCertSign",
            "-out",
            "ca.pem",
            "-keyout",
            "ca-key.pem",
        )
        openssl(
            "req",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-nodes",
            "-subj",
            "/CN=Disposable setup leaf",
            "-out",
            "server.csr",
            "-keyout",
            "server-key.pem",
        )
        extension = cls.fixture / "san.ext"
        for name, san in (
            ("server.pem", "DNS:localhost,IP:127.0.0.1"),
            ("lan.pem", "DNS:localhost,IP:127.0.0.1,IP:192.0.2.5"),
            ("no-ip.pem", "DNS:localhost"),
        ):
            extension.write_text(
                "basicConstraints=critical,CA:FALSE\n"
                "extendedKeyUsage=serverAuth\n"
                f"subjectAltName={san}\n",
                encoding="ascii",
            )
            openssl(
                "x509",
                "-req",
                "-in",
                "server.csr",
                "-CA",
                "ca.pem",
                "-CAkey",
                "ca-key.pem",
                "-CAcreateserial",
                "-days",
                "7",
                "-out",
                name,
                "-extfile",
                extension,
            )

    def setUp(self):
        scratch = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(scratch.cleanup)
        self.home = Path(scratch.name)
        self.directory = self.home / "nested/private"
        self.environ = patch.dict(
            os.environ,
            {
                "OC_BOX_CONTAINER": "0",
                "HOME": "/raw-helper-detail",
                "XDG_DATA_HOME": "/raw-helper-detail",
                "CAROOT": "/raw-helper-detail",
                "TRUST_STORES": "system,java",
                "LD_PRELOAD": "raw-helper-detail",
                "PATH": "/raw-helper-detail",
            },
        )
        self.environ.start()
        self.addCleanup(self.environ.stop)
        for name, kwargs in (
            ("_helper", {"side_effect": lambda tool: f"/usr/bin/{tool}"}),
            ("_require_host", {}),  # The isolated test runner can be UID 0.
        ):
            patcher = patch.object(setup, name, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_mkcert(
        self, command, *, leaf="server.pem", key="server-key.pem", **kwargs
    ):
        self.assertEqual(
            command,
            [
                "/usr/bin/mkcert",
                "-cert-file",
                "server.pem",
                "-key-file",
                "server-key.pem",
                "localhost",
                "127.0.0.1",
            ],
        )
        (stage_fd,) = kwargs["pass_fds"]
        stage = Path(f"/proc/self/fd/{stage_fd}")
        self.assertEqual(kwargs["cwd"], str(stage))
        self.assertEqual(
            kwargs["env"],
            {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "LC_ALL": "C",
                "HOME": str(stage),
                "CAROOT": str(stage / "ca"),
                "TRUST_STORES": "none",
            },
        )
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertTrue(kwargs["start_new_session"])
        self.assertTrue(kwargs["check"])
        self.assertEqual(kwargs["timeout"], 30)
        self.assertEqual(kwargs["umask"], 0o077)
        for source, destination, mode in (
            (leaf, "server.pem", 0o600),
            (key, "server-key.pem", 0o600),
            ("ca.pem", "ca/rootCA.pem", 0o600),
            ("ca-key.pem", "ca/rootCA-key.pem", 0o400),
        ):
            path = stage / destination
            path.write_bytes((self.fixture / source).read_bytes())
            path.chmod(mode)

    def test_secure_generation_and_idempotent_revalidation_without_helpers(self):
        approve = Mock(return_value=True)
        with patch.object(setup.subprocess, "run", side_effect=self.fake_mkcert) as run:
            cert, key = setup.setup(self.directory, approve=approve)
        run.assert_called_once()
        approve.assert_not_called()
        self.assertEqual(
            (cert, key),
            (self.directory / "server.pem", self.directory / "server-key.pem"),
        )
        self.assertEqual((self.directory / "ca").stat().st_mode & 0o777, 0o700)
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            (self.directory / "ca/rootCA-key.pem").stat().st_mode & 0o777, 0o400
        )
        self.assertEqual(
            {p.name for p in self.directory.iterdir()},
            {
                "server.pem",
                "server-key.pem",
                "ca",
                ".generated-v1",
            },
        )
        for origin in ("https://localhost", "https://127.0.0.1"):
            web_tls.validate(cert, key, origin)
        before = {path: (path.stat(), path.read_bytes()) for path in (cert, key)}
        with (
            patch.object(setup, "_helper") as helper,
            patch.object(setup, "validate", wraps=web_tls.validate) as validate,
        ):
            self.assertEqual(setup.setup(self.directory), (cert, key))
        helper.assert_not_called()
        validate.assert_called()
        for path, (info, content) in before.items():
            self.assertTrue(os.path.samestat(info, path.stat()))
            self.assertEqual(info.st_mtime_ns, path.stat().st_mtime_ns)
            self.assertEqual(content, path.read_bytes())

    def test_never_adopts_owned_supplied_pair_or_replaces_a_managed_pair(self):
        self.directory.mkdir(mode=0o700, parents=True)
        cert = self.directory / "server.pem"
        key = self.directory / "server-key.pem"
        for source, destination in (("lan.pem", cert), ("server-key.pem", key)):
            destination.write_bytes((self.fixture / source).read_bytes())
            destination.chmod(0o600)
        web_tls.validate(cert, key, "https://localhost")
        before = {path: path.read_bytes() for path in (cert, key)}
        with patch.object(setup.subprocess, "run") as run:
            with self.assertRaisesRegex(setup.WebTLSSetupError, "unrecognized"):
                setup.setup(self.directory)
        run.assert_not_called()
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
            path.unlink()
        with patch.object(setup.subprocess, "run", side_effect=self.fake_mkcert):
            setup.setup(self.directory)
        cert.write_bytes(before[cert])
        with patch.object(setup.subprocess, "run") as run:
            with self.assertRaisesRegex(setup.WebTLSSetupError, "unrecognized"):
                setup.setup(self.directory)
        run.assert_not_called()
        self.assertEqual(cert.read_bytes(), before[cert])

    def test_rejects_unexpected_san_or_key_and_publishes_nothing(self):
        for kwargs in (
            {"leaf": "lan.pem"},
            {"leaf": "no-ip.pem"},
            {"key": "ca-key.pem"},
        ):
            with self.subTest(kwargs=kwargs):

                def invalid(command, **options):
                    self.fake_mkcert(command, **kwargs, **options)

                with patch.object(setup.subprocess, "run", side_effect=invalid):
                    with self.assertRaises(setup.WebTLSSetupError) as error:
                        setup.setup(self.directory)
                self.assertEqual(error.exception.code, "invalid_bundle")
                self.assertEqual(list(self.directory.iterdir()), [])

    def test_competing_output_is_never_overwritten(self):
        supplied = b"supplied cert data"

        def added(command, **options):
            self.fake_mkcert(command, **options)
            (self.directory / setup.CERT_NAME).write_bytes(supplied)

        with patch.object(setup.subprocess, "run", side_effect=added):
            with self.assertRaises(setup.WebTLSSetupError) as error:
                setup.setup(self.directory)
        self.assertEqual(error.exception.code, "generation_failed")
        self.assertEqual(
            list(self.directory.iterdir()), [self.directory / setup.CERT_NAME]
        )
        self.assertEqual((self.directory / setup.CERT_NAME).read_bytes(), supplied)

    def nss(self, *, location=".pki/nssdb"):
        path = self.home / location
        path.mkdir(mode=0o700, parents=True)
        for name in ("cert9.db", "key4.db", "pkcs11.txt"):
            (path / name).touch(mode=0o600)
            if name.endswith(".db"):
                (path / name).write_bytes(b"SQLite format 3\0" + b"\0" * 84)
        (path / "pkcs11.txt").write_text(
            "library=\nname=NSS Internal PKCS #11 Module\n"
            f"parameters=configdir='sql:{path}' certPrefix='' keyPrefix='' "
            "secmod='secmod.db' flags= updatedir='' updateCertPrefix='' "
            "updateKeyPrefix='' updateid='' updateTokenDescription='' \n"
            "NSS=Flags=internal,critical trustOrder=75 cipherOrder=100 "
            "slotParams=(1={slotFlags=[ECC,RSA,TLS] askpw=any timeout=30})\n\n",
            encoding="ascii",
        )
        return path

    def test_trust_only_after_preflight_and_typed_approval(self):
        def helper(command, **kwargs):
            if command[0] == "/usr/bin/mkcert":
                return self.fake_mkcert(command, **kwargs)
            bound, nss_fd = kwargs["pass_fds"]
            target = Path(f"/proc/self/fd/{nss_fd}")
            self.assertTrue(target.samefile(nss))
            self.assertEqual(
                kwargs["env"],
                {
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "HOME": f"/proc/self/fd/{bound}",
                    "LC_ALL": "C",
                },
            )
            self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            self.assertEqual(kwargs["timeout"], 30)
            root = (self.fixture / "ca.pem").read_bytes()
            self.assertEqual(kwargs["stdin"].read(), root)
            self.assertEqual(
                command,
                [
                    "/usr/bin/certutil",
                    "-A",
                    "-a",
                    "-d",
                    f"sql:{target}",
                    "-h",
                    "internal",
                    "-t",
                    "C,,",
                    "-n",
                    "oc-box local web TLS " + hashlib.sha256(root).hexdigest(),
                ],
            )

        nss = self.nss(location=".local/share/pki/nssdb")
        with (
            patch.object(
                setup.pwd,
                "getpwuid",
                return_value=SimpleNamespace(pw_dir=str(self.home)),
            ),
            patch.object(setup.subprocess, "run", side_effect=helper) as run,
        ):
            for refusal in (False, "TRUST LOCAL CA"):
                with self.assertRaises(setup.WebTLSSetupError) as error:
                    setup.setup(
                        self.directory, install_trust=True, approve=lambda: refusal
                    )
                self.assertEqual(error.exception.code, "trust_refused")
                run.assert_not_called()
                self.assertFalse(self.directory.exists())
            approve = Mock(return_value=True)
            result = setup.setup(self.directory, install_trust=True, approve=approve)
            approve.assert_called_once_with()
            self.assertEqual(
                [call.args[0][0] for call in run.call_args_list],
                [
                    "/usr/bin/mkcert",
                    "/usr/bin/certutil",
                ],
            )
            run.reset_mock()
            self.assertEqual(
                setup.setup(self.directory, install_trust=True, approve=approve), result
            )
            self.assertEqual(
                [call.args[0][0] for call in run.call_args_list], ["/usr/bin/certutil"]
            )

    def test_refuses_redirected_nss_paths_or_module_loading_without_prompt(self):
        nss = self.nss()
        approved = Mock(return_value=True)
        config = nss / "pkcs11.txt"
        original = config.read_text(encoding="ascii")
        with (
            patch.object(
                setup.pwd,
                "getpwuid",
                return_value=SimpleNamespace(pw_dir=str(self.home)),
            ),
            patch.object(setup.subprocess, "run") as run,
        ):
            for text in (
                original.replace("library=", "library=/untrusted/modules.so"),
                original.replace(str(nss), "/etc/pki/nssdb"),
                original + "library=/untrusted/module.so\n",
            ):
                config.write_text(text, encoding="ascii")
                with self.assertRaises(setup.WebTLSSetupError) as error:
                    setup.setup(self.directory, install_trust=True, approve=approved)
                self.assertEqual(error.exception.code, "nss_database")
            config.write_text(original, encoding="ascii")
            db = nss / "cert9.db"
            db.unlink()
            db.symlink_to(self.fixture / "ca.pem")
            with self.assertRaises(setup.WebTLSSetupError) as error:
                setup.setup(self.directory, install_trust=True, approve=approved)
            self.assertEqual(error.exception.code, "nss_database")
        run.assert_not_called()
        approved.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_trust_failure_reports_uncertainty_and_leaves_generated_pair(self):
        self.nss()

        def failed_trust(command, **kwargs):
            if command[0] == "/usr/bin/certutil":
                raise subprocess.CalledProcessError(1, ["raw-helper-detail"])
            return self.fake_mkcert(command, **kwargs)

        with (
            patch.object(
                setup.pwd,
                "getpwuid",
                return_value=SimpleNamespace(pw_dir=str(self.home)),
            ),
            patch.object(setup.subprocess, "run", side_effect=failed_trust),
        ):
            with self.assertRaises(setup.WebTLSSetupError) as error:
                setup.setup(self.directory, install_trust=True, approve=lambda: True)
        self.assertEqual(error.exception.code, "trust_failed")
        self.assertIn("partial", str(error.exception))
        self.assertNotIn("raw-helper-detail", str(error.exception))
        self.assertTrue(all(path.exists() for path in setup.setup(self.directory)))

    def test_approval_does_not_authorize_a_replaced_nss_database(self):
        nss = self.nss()
        replacement = nss / "replacement"

        def approve():
            old = nss / "cert9.db"
            replacement.touch(mode=0o600)
            replacement.write_bytes(old.read_bytes())
            replacement.replace(old)
            return True

        with (
            patch.object(
                setup.pwd,
                "getpwuid",
                return_value=SimpleNamespace(pw_dir=str(self.home)),
            ),
            patch.object(setup.subprocess, "run", side_effect=self.fake_mkcert) as run,
        ):
            with self.assertRaises(setup.WebTLSSetupError) as error:
                setup.setup(self.directory, install_trust=True, approve=approve)
        self.assertEqual(error.exception.code, "nss_database")
        self.assertFalse(
            any(call.args[0][0] == "/usr/bin/certutil" for call in run.call_args_list)
        )


if __name__ == "__main__":
    unittest.main()
