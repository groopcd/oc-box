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

from oc_box import backup_crypto as crypto


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

    def test_pipeline_error_identifies_consumer_storage_failure_without_revealing_stderr(
        self,
    ):
        with self.assertRaises(crypto.BackupError) as error:
            crypto.stream_pipeline(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.buffer.write(b'fixture')",
                ],
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdin.buffer.read(); sys.stderr.write('synthetic-secret: no space left on device /private/path'); sys.exit(125)",
                ],
            )
        self.assertIn("consumer exit 125", str(error.exception))
        self.assertIn("space exhausted", str(error.exception))
        self.assertNotIn("synthetic-secret", str(error.exception))
        self.assertNotIn("/private/path", str(error.exception))

    def test_pipeline_error_identifies_producer_and_unknown_failures_without_tool_output(
        self,
    ):
        for text, expected in (
            ("synthetic-secret failed to decrypt", "decryption/authentication failed"),
            ("synthetic-secret unrecognized failure", "private output was not logged"),
        ):
            with (
                self.subTest(text=text),
                self.assertRaises(crypto.BackupError) as error,
            ):
                crypto.stream_pipeline(
                    [
                        sys.executable,
                        "-c",
                        "import sys; sys.stderr.write(" + repr(text) + "); sys.exit(2)",
                    ],
                    [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                )
            self.assertIn("producer exit 2", str(error.exception))
            self.assertIn(expected, str(error.exception))
            self.assertNotIn("synthetic-secret", str(error.exception))

    def test_pipeline_image_format_fallback_does_not_hide_disk_space_error(self):
        with self.assertRaises(crypto.BackupError) as error:
            crypto.stream_pipeline(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.buffer.write(b'fixture-image')",
                ],
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdin.buffer.read(); sys.stderr.write('payload does not match any formats: private-file: no space left on device'); sys.exit(125)",
                ],
            )
        self.assertIn("consumer exit 125", str(error.exception))
        self.assertIn("space exhausted", str(error.exception))
        self.assertNotIn("private-file", str(error.exception))

    def test_stream_tools_honor_explicit_temp_location_without_forwarding_secrets(self):
        with tempfile.TemporaryDirectory(prefix="oc-box-pipeline-temp-") as temporary:
            with (
                patch.dict(
                    os.environ,
                    {
                        "TMPDIR": temporary,
                        "OPENAI_API_KEY": "synthetic-private",
                        "AGE_PASSPHRASE": "synthetic-private",
                    },
                ),
                crypto.memory_file() as captured,
            ):
                crypto.stream_pipeline(
                    [
                        sys.executable,
                        "-B",
                        "-c",
                        "import json,os,sys; sys.stdout.write(json.dumps({'tmp':os.environ.get('TMPDIR'),'provider':os.environ.get('OPENAI_API_KEY'),'password':os.environ.get('AGE_PASSPHRASE')}))",
                    ],
                    [
                        sys.executable,
                        "-B",
                        "-c",
                        "import json,os,sys; item=json.load(sys.stdin); item['consumer_tmp']=os.environ.get('TMPDIR'); print(json.dumps(item))",
                    ],
                    destination=captured,
                )
                captured.seek(0)
                result = json.load(captured)
            self.assertEqual(
                result,
                {
                    "tmp": temporary,
                    "consumer_tmp": temporary,
                    "provider": None,
                    "password": None,
                },
            )

    def test_quota_error_does_not_echo_paths_or_credentials_and_explains_free_space_limit(
        self,
    ):
        with self.assertRaises(crypto.BackupError) as error:
            crypto.stream_pipeline(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.buffer.write(b'fixture-image')",
                ],
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdin.buffer.read(); sys.stderr.write('write /private/synthetic-secret: disk quota exceeded'); sys.exit(125)",
                ],
            )
        self.assertIn("consumer exit 125", str(error.exception))
        self.assertIn("quota exceeded", str(error.exception))
        self.assertIn("free space does not establish quota", str(error.exception))
        self.assertNotIn("/private", str(error.exception))
        self.assertNotIn("synthetic-secret", str(error.exception))

    def test_age_passphrase_envelope_uses_real_tty_never_env_or_argv(self):
        # Exercise only synthetic data through the real age prompt and key
        # envelope, preserving the surrounding terminal on all paths.
        master, slave = pty.openpty()
        original = termios.tcgetattr(slave)
        output = self.root / "passphrase.age"
        code = "import fcntl,os,sys,termios; os.setsid(); fcntl.ioctl(0,termios.TIOCSCTTY,0); sys.path.insert(0,sys.argv[1]+'/src'); from oc_box import backup_crypto as c; from pathlib import Path; c.generate_key([],Path(sys.argv[2]),minimum=8); p=c.load_key(Path(sys.argv[2]),[]); assert b'AGE-SECRET-KEY-1' in p; print('PASSPHRASE_ROUNDTRIP_OK',flush=True)"
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
        phrase = b"eight888"
        prompts = [
            b"New backup passphrase",
            b"Confirm backup passphrase",
            b"Enter passphrase",
        ]
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
    @unittest.skipUnless(shutil.which("age"), "optional age host tool unavailable")
    def test_backup_age_bridge_keeps_password_out_of_argv_env_and_files(self):
        phrase = "éight888"
        with tempfile.TemporaryDirectory(prefix="oc-box-password-bridge-") as temporary:
            path = Path(temporary) / "key.age"
            with (
                patch.object(crypto.secret_prompt, "read_secret", return_value=phrase),
                patch.object(
                    crypto.subprocess, "Popen", wraps=subprocess.Popen
                ) as child,
            ):
                crypto.encrypt_passphrase(b"synthetic-private-key", path, 8)
            arguments = child.call_args.args[0]
            self.assertNotIn(phrase, " ".join(arguments))
            self.assertNotIn(phrase, json.dumps(child.call_args.kwargs["env"]))
            self.assertNotIn(phrase.encode(), path.read_bytes())
            self.assertEqual(list(Path(temporary).iterdir()), [path])

    def test_stricter_backup_policy_applies_to_creation_and_not_recipients(self):
        with tempfile.TemporaryDirectory(prefix="oc-box-backup-policy-") as temporary:
            settings = Path(temporary) / "oc-box/config.json"
            settings.parent.mkdir()
            settings.write_text('{"backup_min_passphrase_length":16}')
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": temporary}):
                minimum = crypto.passphrase_minimum([])
                self.assertEqual(minimum, 16)
                self.assertEqual(crypto.passphrase_minimum([], 8), 8)
                with (
                    patch.object(
                        crypto.secret_prompt, "read_secret", return_value="eight888"
                    ),
                    patch.object(crypto.subprocess, "Popen") as child,
                    self.assertRaisesRegex(crypto.BackupError, "16 or more"),
                ):
                    crypto.encrypt_passphrase(
                        b"key", Path(temporary) / "unused.age", minimum
                    )
                child.assert_not_called()
                settings.write_text('{"backup_min_passphrase_length":7}')
                self.assertIsNone(crypto.passphrase_minimum(["public-key"]))
                with self.assertRaisesRegex(
                    crypto.BackupError, "backup_min_passphrase_length"
                ):
                    crypto.passphrase_minimum([])

    def test_backup_creation_policy_rejects_bad_options_before_backend_access(self):
        from oc_box import backups
        from oc_box import oc_box as box

        for options in (
            ["--min-passphrase-length", "7"],
            ["--min-passphrase-length", "1025"],
            ["--recipient", "age1" + "q" * 58, "--min-passphrase-length", "8"],
        ):
            with (
                self.subTest(options=options),
                patch.object(box, "find_workspace") as lookup,
                self.assertRaises(crypto.BackupError),
            ):
                args = backups.parser_for("backup").parse_args(
                    ["--output", "/tmp/example.ocbox", *options]
                )
                backups.make_backup(box, args)
            lookup.assert_not_called()

    def test_short_or_mismatched_backup_password_never_starts_age(self):
        for answers in (["short77"], ["eight888", "different"]):
            with (
                self.subTest(answers=answers),
                patch.object(crypto.secret_prompt, "read_secret", side_effect=answers),
                patch.object(crypto.subprocess, "Popen") as child,
                self.assertRaises(crypto.BackupError),
            ):
                crypto.encrypt_passphrase(b"synthetic-key", Path("unused.age"), 8)
            child.assert_not_called()

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
            with self.assertRaisesRegex(crypto.BackupError, "age, age-keygen"):
                crypto.tools()


if __name__ == "__main__":
    unittest.main()
