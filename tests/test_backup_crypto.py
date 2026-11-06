import json
import os
from pathlib import Path
import pty
import select
import termios
import time
import shutil
import sys
import tarfile
import tempfile
import subprocess
import unittest
from unittest.mock import patch

import backup_crypto as crypto


@unittest.skipUnless(
    shutil.which("age") and shutil.which("age-keygen"),
    "optional age host tools unavailable",
)
class AgeEnvelopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-age-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.identity = self.root / "identity.txt"
        self.identity.write_bytes(crypto.small(["age-keygen"]))
        self.identity.chmod(0o600)
        self.recipient = (
            crypto.small(["age-keygen", "-y", str(self.identity)]).decode().strip()
        )

    def envelope(self, *, kind="portable"):
        staging = self.root / "ciphertext"
        staging.mkdir()
        _, recipient = crypto.generate_key([self.recipient], staging / "key.age")
        components = {}
        for name in (
            "project.age",
            "home.age",
            "registry.age",
            *(("image.age",) if kind == "full" else ()),
        ):
            crypto.encrypt_bytes(
                b"synthetic-private-backup-data", recipient, staging / name
            )
            components[name] = crypto.digest(staging / name)
        manifest = {
            "format": "oc-box-backup",
            "format_version": 1,
            "kind": kind,
            "components": components,
            "private-marker": "private-manifest-data",
        }
        crypto.encrypt_bytes(
            json.dumps(manifest).encode(), recipient, staging / "manifest.age"
        )
        output = self.root / "backup.ocbox"
        with output.open("wb") as stream:
            crypto.pack(staging, stream)
        return output, staging

    def repack(self, staging, output):
        with output.open("wb") as stream:
            crypto.pack(staging, stream)

    def test_age_recipient_envelope_roundtrip_has_no_plaintext_metadata_or_secret(self):
        output, _ = self.envelope()
        raw = output.read_bytes()
        self.assertNotIn(b"synthetic-private-backup-data", raw)
        self.assertNotIn(b"private-manifest-data", raw)
        self.assertNotIn(b"AGE-SECRET-KEY-1", raw)
        with crypto.opened(output, [self.identity]) as (manifest, staging, key):
            self.assertEqual(manifest["private-marker"], "private-manifest-data")
            self.assertEqual(
                crypto.decrypt_bytes(staging / "project.age", key),
                b"synthetic-private-backup-data",
            )

    def test_ciphertext_tamper_or_component_swap_is_rejected(self):
        output, staging = self.envelope()
        first = staging / "project.age"
        raw = bytearray(first.read_bytes())
        raw[-1] ^= 1
        first.write_bytes(raw)
        self.repack(staging, output)
        with self.assertRaisesRegex(crypto.BackupError, "checksum/size mismatch"):
            with crypto.opened(output, [self.identity]):
                self.fail("tampered component accepted")

    def test_wrong_recipient_identity_cannot_open_backup(self):
        output, _ = self.envelope()
        wrong = self.root / "wrong.txt"
        wrong.write_bytes(crypto.small(["age-keygen"]))
        wrong.chmod(0o600)
        with self.assertRaises(crypto.BackupError):
            with crypto.opened(output, [wrong]):
                self.fail("wrong identity accepted")

    def test_manifest_tamper_is_authenticated(self):
        output, staging = self.envelope()
        manifest = staging / "manifest.age"
        raw = bytearray(manifest.read_bytes())
        raw[-10] ^= 1
        manifest.write_bytes(raw)
        self.repack(staging, output)
        with self.assertRaises(crypto.BackupError):
            with crypto.opened(output, [self.identity]):
                self.fail("tampered manifest accepted")

    def test_duplicate_path_link_and_unknown_outer_entries_are_refused(self):
        for name, kind in (
            ("../outside", tarfile.REGTYPE),
            ("project.age", tarfile.SYMTYPE),
            ("extra", tarfile.REGTYPE),
        ):
            with self.subTest(name=name):
                output = self.root / "malicious.ocbox"
                with tarfile.open(output, "w") as archive:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.linkname = "/tmp/outside"
                    archive.addfile(member)
                with self.assertRaises(crypto.BackupError):
                    with crypto.opened(output, [self.identity]):
                        self.fail("unsafe outer entry accepted")

    def test_duplicate_valid_outer_name_is_rejected(self):
        output = self.root / "duplicate.ocbox"
        with tarfile.open(output, "w") as archive:
            archive.addfile(tarfile.TarInfo("key.age"))
            archive.addfile(tarfile.TarInfo("key.age"))
        with self.assertRaisesRegex(crypto.BackupError, "duplicates"):
            with crypto.opened(output, [self.identity]):
                self.fail("duplicate accepted")

    def test_age_plugins_or_insecure_keys_are_never_executed(self):
        output, staging = self.envelope()
        plugin = self.root / "plugin.txt"
        plugin.write_text(
            "AGE-PLUGIN-TEST-1MALICIOUS\nAGE-SECRET-KEY-1" + "A" * 58 + "\n"
        )
        plugin.chmod(0o600)
        with patch.object(crypto, "small") as execute:
            with self.assertRaisesRegex(crypto.BackupError, "native unencrypted"):
                crypto.load_key(staging / "key.age", [plugin])
            execute.assert_not_called()
        self.identity.chmod(0o644)
        with self.assertRaisesRegex(crypto.BackupError, "private"):
            with crypto.opened(output, [self.identity]):
                self.fail("insecure identity accepted")

    def test_decrypted_key_envelope_cannot_activate_an_age_plugin(self):
        malicious = b"AGE-PLUGIN-TEST-1MALICIOUS\nAGE-SECRET-KEY-1" + b"A" * 58 + b"\n"
        with patch.object(crypto, "small", return_value=malicious) as execute:
            with self.assertRaisesRegex(
                crypto.BackupError, "Invalid backup key envelope"
            ):
                crypto.load_key(self.root / "untrusted.age", [])
        self.assertEqual(execute.call_count, 1)

    def test_real_streaming_roundtrip_does_not_spool_plaintext(self):
        staging = self.root / "stream.age"
        value = b"large-synthetic-private-value" * 100000
        producer = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'large-synthetic-private-value'*100000)",
        ]
        crypto.encrypt_stream(producer, self.recipient, staging)
        private = self.identity.read_bytes()
        with crypto.memory_file(private) as identity, crypto.memory_file() as output:
            crypto.stream_pipeline(
                [
                    "age",
                    "--decrypt",
                    "--identity",
                    f"/proc/self/fd/{identity.fileno()}",
                    str(staging),
                ],
                [
                    sys.executable,
                    "-c",
                    "import shutil,sys; shutil.copyfileobj(sys.stdin.buffer,sys.stdout.buffer)",
                ],
                first_fds=(identity.fileno(),),
                destination=output,
            )
            output.seek(0)
            self.assertEqual(output.read(), value)
        self.assertNotIn(b"large-synthetic-private-value", staging.read_bytes())

    def test_pipeline_failure_and_timeout_terminate_both_processes(self):
        with self.assertRaisesRegex(crypto.BackupError, "failed"):
            crypto.stream_pipeline(
                [sys.executable, "-c", "import time; time.sleep(20)"],
                [sys.executable, "-c", "raise SystemExit(1)"],
                timeout=2,
            )
        with self.assertRaisesRegex(crypto.BackupError, "timed out"):
            crypto.stream_pipeline(
                [sys.executable, "-c", "import time; time.sleep(20)"],
                [sys.executable, "-c", "import time; time.sleep(20)"],
                timeout=0.1,
            )

    def test_age_passphrase_envelope_uses_real_tty_never_env_or_argv(self):
        # Exercise only synthetic data through the real age prompt and key
        # envelope, preserving the surrounding terminal on all paths.
        master, slave = pty.openpty()
        original = termios.tcgetattr(slave)
        output = self.root / "passphrase.age"
        code = "import fcntl,os,sys,termios; os.setsid(); fcntl.ioctl(0,termios.TIOCSCTTY,0); sys.path.insert(0,sys.argv[1]); import backup_crypto as c; from pathlib import Path; c.generate_key([],Path(sys.argv[2])); p=c.load_key(Path(sys.argv[2]),[]); assert b'AGE-SECRET-KEY-1' in p; print('PASSPHRASE_ROUNDTRIP_OK',flush=True)"
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                code,
                str(Path(__file__).resolve().parents[1]),
                str(output),
            ],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=crypto.environment(),
        )
        seen = bytearray()
        phrase = b"synthetic-age-backup-passphrase"
        prompts = [b"Enter passphrase", b"Confirm passphrase", b"Enter passphrase"]
        step, offset = 0, 0
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    seen.extend(os.read(master, 65536))
                if step < len(prompts) and prompts[step] in seen[offset:]:
                    os.write(master, phrase + b"\n")
                    offset = len(seen)
                    step += 1
                if process.poll() is not None:
                    break
            self.assertIsNotNone(process.poll(), bytes(seen))
            self.assertEqual(process.returncode, 0, bytes(seen))
            self.assertIn(b"PASSPHRASE_ROUNDTRIP_OK", seen)
            self.assertNotIn(phrase, seen)
            self.assertNotIn(phrase, output.read_bytes())
            self.assertEqual(termios.tcgetattr(slave), original)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            os.close(master)
            os.close(slave)


class BackupEnvironmentTests(unittest.TestCase):
    def test_backup_children_never_inherit_tokens_loader_hooks_or_age_settings(self):
        with patch.dict(
            os.environ,
            {
                "PERSONAL_GITHUB_TOKEN": "synthetic",
                "BRAVE_API_KEY": "synthetic-api",
                "AGE_PASSPHRASE": "not-used",
                "AGE_PLUGIN_PATH": "/bad",
                "LD_PRELOAD": "/bad",
                "PYTHONPATH": "/bad",
                "HTTP_PROXY": "http://secret.invalid",
            },
        ):
            env = crypto.environment()
        for key in (
            "PERSONAL_GITHUB_TOKEN",
            "BRAVE_API_KEY",
            "AGE_PASSPHRASE",
            "AGE_PLUGIN_PATH",
            "LD_PRELOAD",
            "PYTHONPATH",
            "HTTP_PROXY",
        ):
            self.assertNotIn(key, env)

    def test_missing_age_yields_actionable_optional_dependency_message(self):
        with patch.object(crypto.shutil, "which", return_value=None):
            with self.assertRaisesRegex(crypto.BackupError, "package 'age'"):
                crypto.tools()


if __name__ == "__main__":
    unittest.main()
